import sqlite3
from dataclasses import replace

import pytest

import openstatspec.sql.wide as wide
from openstatspec.sql.profiles import DOLT, MYSQL, SQLITE, TargetCapabilityExceededError
from openstatspec.sql.wide import create_wide_dataset


def test_invalid_string_row_leaves_no_dataset_or_data_table(tmp_path) -> None:
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]

    with pytest.raises(TargetCapabilityExceededError):
        create_wide_dataset(
            database_url=database, dataset_id="broken", source_name="fixture.sav",
            source_format="SAV", rows=[{"name": None}], variables=variables,
        )

    connection = sqlite3.connect(database_path)
    tables = [row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")]
    assert "data_broken" not in tables
    assert "catalog_identity" in tables
    assert connection.execute("select count(*) from dataset").fetchone() == (0,)
    assert connection.execute("select count(*) from variable").fetchone() == (0,)

def test_failed_create_does_not_drop_a_concurrently_created_table(
    tmp_path, monkeypatch,
) -> None:
    database_path = tmp_path / "race.sqlite"
    database = f"sqlite:///{database_path}"
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]
    real_inspect = wide.inspect
    inspected_data_table = 0

    def inspect_with_concurrent_table(bind):
        actual = real_inspect(bind)

        class Inspector:
            def has_table(self, table_name):
                nonlocal inspected_data_table
                if table_name == "data_race":
                    inspected_data_table += 1
                    return inspected_data_table > 1
                return actual.has_table(table_name)

        return Inspector()

    real_create = wide.Table.create
    real_drop = wide.Table.drop
    dropped = []

    def fail_data_table_create(table, bind, **kwargs):
        if table.name == "data_race":
            raise RuntimeError("concurrent create won")
        return real_create(table, bind, **kwargs)

    def observe_drop(table, bind, **kwargs):
        if table.name == "data_race":
            dropped.append(table.name)
        return real_drop(table, bind, **kwargs)

    monkeypatch.setattr(wide, "inspect", inspect_with_concurrent_table)
    monkeypatch.setattr(wide.Table, "create", fail_data_table_create)
    monkeypatch.setattr(wide.Table, "drop", observe_drop)

    with pytest.raises(RuntimeError, match="concurrent create won"):
        create_wide_dataset(
            database_url=database, dataset_id="race", source_name="race.sav",
            source_format="SAV", rows=[{"name": "ok"}], variables=variables,
        )

    assert inspected_data_table == 1
    assert dropped == []


def test_dataset_name_cannot_equal_an_existing_normative_uuid(tmp_path) -> None:
    database = f"sqlite:///{tmp_path / 'namespace.sqlite'}"
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]
    first = create_wide_dataset(
        database_url=database, dataset_id="first", source_name="first.sav",
        source_format="SAV", rows=[{"name": "first"}], variables=variables,
    )

    with pytest.raises(ValueError, match="already exists"):
        create_wide_dataset(
            database_url=database, dataset_id=first["dataset_id"],
            source_name="second.sav", source_format="SAV",
            rows=[{"name": "second"}], variables=variables,
        )

def test_failed_preflight_persists_operation_without_creating_dataset(tmp_path) -> None:
    database_path = tmp_path / "preflight.sqlite"
    database = f"sqlite:///{database_path}"

    with pytest.raises(Exception, match="Target capability exceeded"):
        create_wide_dataset(
            database_url=database, dataset_id="too-wide", source_name="too-wide.sav",
            source_format="SAV", rows=(), variables=[{}] * 2_001,
        )

    connection = sqlite3.connect(database_path)
    assert connection.execute("select count(*) from dataset").fetchone() == (0,)
    assert "data_too_wide" not in {
        row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")
    }
    assert connection.execute(
        "select operation_kind, status from operation"
    ).fetchall() == [("import", "failed")]
    direction, severity, code, details = connection.execute(
        "select direction, severity, event_code, detail_json from fidelity_event"
    ).fetchone()
    assert (direction, severity, code) == (
        "import", "error", "target_capability_exceeded",
    )
    assert '"variable_count": 2001' in details


def test_identifier_mapping_preflight_records_failure_before_dataset_creation(tmp_path) -> None:
    database_path = tmp_path / "identifier.sqlite"
    database = f"sqlite:///{database_path}"
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "wrong_name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]

    with pytest.raises(Exception, match="Target capability exceeded"):
        create_wide_dataset(
            database_url=database, dataset_id="identifier", source_name="identifier.sav",
            source_format="SAV", rows=[{"wrong_name": "x"}], variables=variables,
        )

    connection = sqlite3.connect(database_path)
    assert connection.execute("select count(*) from dataset").fetchone() == (0,)
    assert connection.execute(
        "select status from operation"
    ).fetchall() == [("failed",)]
    details = connection.execute(
        "select detail_json from fidelity_event"
    ).fetchone()[0]
    assert '"reason": "physical_identifier_mapping_invalid"' in details


def test_declared_string_width_preflight_is_atomic_and_diagnostic(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "string-width.sqlite"
    database = f"sqlite:///{database_path}"
    monkeypatch.setattr(
        wide, "effective_profile",
        lambda _url: (replace(SQLITE, max_text_value_bytes=3), {}),
    )
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 4, "label": "",
        "format": "A4", "measure": "nominal", "alignment": "left",
        "display_width": 4, "value_labels": "{}", "missing_ranges": "[]",
    }]

    with pytest.raises(Exception, match="Target capability exceeded"):
        create_wide_dataset(
            database_url=database, dataset_id="too-wide-string", source_name="fixture.sav",
            source_format="SAV", rows=[{"name": "aa"}], variables=variables,
        )

    connection = sqlite3.connect(database_path)
    assert connection.execute("select count(*) from dataset").fetchone() == (0,)
    assert "data_too_wide_string" not in {
        row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")
    }
    assert connection.execute(
        "select status from operation"
    ).fetchall() == [("failed",)]
    details = connection.execute(
        "select detail_json from fidelity_event"
    ).fetchone()[0]
    assert '"reason": "declared_string_width_limit"' in details
    assert '"string_width": 4' in details

def test_nonatomic_failure_after_normative_write_cleans_both_catalogs_and_data(
    tmp_path, monkeypatch,
) -> None:
    database_path = tmp_path / "nonatomic-cleanup.sqlite"
    database = f"sqlite:///{database_path}"
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]
    monkeypatch.setattr(
        wide, "effective_profile",
        lambda _url: (replace(MYSQL, name="mysql"), {}),
    )
    real_store = wide.store_normative_dataset

    def fail_after_normative_write(*args, **kwargs):
        real_store(*args, **kwargs)
        raise RuntimeError("fault after normative write")

    monkeypatch.setattr(wide, "store_normative_dataset", fail_after_normative_write)

    with pytest.raises(RuntimeError, match="fault after normative write"):
        create_wide_dataset(
            database_url=database, dataset_id="cleanup", source_name="fixture.sav",
            source_format="SAV", rows=[{"name": "ok"}], variables=variables,
        )

    connection = sqlite3.connect(database_path)
    assert "data_cleanup" not in {
        row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    existing_tables = {
        row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    assert not {name for name in existing_tables if name.endswith("_catalog")}
    assert connection.execute(
        "select count(*) from dataset where dataset_name = 'cleanup'"
    ).fetchone() == (0,)
    assert connection.execute(
        "select count(*) from variable"
    ).fetchone() == (0,)
    assert connection.execute(
        "select operation_kind, status from operation"
    ).fetchall() == [("import", "failed")]
    assert connection.execute(
        "select direction, severity, event_code, dataset_id from fidelity_event"
    ).fetchall() == [("import", "error", "import_failed", None)]

def test_occupied_foreign_namespace_fails_without_modification(tmp_path) -> None:
    database_path = tmp_path / "foreign.sqlite"
    database = f"sqlite:///{database_path}"
    connection = sqlite3.connect(database_path)
    connection.execute("create table foreign_data (value text not null)")
    connection.execute("insert into foreign_data values ('keep')")
    connection.commit()
    before_schema = connection.execute(
        "select name, sql from sqlite_master where type = 'table' order by name"
    ).fetchall()

    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]
    with pytest.raises(RuntimeError, match="occupied"):
        create_wide_dataset(
            database_url=database, dataset_id="foreign", source_name="fixture.sav",
            source_format="SAV", rows=[{"name": "ok"}], variables=variables,
        )

    assert connection.execute(
        "select name, sql from sqlite_master where type = 'table' order by name"
    ).fetchall() == before_schema
    assert connection.execute("select value from foreign_data").fetchall() == [("keep",)]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_dolt_nonfinite_preflight_creates_no_dataset_or_physical_table(
    tmp_path, monkeypatch, value,
) -> None:
    database_path = tmp_path / "dolt-nonfinite.sqlite"
    database = f"sqlite:///{database_path}"
    monkeypatch.setattr(wide, "effective_profile", lambda _url: (DOLT, {}))
    variables = [{
        "ordinal": 1, "source_name": "value", "physical_name": "value",
        "storage_kind": "numeric", "string_width": None, "label": "",
        "format": "F8.2", "measure": "scale", "alignment": "right",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]

    with pytest.raises(Exception, match="Target capability exceeded"):
        create_wide_dataset(
            database_url=database, dataset_id="nonfinite",
            source_name="nonfinite.sav", source_format="SAV",
            rows=[{"value": value}], variables=variables,
        )

    connection = sqlite3.connect(database_path)
    assert "data_nonfinite" not in {
        row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    assert connection.execute("select count(*) from dataset").fetchone() == (0,)
    assert connection.execute("select count(*) from variable").fetchone() == (0,)
    assert connection.execute(
        "select status from operation"
    ).fetchall() == [("failed",)]
    assert connection.execute(
        "select dataset_id, event_code from fidelity_event"
    ).fetchall() == [(None, "target_capability_exceeded")]


def test_empty_namespace_dolt_width_failure_initializes_identity_and_one_audit(
    tmp_path, monkeypatch,
) -> None:
    database_path = tmp_path / "dolt-preflight.sqlite"
    database = f"sqlite:///{database_path}"
    monkeypatch.setattr(wide, "effective_profile", lambda _url: (DOLT, {}))

    with pytest.raises(Exception, match="Target capability exceeded"):
        create_wide_dataset(
            database_url=database, dataset_id="too-wide-dolt",
            source_name="too-wide-dolt.sav", source_format="SAV",
            rows=(), variables=[{}] * 306,
        )

    connection = sqlite3.connect(database_path)
    assert connection.execute(
        "select contract_id, schema_version from catalog_identity"
    ).fetchall() == [("openstatspec-strict-wide-table-v1", 1)]
    assert connection.execute(
        "select operation_kind, status from operation"
    ).fetchall() == [("import", "failed")]
    assert connection.execute(
        "select dataset_id, event_code from fidelity_event"
    ).fetchall() == [(None, "target_capability_exceeded")]


def test_nonatomic_failure_during_final_completion_still_cleans_dataset(
    tmp_path, monkeypatch,
) -> None:
    database_path = tmp_path / "normative-completion.sqlite"
    database = f"sqlite:///{database_path}"
    dataset_name = "cleanup-normative-completion"
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]
    monkeypatch.setattr(
        wide, "effective_profile",
        lambda _url: (replace(MYSQL, name="mysql"), {}),
    )
    real_finish = wide.finish_normative_operation
    triggered = False

    def fail_first_normative_finish(*args, **kwargs):
        nonlocal triggered
        if not triggered:
            triggered = True
            raise RuntimeError("fault during final completion")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(
        wide, "finish_normative_operation", fail_first_normative_finish,
    )
    with pytest.raises(RuntimeError, match="fault"):
        create_wide_dataset(
            database_url=database, dataset_id=dataset_name,
            source_name=f"{dataset_name}.sav", source_format="SAV",
            rows=[{"name": "ok"}], variables=variables,
        )

    connection = sqlite3.connect(database_path)
    assert triggered is True
    tables = {
        row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    assert f"data_{dataset_name}" not in tables
    assert not {name for name in tables if name.endswith("_catalog")}
    assert connection.execute(
        "select count(*) from dataset where dataset_name = ?", (dataset_name,)
    ).fetchone() == (0,)
    assert connection.execute(
        "select status from operation"
    ).fetchall() == [("failed",)]
    assert connection.execute(
        "select event_code, dataset_id from fidelity_event"
    ).fetchall() == [("import_failed", None)]
