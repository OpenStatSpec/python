import sqlite3
from dataclasses import replace

import pytest
from sqlalchemy.exc import IntegrityError

import openstatspec.sql.wide as wide
from openstatspec.sql.profiles import MYSQL, SQLITE
from openstatspec.sql.wide import create_wide_dataset


def test_failed_row_insert_leaves_no_catalog_or_data_table(tmp_path) -> None:
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
    wide.initialize_wide_catalog(database_url=database)
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]

    with pytest.raises(IntegrityError):
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


def test_failed_preflight_persists_operation_without_creating_dataset(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "preflight.sqlite"
    database = f"sqlite:///{database_path}"
    wide.initialize_wide_catalog(database_url=database)
    too_many = SQLITE.max_source_variables + 1
    monkeypatch.setattr(
        wide, "effective_profile", lambda _url, **_kwargs: (SQLITE, {}),
    )

    with pytest.raises(Exception, match="Target capability exceeded"):
        create_wide_dataset(
            database_url=database, dataset_id="too-wide", source_name="too-wide.sav",
            source_format="SAV", rows=(), variables=[{}] * too_many,
        )

    connection = sqlite3.connect(database_path)
    assert connection.execute("select count(*) from dataset_catalog").fetchone() == (0,)
    assert "data_too_wide" not in {
        row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")
    }
    assert connection.execute(
        "select direction, status, dataset_id from operation_catalog"
    ).fetchall() == [("import", "failed", None)]
    direction, severity, code, details = connection.execute(
        "select direction, severity, code, details from fidelity_event_catalog"
    ).fetchone()
    assert (direction, severity, code) == ("import", "error", "target_capability_exceeded")
    assert f'"variable_count": {too_many}' in details


def test_identifier_mapping_preflight_records_failure_before_dataset_creation(tmp_path) -> None:
    database_path = tmp_path / "identifier.sqlite"
    database = f"sqlite:///{database_path}"
    wide.initialize_wide_catalog(database_url=database)
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
    assert connection.execute("select count(*) from dataset_catalog").fetchone() == (0,)
    assert connection.execute("select dataset_id, status from operation_catalog").fetchall() == [(None, "failed")]
    details = connection.execute("select details from fidelity_event_catalog").fetchone()[0]
    assert '"reason": "physical_identifier_mapping_invalid"' in details


def test_declared_string_width_preflight_is_atomic_and_diagnostic(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "string-width.sqlite"
    database = f"sqlite:///{database_path}"
    wide.initialize_wide_catalog(database_url=database)
    monkeypatch.setattr(
        wide, "effective_profile",
        lambda _url, **_kwargs: (replace(SQLITE, max_text_value_bytes=3), {}),
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
    assert connection.execute("select count(*) from dataset_catalog").fetchone() == (0,)
    assert "data_too_wide_string" not in {
        row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")
    }
    assert connection.execute(
        "select dataset_id, status from operation_catalog"
    ).fetchall() == [(None, "failed")]
    details = connection.execute("select details from fidelity_event_catalog").fetchone()[0]
    assert '"reason": "declared_string_width_limit"' in details
    assert '"string_width": 4' in details

def test_nonatomic_failure_after_normative_write_cleans_both_catalogs_and_data(
    tmp_path, monkeypatch,
) -> None:
    database_path = tmp_path / "nonatomic-cleanup.sqlite"
    database = f"sqlite:///{database_path}"
    wide.initialize_wide_catalog(database_url=database)
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]
    monkeypatch.setattr(
        wide, "effective_profile",
        lambda _url, **_kwargs: (replace(MYSQL, name="mysql"), {}),
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
    if "dataset_catalog" in existing_tables:
        assert connection.execute(
            "select count(*) from dataset_catalog where dataset_id = 'cleanup'"
        ).fetchone() == (0,)
    if "variable_catalog" in existing_tables:
        assert connection.execute(
            "select count(*) from variable_catalog where dataset_id = 'cleanup'"
        ).fetchone() == (0,)
    assert connection.execute(
        "select count(*) from dataset where dataset_name = 'cleanup'"
    ).fetchone() == (0,)
    assert connection.execute(
        "select count(*) from variable"
    ).fetchone() == (0,)
    assert connection.execute(
        "select direction, status, dataset_id from operation_catalog"
    ).fetchall() == [("import", "failed", None)]
    assert connection.execute(
        "select direction, severity, code, dataset_id from fidelity_event_catalog"
    ).fetchall() == [("import", "error", "import_failed", None)]
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

    with pytest.raises(RuntimeError, match="foreign"):
        wide.initialize_wide_catalog(database_url=database)

    assert connection.execute(
        "select name, sql from sqlite_master where type = 'table' order by name"
    ).fetchall() == before_schema
    assert connection.execute("select value from foreign_data").fetchall() == [("keep",)]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_preflight_creates_no_dataset_or_physical_table(
    tmp_path, value,
) -> None:
    database_path = tmp_path / "dolt-nonfinite.sqlite"
    database = f"sqlite:///{database_path}"
    wide.initialize_wide_catalog(database_url=database)
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


def test_limited_width_failure_preserves_initialized_identity_and_one_audit(
    tmp_path, monkeypatch,
) -> None:
    database_path = tmp_path / "dolt-preflight.sqlite"
    database = f"sqlite:///{database_path}"
    wide.initialize_wide_catalog(database_url=database)
    monkeypatch.setattr(
        wide, "effective_profile",
        lambda _url, **_kwargs: (replace(SQLITE, max_source_variables=1), {}),
    )

    with pytest.raises(Exception, match="Target capability exceeded"):
        create_wide_dataset(
            database_url=database, dataset_id="too-wide-dolt",
            source_name="too-wide-dolt.sav", source_format="SAV",
            rows=(), variables=[{}, {}],
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
    assert connection.execute(
        "select dataset_id, status from operation_catalog"
    ).fetchall() == [(None, "failed")]
    assert connection.execute(
        "select dataset_id, code from fidelity_event_catalog"
    ).fetchall() == [(None, "target_capability_exceeded")]


@pytest.mark.parametrize("failure_point", ["mirror_completion", "normative_completion"])
def test_nonatomic_failure_during_final_completion_still_cleans_dataset(
    tmp_path, monkeypatch, failure_point,
) -> None:
    database_path = tmp_path / f"{failure_point}.sqlite"
    database = f"sqlite:///{database_path}"
    wide.initialize_wide_catalog(database_url=database)
    dataset_id = f"cleanup-{failure_point}"
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8, "label": "",
        "format": "A8", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]
    monkeypatch.setattr(
        wide, "effective_profile",
        lambda _url, **_kwargs: (replace(MYSQL, name="mysql"), {}),
    )
    triggered = False
    if failure_point == "mirror_completion":
        real_update = wide.update

        def fail_first_operation_update(table):
            nonlocal triggered
            if table.name == "operation_catalog" and not triggered:
                triggered = True
                raise RuntimeError("fault after data insert")
            return real_update(table)

        monkeypatch.setattr(wide, "update", fail_first_operation_update)
    else:
        real_finish = wide.finish_normative_operation

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
            database_url=database, dataset_id=dataset_id,
            source_name=f"{dataset_id}.sav", source_format="SAV",
            rows=[{"name": "ok"}], variables=variables,
        )

    connection = sqlite3.connect(database_path)
    assert triggered is True
    assert f"data_{dataset_id}" not in {
        row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    existing_tables = {
        row[0] for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    if "dataset_catalog" in existing_tables:
        assert connection.execute(
            "select count(*) from dataset_catalog where dataset_id = ?", (dataset_id,)
        ).fetchone() == (0,)
    assert connection.execute(
        "select count(*) from dataset where dataset_name = ?", (dataset_id,)
    ).fetchone() == (0,)
    assert connection.execute(
        "select status, dataset_id from operation_catalog"
    ).fetchall() == [("failed", None)]
    assert connection.execute(
        "select status from operation"
    ).fetchall() == [("failed",)]
    assert connection.execute(
        "select code, dataset_id from fidelity_event_catalog"
    ).fetchall() == [("import_failed", None)]
