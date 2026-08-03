from decimal import Decimal
import sqlite3

import pytest
from sqlalchemy import Table

import openstatspec
import openstatspec.sql.wide as wide
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.profiles import MYSQL, TargetCapabilityExceededError, preflight
from openstatspec.sql.wide import (
    ImportRecoveryError,
    _bounded_batches,
    create_wide_dataset,
    read_wide_dataset,
    record_export_operation,
    validate_wide_dataset,
)


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
                "select name from sqlite_master where type = 'table'"
            )
        }
    finally:
        connection.close()


def test_import_requires_explicit_catalog_and_creates_no_relations(tmp_path):
    path = tmp_path / "absent.sqlite"
    database = f"sqlite:///{path}"

    with pytest.raises(UnsupportedOperationError, match="catalog is absent"):
        create_wide_dataset(
            database_url=database,
            dataset_id="sample",
            source_name="fixture.sav",
            source_format="SAV",
            rows=[{"name": "ok"}],
            variables=_variables(),
        )

    assert _table_names(path) == set()


@pytest.mark.parametrize(
    "operation",
    [
        lambda database: read_wide_dataset(database_url=database, dataset_id="missing"),
        lambda database: validate_wide_dataset(database_url=database, dataset_id="missing"),
        lambda database: record_export_operation(
            database_url=database,
            dataset_id="missing",
            destination="output.sav",
            allowed_fidelity_events=(),
        ),
    ],
)
def test_read_validate_and_export_require_catalog_without_mutation(tmp_path, operation):
    path = tmp_path / "absent.sqlite"
    database = f"sqlite:///{path}"

    with pytest.raises(UnsupportedOperationError, match="catalog is absent"):
        operation(database)

    assert _table_names(path) == set()


@pytest.mark.parametrize(
    "foreign_sql",
    [
        "create table foreign_relation (value integer)",
        "create table view_source (value integer); create view foreign_view as select value from view_source",
    ],
)
def test_initializer_rejects_foreign_tables_and_views_without_modification(
    tmp_path, foreign_sql,
):
    path = tmp_path / "foreign.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(foreign_sql)
    before = connection.execute(
        "select type, name, sql from sqlite_master "
        "where name not like 'sqlite_%' order by type, name"
    ).fetchall()
    connection.close()

    with pytest.raises(UnsupportedOperationError, match="catalog is foreign"):
        openstatspec.initialize_catalog(database_url=f"sqlite:///{path}")

    connection = sqlite3.connect(path)
    after = connection.execute(
        "select type, name, sql from sqlite_master "
        "where name not like 'sqlite_%' order by type, name"
    ).fetchall()
    connection.close()
    assert after == before


def test_initializer_compensates_partial_catalog_install(tmp_path, monkeypatch):
    path = tmp_path / "partial.sqlite"

    def fail_migration(*_args, **_kwargs):
        raise RuntimeError("injected catalog migration failure")

    monkeypatch.setattr(wide, "_migrate_catalog_columns", fail_migration)
    with pytest.raises(RuntimeError, match="injected catalog migration failure"):
        openstatspec.initialize_catalog(database_url=f"sqlite:///{path}")

    assert _table_names(path) == set()


def test_post_ddl_failure_removes_dataset_state_and_persists_null_dataset_audit(
    tmp_path, monkeypatch,
):
    path = tmp_path / "runtime.sqlite"
    database = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=database)
    original = wide.store_normative_dataset

    def store_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected normative failure")

    monkeypatch.setattr(wide, "store_normative_dataset", store_then_fail)
    with pytest.raises(RuntimeError, match="injected normative failure"):
        create_wide_dataset(
            database_url=database,
            dataset_id="sample",
            source_name="fixture.sav",
            source_format="SAV",
            rows=[{"name": "ok"}],
            variables=_variables(),
        )

    assert "data_sample" not in _table_names(path)
    connection = sqlite3.connect(path)
    assert connection.execute("select count(*) from dataset_catalog").fetchone() == (0,)
    assert connection.execute("select count(*) from dataset").fetchone() == (0,)
    assert connection.execute(
        "select status, dataset_id from operation_catalog"
    ).fetchall() == [("failed", None)]
    assert connection.execute(
        "select code, dataset_id from fidelity_event_catalog"
    ).fetchall() == [("import_failed", None)]
    assert connection.execute(
        "select status from operation"
    ).fetchall() == [("failed",)]
    assert connection.execute(
        "select event_code, dataset_id from fidelity_event"
    ).fetchall() == [("import_failed", None)]
    connection.close()


def test_cleanup_failure_has_machine_readable_error(tmp_path, monkeypatch):
    path = tmp_path / "cleanup.sqlite"
    database = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=database)

    def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("injected cleanup failure")

    def fail_mutation(*_args, **_kwargs):
        raise RuntimeError("injected mutation failure")

    # Exercise the non-transactional MySQL-wire compensation path while
    # retaining SQLite as the dependency-free test transport.
    monkeypatch.setattr(
        wide, "effective_profile", lambda _url, **_kwargs: (MYSQL, {}),
    )
    monkeypatch.setattr(wide, "_cleanup_import_state", fail_cleanup)
    monkeypatch.setattr(wide, "store_normative_dataset", fail_mutation)
    with pytest.raises(ImportRecoveryError) as error:
        create_wide_dataset(
            database_url=database,
            dataset_id="sample",
            source_name="fixture.sav",
            source_format="SAV",
            rows=[{"name": "ok"}],
            variables=_variables(),
        )

    assert error.value.code == "cleanup_failed"
    assert error.value.details["original_cause"]["type"] == "RuntimeError"
    assert error.value.details["cleanup_fault"]["type"] == "RuntimeError"
    assert error.value.details["success_forbidden"] is True
    evidence = error.value.details["deterministic_recovery_evidence"]
    assert evidence["procedure_id"] == "openstatspec.import-compensation.v1"
    assert evidence["cleanup_attempted"] is True
    assert evidence["cleanup_succeeded"] is False
    assert evidence["operation_owned_state_targeted"] is True
    assert evidence["cleanup_failed_audit_persisted"] is True
    assert evidence["terminal_reporting"] == "catalog_and_exception"
    assert len(evidence["residual_inventory_sha256"]) == 64
    connection = sqlite3.connect(path)
    assert connection.execute(
        "select status, dataset_id from operation_catalog"
    ).fetchall() == [("failed", None)]
    assert connection.execute(
        "select code, dataset_id from fidelity_event_catalog"
    ).fetchall() == [("cleanup_failed", None)]
    connection.close()


def test_database_decimal_numeric_wrappers_are_restored_to_binary64():
    variables = [{
        "physical_name": "score",
        "storage_kind": "numeric",
    }]
    rows = wide._canonicalize_database_numeric_rows(
        [
            {"score": Decimal("1.5000000000"), "name": "alpha"},
            {"score": None, "name": "missing"},
        ],
        variables,
    )

    assert rows == [
        {"score": 1.5, "name": "alpha"},
        {"score": None, "name": "missing"},
    ]
    assert isinstance(rows[0]["score"], float)


@pytest.mark.parametrize(
    "value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_database_decimal_nonfinite_wrappers_remain_rejected(value):
    variables = [{
        "ordinal": 1,
        "source_name": "score",
        "physical_name": "score",
        "storage_kind": "numeric",
    }]
    rows = wide._canonicalize_database_numeric_rows([{"score": value}], variables)

    with pytest.raises(TargetCapabilityExceededError) as error:
        preflight(MYSQL, variables, rows=rows)

    assert error.value.details["reason"] == "nonfinite_numeric_value"


def test_bounded_batches_never_exceed_statement_payload_limit():
    variables = _variables()
    rows = [
        {"name": "aaaa"},
        {"name": "bbbb"},
        {"name": "cccc"},
    ]
    single = wide.statement_payload_bytes(rows[0], variables)

    batches = list(_bounded_batches(rows, variables, single * 2))

    assert batches == [rows[:2], rows[2:]]


def test_duplicate_import_failure_preserves_existing_dataset(tmp_path):
    path = tmp_path / "duplicate.sqlite"
    database = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=database)
    create_wide_dataset(
        database_url=database,
        dataset_id="sample",
        source_name="first.sav",
        source_format="SAV",
        rows=[{"name": "first"}],
        variables=_variables(),
    )

    with pytest.raises(ValueError, match="already exists"):
        create_wide_dataset(
            database_url=database,
            dataset_id="sample",
            source_name="second.sav",
            source_format="SAV",
            rows=[{"name": "second"}],
            variables=_variables(),
        )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select dataset_id, case_count from dataset_catalog"
    ).fetchall() == [("sample", 1)]
    assert connection.execute(
        "select dataset_name, source_case_count from dataset"
    ).fetchall() == [("sample", 1)]
    assert connection.execute(
        "select name from data_sample"
    ).fetchall() == [("first",)]
    connection.close()



def test_missing_additive_column_requires_explicit_migration_without_audit_mutation(
    tmp_path,
):
    path = tmp_path / "migration-required.sqlite"
    database = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=database)
    connection = sqlite3.connect(path)
    connection.execute("alter table variable_catalog drop column compat_name")
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedOperationError, match="catalog is migration_required"):
        create_wide_dataset(
            database_url=database, dataset_id="sample",
            source_name="fixture.sav", source_format="SAV",
            rows=[{"name": "ok"}], variables=_variables(),
        )

    connection = sqlite3.connect(path)
    assert "compat_name" not in {
        row[1] for row in connection.execute("pragma table_info(variable_catalog)")
    }
    assert connection.execute("select count(*) from operation_catalog").fetchone() == (0,)
    connection.close()

    result = openstatspec.initialize_catalog(database_url=database)
    assert result["catalog"] == "verified"
    connection = sqlite3.connect(path)
    assert "compat_name" in {
        row[1] for row in connection.execute("pragma table_info(variable_catalog)")
    }
    connection.close()


def test_multiple_catalog_identities_are_ambiguous_and_never_mutated(tmp_path):
    path = tmp_path / "ambiguous.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        "create table catalog_identity ("
        "catalog_identity_key integer primary key, "
        "contract_id varchar(128) not null unique, "
        "schema_version integer not null, created_at datetime not null);"
        "insert into catalog_identity values "
        "(1, 'openstatspec-strict-wide-table-v1', 1, '2026-01-01'),"
        "(2, 'conflicting-contract', 1, '2026-01-01');"
    )
    before = connection.execute(
        "select * from catalog_identity order by catalog_identity_key"
    ).fetchall()
    connection.close()

    database = f"sqlite:///{path}"
    with pytest.raises(UnsupportedOperationError, match="catalog is ambiguous"):
        create_wide_dataset(
            database_url=database, dataset_id="sample",
            source_name="fixture.sav", source_format="SAV",
            rows=[{"name": "ok"}], variables=_variables(),
        )
    with pytest.raises(UnsupportedOperationError, match="catalog is ambiguous"):
        openstatspec.initialize_catalog(database_url=database)

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select * from catalog_identity order by catalog_identity_key"
    ).fetchall() == before
    assert _table_names(path) == {"catalog_identity"}
    connection.close()


@pytest.mark.parametrize(
    ("reflected", "expected"),
    [
        (
            "((contract_id)::text = "
            "'openstatspec-strict-wide-table-v1'::text)",
            "contract_id = 'openstatspec-strict-wide-table-v1'",
        ),
        (
            "(`contract_id` = _utf8mb4'openstatspec-strict-wide-table-v1')",
            "contract_id = 'openstatspec-strict-wide-table-v1'",
        ),
        ("((catalog_identity_key)::integer = 1)", "catalog_identity_key = 1"),
    ],
)
def test_reflected_check_normalization_removes_only_noop_dialect_syntax(
    reflected, expected,
):
    assert wide._normalized_check_sql(reflected) == expected


def test_reflected_mysql_integer_display_width_is_not_semantic():
    from types import SimpleNamespace
    from sqlalchemy.dialects import mysql

    inspector = SimpleNamespace(bind=SimpleNamespace(dialect=mysql.dialect()))
    assert wide._normalized_sql_type(
        inspector, mysql.INTEGER(display_width=11),
    ) == "INTEGER"
    assert wide._normalized_sql_type(
        inspector, mysql.BIGINT(display_width=20),
    ) == "BIGINT"
    assert wide._normalized_sql_type(
        inspector, mysql.VARCHAR(length=255),
    ) == "VARCHAR(255)"

@pytest.mark.parametrize("database_url", ["sqlite://", "sqlite:///:memory:"])
def test_catalog_initialization_rejects_ephemeral_sqlite_url(database_url):
    with pytest.raises(
        UnsupportedOperationError,
        match="requires a persistent SQLite database URL",
    ):
        openstatspec.initialize_catalog(database_url=database_url)

