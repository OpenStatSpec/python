import sqlite3

import pytest

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql import wide
from openstatspec.sql.wide import (
    _bounded_batches,
    create_wide_dataset,
)


NORMATIVE_TABLES = {
    "catalog_identity", "dataset", "operation", "variable",
    "dataset_weight_variable", "value_label_set", "value_label",
    "variable_value_label_set", "missing_rule", "dataset_attribute",
    "variable_attribute", "document", "variable_set", "variable_set_member",
    "multiple_response_set", "multiple_response_member", "fidelity_event",
}


def _variables():
    return [{
        "ordinal": 1,
        "source_name": "name",
        "physical_name": "name",
        "storage_kind": "string",
        "string_width": 8,
        "label": "",
        "format": "A8",
        "measure": "nominal",
        "alignment": "left",
        "display_width": 8,
        "value_labels": "{}",
        "missing_ranges": "[]",
    }]


def _table_names(path):
    connection = sqlite3.connect(path)
    try:
        return {
            row[0] for row in connection.execute(
                "select name from sqlite_master "
                "where type = 'table' and name not like 'sqlite_%'"
            )
        }
    finally:
        connection.close()


def _create_dataset(database):
    return create_wide_dataset(
        database_url=database,
        dataset_id="sample",
        source_name="fixture.sav",
        source_format="SAV",
        rows=[{"name": "ok"}],
        variables=_variables(),
    )


def test_import_initializes_only_normative_catalog(tmp_path):
    path = tmp_path / "absent.sqlite"
    database = f"sqlite:///{path}"

    _create_dataset(database)
    assert _table_names(path) == NORMATIVE_TABLES | {"data_sample"}


def test_initializer_creates_only_normative_catalog_and_is_idempotent(tmp_path):
    path = tmp_path / "strict.sqlite"
    database = f"sqlite:///{path}"

    initialized = openstatspec.initialize_catalog(database_url=database)
    assert initialized["profile"] == "sqlite"
    assert initialized["catalog"] == "verified"
    assert _table_names(path) == NORMATIVE_TABLES
    assert openstatspec.initialize_catalog(database_url=database)["catalog"] == "verified"
    assert _table_names(path) == NORMATIVE_TABLES


@pytest.mark.parametrize(
    "foreign_sql",
    [
        "create table foreign_relation (value integer)",
        "create view foreign_view as select 1 as value",
    ],
)
def test_initializer_rejects_foreign_relations_without_modification(
    tmp_path, foreign_sql,
):
    path = tmp_path / "foreign.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(foreign_sql)
    before = connection.execute(
        "select type, name, sql from sqlite_master "
        "where name not like 'sqlite_%' order by type, name"
    ).fetchall()
    connection.close()

    with pytest.raises(UnsupportedOperationError, match="foreign"):
        openstatspec.initialize_catalog(database_url=f"sqlite:///{path}")

    connection = sqlite3.connect(path)
    after = connection.execute(
        "select type, name, sql from sqlite_master "
        "where name not like 'sqlite_%' order by type, name"
    ).fetchall()
    connection.close()
    assert after == before


def test_initializer_rejects_obsolete_relation_until_manual_remediation(tmp_path):
    path = tmp_path / "obsolete.sqlite"
    database = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=database)
    connection = sqlite3.connect(path)
    connection.execute("create table dataset_catalog (dataset_id text primary key)")
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedOperationError, match="obsolete"):
        openstatspec.initialize_catalog(database_url=database)


def test_bounded_batches_never_exceed_statement_payload_limit():
    variables = _variables()
    rows = [{"name": "aaaa"}, {"name": "bbbb"}, {"name": "cccc"}]
    single = wide.statement_payload_bytes(rows[0], variables)

    assert list(_bounded_batches(rows, variables, single * 2)) == [
        rows[:2], rows[2:],
    ]


def test_duplicate_import_preserves_existing_dataset(tmp_path):
    path = tmp_path / "duplicate.sqlite"
    database = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=database)
    _create_dataset(database)

    with pytest.raises(ValueError, match="already exists"):
        _create_dataset(database)

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select dataset_name, source_case_count from dataset"
    ).fetchall() == [("sample", 1)]
    assert connection.execute("select name from data_sample").fetchall() == [("ok",)]
    connection.close()
