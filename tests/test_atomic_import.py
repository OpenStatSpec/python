import sqlite3
from dataclasses import replace

import pytest
from sqlalchemy.exc import IntegrityError

import openstatspec.sql.wide as wide
from openstatspec.sql.profiles import SQLITE
from openstatspec.sql.wide import create_wide_dataset


def test_failed_row_insert_leaves_no_catalog_or_data_table(tmp_path) -> None:
    database_path = tmp_path / "dataset.sqlite"
    database = f"sqlite:///{database_path}"
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
    assert "dataset_catalog" in tables
    assert connection.execute("select count(*) from dataset_catalog").fetchone() == (0,)
    assert "variable_catalog" in tables
    assert connection.execute("select count(*) from variable_catalog").fetchone() == (0,)


def test_failed_preflight_persists_operation_without_creating_dataset(tmp_path) -> None:
    database_path = tmp_path / "preflight.sqlite"
    database = f"sqlite:///{database_path}"

    with pytest.raises(Exception, match="Target capability exceeded"):
        create_wide_dataset(
            database_url=database, dataset_id="too-wide", source_name="too-wide.sav",
            source_format="SAV", rows=(), variables=[{}] * 2_001,
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
    assert connection.execute("select count(*) from dataset_catalog").fetchone() == (0,)
    assert connection.execute("select dataset_id, status from operation_catalog").fetchall() == [(None, "failed")]
    details = connection.execute("select details from fidelity_event_catalog").fetchone()[0]
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
