import hashlib
import pytest
import json
from conformance import compare_sav_semantics
import sqlite3

import openstatspec
import pandas as pd
import pyreadstat


def test_sav_round_trip_uses_one_wide_table_and_catalog(tmp_path) -> None:
    source = tmp_path / "tiny.sav"
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    exported = tmp_path / "roundtrip.sav"
    pyreadstat.write_sav(
        pd.DataFrame({"age": [34.0, None], "name": ["Ada", ""]}),
        source,
        file_label="Tiny fixture",
        column_labels={"age": "Age", "name": "Name"},
        variable_value_labels={"age": {34.0: "thirty-four"}},
        variable_measure={"age": "scale", "name": "nominal"},
        variable_format={"age": "F8.0", "name": "A12"},
        missing_ranges={"age": [34.0]},
        note=["Fixture note"],
    )

    imported = openstatspec.import_sav(source, database_url=database, dataset_id="tiny")
    assert imported["case_count"] == 2
    assert imported["data_table"] == "data_tiny"
    assert openstatspec.validate(database_url=database, dataset_id="tiny")["valid"] is True

    connection = sqlite3.connect(database_path)
    table_names = [row[0] for row in connection.execute("select name from sqlite_master where type = \"table\" order by name")]
    assert table_names == ["data_tiny", "dataset_catalog", "document_catalog", "fidelity_event_catalog", "missing_rule_catalog", "multiple_response_set_catalog", "operation_catalog", "value_label_catalog", "variable_catalog"]
    assert connection.execute("select __case_ordinal, age, name from data_tiny order by __case_ordinal").fetchall() == [
        (1, 34.0, "Ada"), (2, None, "")
    ]
    assert connection.execute("select source_encoding, documents from dataset_catalog").fetchone() == ("UTF-8", json.dumps(["Fixture note"]))
    assert connection.execute("select source_sha256 from dataset_catalog").fetchone() == (hashlib.sha256(source.read_bytes()).hexdigest(),)
    created_at, modified_at, imported_at = connection.execute("select source_created_at, source_modified_at, imported_at from dataset_catalog").fetchone()
    assert created_at and modified_at and imported_at

    openstatspec.export_sav(database_url=database, dataset_id="tiny", destination=exported, allow_loss=["unobservable-source-dictionary-features"])
    frame, meta = pyreadstat.read_sav(exported, user_missing=True)
    assert frame["age"].iloc[0] == 34.0
    assert pd.isna(frame["age"].iloc[1])
    assert frame["name"].tolist() == ["Ada", ""]
    assert meta.column_labels == ["Age", "Name"]
    assert meta.variable_value_labels == {"age": {34.0: "thirty-four"}}
    assert meta.original_variable_types == {"age": "F8.0", "name": "A12"}
    assert meta.variable_measure == {"age": "scale", "name": "nominal"}
    zsav = tmp_path / "roundtrip.zsav"
    openstatspec.export_sav(database_url=database, dataset_id="tiny", destination=zsav, allow_loss=["unobservable-source-dictionary-features"])
    compressed_frame, _ = pyreadstat.read_sav(zsav, user_missing=True)
    assert compressed_frame["name"].tolist() == ["Ada", ""]
    assert meta.missing_ranges == {"age": [{"lo": 34.0, "hi": 34.0}]}
    assert meta.notes == ["Fixture note"]
    assert compare_sav_semantics(source, exported) == {"equivalent": True, "differences": []}



def test_validation_rejects_catalog_type_mismatch(tmp_path) -> None:
    source = tmp_path / "fixture.sav"
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    pyreadstat.write_sav(pd.DataFrame({"answer": [1.0]}), source)
    openstatspec.import_sav(source, database_url=database, dataset_id="fixture")

    connection = sqlite3.connect(database_path)
    connection.execute("update variable_catalog set storage_kind = 'string' where dataset_id = 'fixture'")
    connection.commit()
    with pytest.raises(ValueError, match="String variable"):
        openstatspec.validate(database_url=database, dataset_id="fixture")


def test_import_rejects_physical_table_name_collision_without_partial_catalog(tmp_path) -> None:
    source = tmp_path / "fixture.sav"
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    pyreadstat.write_sav(pd.DataFrame({"answer": [1.0]}), source)
    openstatspec.import_sav(source, database_url=database, dataset_id="wave-1")

    with pytest.raises(ValueError, match="collides"):
        openstatspec.import_sav(source, database_url=database, dataset_id="wave 1")

    connection = sqlite3.connect(database_path)
    assert connection.execute("select dataset_id from dataset_catalog").fetchall() == [("wave-1",)]


def test_long_utf8_strings_and_date_format_remain_source_semantic(tmp_path) -> None:
    source = tmp_path / "semantic.sav"
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    exported = tmp_path / "semantic-roundtrip.sav"
    long_text = "Õ" * 300
    pyreadstat.write_sav(
        pd.DataFrame({"comment": [long_text], "interview_date": [23123.0]}), source,
        variable_format={"comment": "A320", "interview_date": "DATE11"},
    )
    openstatspec.import_sav(source, database_url=database, dataset_id="semantic")
    connection = sqlite3.connect(database_path)
    assert connection.execute("select comment, interview_date from data_semantic").fetchone() == (long_text, 23123.0)
    assert connection.execute("select storage_kind, format from variable_catalog where source_name = 'interview_date'").fetchone() == ("numeric", "DATE11")
    openstatspec.export_sav(database_url=database, dataset_id="semantic", destination=exported, allow_loss=["unobservable-source-dictionary-features"])
    assert compare_sav_semantics(source, exported) == {"equivalent": True, "differences": []}


def test_normalized_documents_labels_and_missing_rules_drive_export(tmp_path) -> None:
    source = tmp_path / "normalized.sav"
    database_path = tmp_path / "normalized.sqlite"
    database = f"sqlite:///{database_path}"
    exported = tmp_path / "normalized-roundtrip.sav"
    pyreadstat.write_sav(
        pd.DataFrame({"numeric": [1.0, 2.0, 3.0, 4.0], "text": ["a", "b", "c", "d"]}),
        source,
        variable_value_labels={
            "numeric": {3.0: "third", 1.0: "first"},
            "text": {"b": "bee", "a": "aye"},
        },
        missing_ranges={"numeric": [1.0, {"lo": 2.0, "hi": 4.0}]},
        note=["first document", "second document"],
    )

    openstatspec.import_sav(source, database_url=database, dataset_id="normalized")
    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select ordinal, text from document_catalog where dataset_id = 'normalized' order by ordinal"
    ).fetchall() == [(1, "first document"), (2, "second document")]
    _, source_metadata = pyreadstat.read_sav(source, user_missing=True, disable_datetime_conversion=True)
    expected_label_rows = [
        (1, ordinal, "numeric", float(value), None, label)
        for ordinal, (value, label) in enumerate(source_metadata.variable_value_labels["numeric"].items(), start=1)
    ] + [
        (2, ordinal, "text", None, value, label)
        for ordinal, (value, label) in enumerate(source_metadata.variable_value_labels["text"].items(), start=1)
    ]
    assert connection.execute(
        "select variable_ordinal, ordinal, value_type, numeric_value, text_value, label "
        "from value_label_catalog where dataset_id = 'normalized' order by variable_ordinal, ordinal"
    ).fetchall() == expected_label_rows
    assert connection.execute(
        "select ordinal, kind, lower_type, lower_numeric, upper_type, upper_numeric, "
        "lower_inclusive, upper_inclusive from missing_rule_catalog "
        "where dataset_id = 'normalized' and variable_ordinal = 1 order by ordinal"
    ).fetchall() == [
        (1, "range", "numeric", 2.0, "numeric", 4.0, 1, 1),
        (2, "discrete", "numeric", 1.0, "numeric", 1.0, 1, 1),
    ]

    # New normalized rows are authoritative; legacy JSON remains only for old catalogs.
    connection.execute("update dataset_catalog set documents = '[]' where dataset_id = 'normalized'")
    connection.execute(
        "update variable_catalog set value_labels = '{}', missing_ranges = '[]' where dataset_id = 'normalized'"
    )
    connection.commit()
    openstatspec.export_sav(
        database_url=database, dataset_id="normalized", destination=exported,
        allow_loss=["unobservable-source-dictionary-features"],
    )
    _, metadata = pyreadstat.read_sav(exported, user_missing=True)
    assert metadata.notes == ["first document", "second document"]
    assert list(metadata.variable_value_labels["numeric"].items()) == list(source_metadata.variable_value_labels["numeric"].items())
    assert list(metadata.variable_value_labels["text"].items()) == list(source_metadata.variable_value_labels["text"].items())
    assert metadata.missing_ranges == {"numeric": [{"lo": 2.0, "hi": 4.0}, {"lo": 1.0, "hi": 1.0}]}


def test_legacy_json_metadata_remains_exportable_after_catalog_migration(tmp_path) -> None:
    source = tmp_path / "legacy.sav"
    database_path = tmp_path / "legacy.sqlite"
    database = f"sqlite:///{database_path}"
    exported = tmp_path / "legacy-roundtrip.sav"
    pyreadstat.write_sav(
        pd.DataFrame({"code": [1.0, 2.0]}), source,
        variable_value_labels={"code": {1.0: "one"}},
        missing_ranges={"code": [2.0]},
        note=["legacy document"],
    )
    openstatspec.import_sav(source, database_url=database, dataset_id="legacy")
    connection = sqlite3.connect(database_path)
    connection.execute("delete from document_catalog where dataset_id = 'legacy'")
    connection.execute("delete from value_label_catalog where dataset_id = 'legacy'")
    connection.execute("delete from missing_rule_catalog where dataset_id = 'legacy'")
    connection.commit()

    openstatspec.export_sav(
        database_url=database, dataset_id="legacy", destination=exported,
        allow_loss=["unobservable-source-dictionary-features"],
    )
    _, metadata = pyreadstat.read_sav(exported, user_missing=True)
    assert metadata.notes == ["legacy document"]
    assert metadata.variable_value_labels == {"code": {1.0: "one"}}
    assert metadata.missing_ranges == {"code": [{"lo": 2.0, "hi": 2.0}]}
