import json
import sqlite3
from uuid import UUID

import pytest

from openstatspec.sql.wide import create_wide_dataset, initialize_wide_catalog, record_export_operation
from openstatspec.sql.normative import catalog as normative_catalog, create as create_normative
from sqlalchemy import MetaData, create_engine
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.schema import CreateTable


NORMATIVE_TABLES = {
    "catalog_identity", "dataset", "operation", "variable",
    "dataset_weight_variable", "value_label_set", "value_label",
    "variable_value_label_set", "missing_rule", "dataset_attribute",
    "variable_attribute", "document", "variable_set", "variable_set_member",
    "multiple_response_set", "multiple_response_member", "fidelity_event",
}


def test_catalog_identity_key_is_not_auto_incremented_in_mysql_family():
    tables = normative_catalog(MetaData())
    ddl = str(CreateTable(tables.catalog_identity).compile(dialect=mysql_dialect()))

    assert "AUTO_INCREMENT" not in ddl
    assert "CHECK (catalog_identity_key = 1)" in ddl


def test_catalog_creation_refuses_unowned_logical_relation(tmp_path):
    database_path = tmp_path / "occupied.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute("create table dataset (foreign_id integer primary key)")
    connection.commit()
    connection.close()

    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as sql_connection:
        with pytest.raises(RuntimeError, match="unowned OpenStatSpec relation"):
            create_normative(sql_connection, normative_catalog(MetaData()))
    check = sqlite3.connect(database_path)
    assert check.execute(
        "select count(*) from sqlite_master where type = 'table' and name = 'catalog_identity'"
    ).fetchone() == (0,)


def variables():
    return [
        {
            "ordinal": 1, "source_name": "answer", "physical_name": "answer",
            "storage_kind": "numeric", "string_width": None, "label": "Answer",
            "format": "F8.0", "print_format": "[5, 8, 0]",
            "write_format": "[5, 12, 2]", "measure": "nominal",
            "role": "input", "alignment": "right", "display_width": 12,
            "attributes": "{}", "compat_name": None,
            "value_labels": json.dumps({"1.0": "Yes", "2.0": "No"}),
            "missing_ranges": json.dumps([{"lo": -99.0, "hi": -1.0}, 999.0]),
        },
        {
            "ordinal": 2, "source_name": "comment", "physical_name": "comment",
            "storage_kind": "string", "string_width": 40, "label": "Comment",
            "format": "A40", "print_format": "[1, 40, 0]",
            "write_format": "[1, 40, 0]", "measure": "nominal",
            "role": "target", "alignment": "left", "display_width": 40,
            "attributes": "{}", "compat_name": None,
            "value_labels": "{}", "missing_ranges": "[]",
        },
    ]


def test_import_writes_complete_normative_catalog(tmp_path):
    database_path = tmp_path / "normative.sqlite"
    database_url = f"sqlite:///{database_path}"
    initialize_wide_catalog(database_url=database_url)
    result = create_wide_dataset(
        database_url=database_url, dataset_id="wave_1",
        source_name="fixture.sav", source_format="SAV",
        rows=[{"answer": 1.0, "comment": "hello"}], variables=variables(),
        file_label="Fixture", source_encoding="UTF-8", source_sha256="abc123",
        documents=json.dumps(["first document"]),
        file_attributes=json.dumps({"Source": ["one", "two"]}),
        file_attribute_values={"Source": ["one", "two"]},
        variable_attribute_values={"answer": {"Origin": "fixture"}},
        multiple_response_sets=json.dumps({
            "$answers": {"counted_value": 1.0, "variable_list": ["answer"]},
        }),
        source_extensions={"spss.variable_sets": {"Analysis": ["answer", "comment"]}},
        case_weight_variable="answer",
        fidelity_events=[{
            "code": "fixture-warning", "detail": "Synthetic diagnostic",
            "source_item": "answer", "details": {"test": True},
        }],
    )
    connection = sqlite3.connect(database_path)
    table_names = {
        row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    assert NORMATIVE_TABLES <= table_names
    assert connection.execute(
        "select catalog_identity_key, contract_id, schema_version from catalog_identity"
    ).fetchall() == [(1, "openstatspec-strict-wide-table-v1", 1)]

    dataset = connection.execute(
        "select dataset_id, spec_version, source_format, physical_table_name, "
        "dataset_name, dataset_label, source_encoding, source_hash, source_case_count, imported_at "
        "from dataset"
    ).fetchone()
    UUID(dataset[0])
    assert dataset[1:-1] == (
        "1.0", "SAV", "data_wave_1", "wave_1", "Fixture", "UTF-8", "abc123", 1,
    )
    assert dataset[-1]

    variable_rows = connection.execute(
        "select source_ordinal, source_name, physical_name, storage_kind, "
        "declared_string_width, variable_label, print_format_family, print_format_width, "
        "print_format_decimals, write_format_family, write_format_width, "
        "write_format_decimals, measurement_level, variable_role, display_width, "
        "display_alignment from variable order by source_ordinal"
    ).fetchall()
    assert variable_rows == [
        (1, "answer", "answer", "numeric", None, "Answer", "5", 8, 0, "5", 12, 2, "nominal", "input", 12, "right"),
        (2, "comment", "comment", "string", 40, "Comment", "1", 40, 0, "1", 40, 0, "nominal", "target", 40, "left"),
    ]
    assert connection.execute("select numeric_code, label from value_label order by ordinal").fetchall() == [(1.0, "Yes"), (2.0, "No")]
    assert connection.execute("select rule_kind, numeric_lower, numeric_upper, numeric_value from missing_rule order by ordinal").fetchall() == [("numeric_range", -99.0, -1.0, None), ("discrete", None, None, 999.0)]
    assert connection.execute("select attribute_name, array_ordinal, attribute_value from dataset_attribute order by array_ordinal").fetchall() == [("Source", 1, "one"), ("Source", 2, "two")]
    assert connection.execute("select attribute_name, array_ordinal, attribute_value from variable_attribute").fetchall() == [("Origin", 1, "fixture")]
    assert connection.execute("select source_ordinal, document_text from document").fetchall() == [(1, "first document")]
    assert connection.execute("select source_ordinal, set_name from variable_set").fetchall() == [(1, "Analysis")]
    assert connection.execute("select source_ordinal from variable_set_member order by source_ordinal").fetchall() == [(1,), (2,)]
    assert connection.execute(
        "select source_ordinal, set_name, set_kind, counted_value_kind, "
        "counted_numeric_value, counted_string_value, category_label_behavior, "
        "label_source from multiple_response_set"
    ).fetchall() == [(1, "$answers", "MD", "numeric", 1.0, None, "variable_labels", "set_label")]
    assert connection.execute("select source_ordinal from multiple_response_member").fetchall() == [(1,)]
    assert connection.execute(
        "select v.source_name from dataset_weight_variable w "
        "join variable v on v.variable_id = w.variable_id"
    ).fetchall() == [("answer",)]
    assert connection.execute(
        "select operation_kind, status, source_format, started_at is not null, completed_at is not null "
        "from operation where operation_id = ?", (result["operation_id"],),
    ).fetchone() == ("import", "succeeded", "SAV", 1, 1)
    event = connection.execute(
        "select dataset_id, direction, severity, event_code, source_item, detail_json, created_at "
        "from fidelity_event where operation_id = ?", (result["operation_id"],),
    ).fetchone()
    assert event[:5] == (dataset[0], "import", "warning", "fixture-warning", "answer")
    assert json.loads(event[5]) == {"message": "Synthetic diagnostic", "test": True}
    assert event[6]

    export_id = record_export_operation(
        database_url=database_url, dataset_id="wave_1", destination="out.sav",
        allowed_fidelity_events=[{
            "code": "export-warning", "detail": "Accepted", "source_item": "comment",
        }],
    )
    assert connection.execute(
        "select operation_kind, status from operation where operation_id = ?", (export_id,),
    ).fetchone() == ("export", "succeeded")
    export_event = connection.execute(
        "select dataset_id, direction, event_code, source_item, detail_json "
        "from fidelity_event where operation_id = ?", (export_id,),
    ).fetchone()
    assert export_event[:4] == (dataset[0], "export", "export-warning", "comment")
    assert json.loads(export_event[4])["accepted_by_user"] is True
