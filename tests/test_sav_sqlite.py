import hashlib
import json
import sqlite3

import pandas as pd
import pyspssio
import pytest

import openstatspec
import openstatspec.spss.sav as sav_module
from conformance import compare_sav_semantics, write_supported_semantics_fixture
from openstatspec.core import UnsupportedOperationError

_REQUIRED_ENGINE_LOSS = []

_COMPAT_NAME_LOSS = _REQUIRED_ENGINE_LOSS

def test_pyspssio_round_trip_uses_one_wide_table_and_catalog(tmp_path) -> None:
    source = tmp_path / "tiny.sav"
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    openstatspec.initialize_catalog(database_url=database)
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
    assert {
        "attribute_catalog", "data_tiny", "dataset_catalog", "document_catalog",
        "fidelity_event_catalog", "missing_rule_catalog",
        "multiple_response_set_catalog", "operation_catalog",
        "source_extension_catalog", "value_label_catalog", "variable_catalog",
        "dataset", "operation", "variable", "value_label_set", "value_label",
        "variable_value_label_set", "missing_rule", "dataset_attribute",
        "variable_attribute", "document", "variable_set", "variable_set_member",
        "multiple_response_set", "multiple_response_member", "fidelity_event",
    } <= set(table_names)
    assert connection.execute("select __case_ordinal, age, name from data_tiny order by __case_ordinal").fetchall() == [(1, 34.0, "Ada"), (2, None, "")]
    assert connection.execute("select source_encoding, file_attributes, case_weight_variable from dataset_catalog").fetchone() == ("UTF-8", json.dumps({"Source": "test"}), "age")
    assert connection.execute("select source_sha256 from dataset_catalog").fetchone() == (hashlib.sha256(source.read_bytes()).hexdigest(),)
    assert connection.execute("select role, alignment, display_width, attributes from variable_catalog where source_name = 'age'").fetchone() == ("target", "right", 12, json.dumps({"Origin": "fixture"}))

    openstatspec.export_sav(database_url=database, dataset_id="tiny", destination=exported)
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



def test_file_label_round_trips_through_sqlite_and_export(tmp_path) -> None:
    source = tmp_path / "label-source.sav"
    database_path = tmp_path / "label.sqlite"
    database = "sqlite:///{}".format(database_path)
    openstatspec.initialize_catalog(database_url=database)
    destination = tmp_path / "label-destination.sav"
    label = "OpenStatSpec label fixture"
    pyspssio.write_sav(
        str(source), pd.DataFrame({"answer": [1.0]}), metadata={"file_label": label}
    )

    openstatspec.import_sav(source, database_url=database, dataset_id="label")
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select file_label from dataset_catalog where dataset_id = ?", ("label",)
    ).fetchone() == (label,)

    openstatspec.export_sav(
        database_url=database, dataset_id="label", destination=destination,
        allow_loss=_REQUIRED_ENGINE_LOSS,
    )
    assert pyspssio.read_metadata(str(destination))["file_label"] == label

def test_supported_pyspssio_metadata_round_trips_through_sqlite_for_sav_and_zsav(tmp_path, suffix: str = ".sav") -> None:
    source = tmp_path / f"supported{suffix}"
    database_path = tmp_path / f"supported-{suffix[1:]}.sqlite"
    destination = tmp_path / f"supported-roundtrip{suffix}"
    expected = write_supported_semantics_fixture(source)
    database = f"sqlite:///{database_path}"
    openstatspec.initialize_catalog(database_url=database)

    result = openstatspec.import_sav(source, database_url=database, dataset_id=f"supported-{suffix[1:]}")
    assert result["case_count"] == 4
    assert openstatspec.validate(database_url=database, dataset_id=f"supported-{suffix[1:]}")["valid"] is True
    connection = sqlite3.connect(database_path)
    assert connection.execute(f"select comment from data_supported_{suffix[1:]} order by __case_ordinal").fetchone() == (expected["long_text"],)
    openstatspec.export_sav(database_url=database, dataset_id=f"supported-{suffix[1:]}", destination=destination, allow_loss=_COMPAT_NAME_LOSS)
    assert compare_sav_semantics(source, destination) == {"equivalent": True, "differences": []}

@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_pyspssio_preserves_supported_metadata_for_both_formats(tmp_path, suffix: str) -> None:
    test_supported_pyspssio_metadata_round_trips_through_sqlite_for_sav_and_zsav(tmp_path, suffix)

def test_import_rejects_physical_table_name_collision_without_partial_catalog(tmp_path) -> None:
    source = tmp_path / "fixture.sav"
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    openstatspec.initialize_catalog(database_url=database)
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    openstatspec.import_sav(source, database_url=database, dataset_id="wave-1")
    with pytest.raises(ValueError, match="collides"):
        openstatspec.import_sav(source, database_url=database, dataset_id="wave 1")
    connection = sqlite3.connect(database_path)
    assert connection.execute("select dataset_id from dataset_catalog").fetchall() == [("wave-1",)]
@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_raw_dictionary_bridge_preserves_distinct_formats_sets_and_attribute_arrays(tmp_path, suffix: str) -> None:
    """The raw IBM I/O path, rather than write_sav, is the fidelity proof."""
    from openstatspec.spss.dictionary import (
        attribute_values,
        file_attribute_pairs,
        format_tuples,
        set_file_attribute_pairs,
        set_format_tuples,
        set_variable_attribute_pairs,
        variable_attribute_pairs,
    )

    source = tmp_path / f"raw-source{suffix}"
    destination = tmp_path / f"raw-destination{suffix}"
    database = f"sqlite:///{tmp_path / f'raw-{suffix[1:]}.sqlite'}"
    openstatspec.initialize_catalog(database_url=database)
    with pyspssio.Writer(str(source), mode="w") as writer:
        writer.compression = 2 if suffix == ".zsav" else 1
        writer._add_var("answer", 0)  # pylint: disable=protected-access
        writer._add_var("comment", 12)  # pylint: disable=protected-access
        set_format_tuples(
            writer, name="answer", print_format=(5, 8, 1), write_format=(3, 12, 3),
        )
        set_format_tuples(
            writer, name="comment", print_format=(1, 12, 0), write_format=(1, 12, 0),
        )
        set_file_attribute_pairs(writer, [("Array[01]", "one"), ("Array[02]", "two")])
        set_variable_attribute_pairs(
            writer, "answer", [("Array[1]", "red"), ("Array[2]", "blue")],
        )
        writer.var_sets = {"Analysis": ["answer", "comment"]}
        writer.commit_header()
        writer.write_data(pd.DataFrame({"answer": [1.0], "comment": ["yes"]}))

    openstatspec.import_sav(source, database_url=database, dataset_id="raw")
    connection = sqlite3.connect(tmp_path / f"raw-{suffix[1:]}.sqlite")
    assert connection.execute(
        "select print_format, write_format from variable_catalog "
        "where dataset_id = 'raw' and source_name = 'answer'"
    ).fetchone() == ("[5, 8, 1]", "[3, 12, 3]")
    assert connection.execute(
        "select payload from source_extension_catalog "
        "where dataset_id = 'raw' and extension_key = 'spss.variable_sets'"
    ).fetchone() == (json.dumps({"Analysis": ["answer", "comment"]}),)
    openstatspec.export_sav(
        database_url=database, dataset_id="raw", destination=destination,
        allow_loss=_REQUIRED_ENGINE_LOSS,
    )
    with pyspssio.Reader(str(destination), mode="r") as reader:
        print_formats, write_formats = format_tuples(reader)
        assert print_formats["answer"] == (5, 8, 1)
        assert write_formats["answer"] == (3, 12, 3)
        assert reader.var_sets == {"Analysis": ["answer", "comment"]}
        assert attribute_values(file_attribute_pairs(reader)) == {"Array": ["one", "two"]}
        assert attribute_values(variable_attribute_pairs(reader, "answer")) == {
            "Array": ["red", "blue"],
        }


@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_very_long_string_round_trips_through_sqlite_and_export(tmp_path, suffix: str) -> None:
    payload = "ü" * 170
    payload_width = len(payload.encode("utf-8"))
    source = tmp_path / f"long-source{suffix}"
    destination = tmp_path / f"long-destination{suffix}"
    database_path = tmp_path / f"long-{suffix[1:]}.sqlite"
    database = f"sqlite:///{database_path}"
    openstatspec.initialize_catalog(database_url=database)
    pyspssio.write_sav(
        str(source), pd.DataFrame({"comment": [payload, "short"]}),
    )
    assert pyspssio.read_metadata(str(source))["var_types"]["comment"] == payload_width

    openstatspec.import_sav(source, database_url=database, dataset_id="long")
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select string_width from variable_catalog where dataset_id = ? and source_name = ?",
        ("long", "comment"),
    ).fetchone() == (payload_width,)

    openstatspec.export_sav(
        database_url=database, dataset_id="long", destination=destination,
        allow_loss=_REQUIRED_ENGINE_LOSS,
    )
    frame, metadata = pyspssio.read_sav(str(destination), convert_datetimes=False)
    assert metadata["var_types"]["comment"] == payload_width
    assert frame["comment"].tolist() == [payload, "short"]

def test_export_recovery_preserves_dangling_destination_symlink(tmp_path) -> None:
    missing_target = tmp_path / "missing-target.sav"
    destination = tmp_path / "destination.sav"
    backup = tmp_path / "destination.previous"
    destination.symlink_to(missing_target)

    assert sav_module._path_entry_exists(destination) is True
    destination.replace(backup)
    destination.write_bytes(b"staged export")
    sav_module._restore_export_destination(
        destination=destination,
        backup=backup,
        had_previous=True,
    )

    assert destination.is_symlink()
    assert destination.readlink() == missing_target

def test_successful_export_cleanup_removes_dangling_symlink_backup(
    tmp_path,
) -> None:
    missing_target = tmp_path / "missing-original.sav"
    backup = tmp_path / ".destination.previous"
    backup.symlink_to(missing_target)

    assert sav_module._path_entry_exists(backup) is True
    if sav_module._path_entry_exists(backup):
        backup.unlink()

    assert sav_module._path_entry_exists(backup) is False
