import hashlib
import json
import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
from conformance import compare_sav_semantics, write_supported_semantics_fixture
from openstatspec.core import UnsupportedOperationError


_REQUIRED_ENGINE_LOSS = [
    "file-label-and-documents-unobservable",
    "separate-write-format-unobservable",
]


def test_pyspssio_round_trip_uses_one_wide_table_and_catalog(tmp_path) -> None:
    source = tmp_path / "tiny.sav"
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    exported = tmp_path / "roundtrip.zsav"
    pyspssio.write_sav(
        str(source),
        pd.DataFrame({"age": [34.0, None], "name": ["Ada", ""]}),
        metadata={
            "var_types": {"name": 12},
            "var_labels": {"age": "Age", "name": "Name"},
            "var_value_labels": {"age": {34.0: "thirty-four"}},
            "var_measure_levels": {"age": "scale", "name": "nominal"},
            "var_roles": {"age": "target", "name": "input"},
            "var_alignments": {"age": "right", "name": "left"},
            "var_column_widths": {"age": 12, "name": 16},
            "var_formats": {"age": "F8.0", "name": "A12"},
            "var_missing_values": {"age": {"values": [34.0]}},
            "var_attributes": {"age": {"Origin": "fixture"}},
            "file_attributes": {"Source": "test"},
            "case_weight_var": "age",
        },
    )

    imported = openstatspec.import_sav(source, database_url=database, dataset_id="tiny")
    assert imported["case_count"] == 2
    assert imported["data_table"] == "data_tiny"
    assert {diagnostic.code for diagnostic in imported.diagnostics} == set(_REQUIRED_ENGINE_LOSS)
    assert openstatspec.validate(database_url=database, dataset_id="tiny")["valid"] is True

    connection = sqlite3.connect(database_path)
    table_names = [row[0] for row in connection.execute("select name from sqlite_master where type = 'table' order by name")]
    assert table_names == ["data_tiny", "dataset_catalog", "document_catalog", "fidelity_event_catalog", "missing_rule_catalog", "multiple_response_set_catalog", "operation_catalog", "source_extension_catalog", "value_label_catalog", "variable_catalog"]
    assert connection.execute("select __case_ordinal, age, name from data_tiny order by __case_ordinal").fetchall() == [(1, 34.0, "Ada"), (2, None, "")]
    assert connection.execute("select source_encoding, file_attributes, case_weight_variable from dataset_catalog").fetchone() == ("UTF-8", json.dumps({"Source": "test"}), "age")
    assert connection.execute("select source_sha256 from dataset_catalog").fetchone() == (hashlib.sha256(source.read_bytes()).hexdigest(),)
    assert connection.execute("select role, alignment, display_width, attributes from variable_catalog where source_name = 'age'").fetchone() == ("target", "right", 12, json.dumps({"Origin": "fixture"}))

    with pytest.raises(UnsupportedOperationError, match="file-label-and-documents-unobservable"):
        openstatspec.export_sav(database_url=database, dataset_id="tiny", destination=exported)
    openstatspec.export_sav(database_url=database, dataset_id="tiny", destination=exported, allow_loss=_REQUIRED_ENGINE_LOSS)
    frame, meta = pyspssio.read_sav(str(exported), convert_datetimes=False, include_user_missing=True)
    assert frame["age"].iloc[0] == 34.0
    assert pd.isna(frame["age"].iloc[1])
    assert frame["name"].tolist() == ["Ada", ""]
    assert meta["var_labels"] == {"age": "Age", "name": "Name"}
    assert meta["var_value_labels"] == {"age": {34.0: "thirty-four"}}
    assert meta["var_formats"] == {"age": "F8", "name": "A12"}
    assert meta["var_measure_levels"] == {"age": "scale", "name": "nominal"}
    assert meta["var_roles"] == {"age": "target", "name": "input"}
    assert meta["var_alignments"] == {"age": "right", "name": "left"}
    assert meta["var_column_widths"] == {"age": 12, "name": 16}
    assert meta["var_attributes"] == {"age": {"Origin": "fixture"}}
    assert meta["file_attributes"] == {"Source": "test"}
    assert meta["case_weight_var"] == "age"


def test_supported_pyspssio_metadata_round_trips_through_sqlite_for_sav_and_zsav(tmp_path, suffix: str = ".sav") -> None:
    source = tmp_path / f"supported{suffix}"
    database_path = tmp_path / f"supported-{suffix[1:]}.sqlite"
    destination = tmp_path / f"supported-roundtrip{suffix}"
    expected = write_supported_semantics_fixture(source)

    result = openstatspec.import_sav(source, database_url=f"sqlite:///{database_path}", dataset_id=f"supported-{suffix[1:]}")
    assert result["case_count"] == 4
    assert openstatspec.validate(database_url=f"sqlite:///{database_path}", dataset_id=f"supported-{suffix[1:]}")["valid"] is True
    connection = sqlite3.connect(database_path)
    assert connection.execute(f"select comment from data_supported_{suffix[1:]} order by __case_ordinal").fetchone() == (expected["long_text"],)
    openstatspec.export_sav(database_url=f"sqlite:///{database_path}", dataset_id=f"supported-{suffix[1:]}", destination=destination, allow_loss=_REQUIRED_ENGINE_LOSS)
    assert compare_sav_semantics(source, destination) == {"equivalent": True, "differences": []}


@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_pyspssio_preserves_supported_metadata_for_both_formats(tmp_path, suffix: str) -> None:
    test_supported_pyspssio_metadata_round_trips_through_sqlite_for_sav_and_zsav(tmp_path, suffix)


def test_import_rejects_physical_table_name_collision_without_partial_catalog(tmp_path) -> None:
    source = tmp_path / "fixture.sav"
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    openstatspec.import_sav(source, database_url=database, dataset_id="wave-1")
    with pytest.raises(ValueError, match="collides"):
        openstatspec.import_sav(source, database_url=database, dataset_id="wave 1")
    connection = sqlite3.connect(database_path)
    assert connection.execute("select dataset_id from dataset_catalog").fetchall() == [("wave-1",)]