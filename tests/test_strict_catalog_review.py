"""Regression coverage for strict-catalog review findings."""

import json
import sqlite3

from openstatspec import export_sav

import pytest
from sqlalchemy import MetaData, create_engine

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql import wide
from openstatspec.sql.profiles import SQLITE, TargetCapabilityExceededError
from openstatspec.sql.workflow import create_workflow_catalog, workflow_catalog


def _string_variables():
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


def _numeric_variables():
    return [{
        "ordinal": 1,
        "source_name": "score",
        "physical_name": "score",
        "storage_kind": "numeric",
        "string_width": None,
        "label": "",
        "format": "F8.2",
        "measure": "scale",
        "alignment": "right",
        "display_width": 8,
        "value_labels": "{}",
        "missing_ranges": "[]",
    }]


def _create(database, dataset_id, *, operation_details=None):
    return wide.create_wide_dataset(
        database_url=database,
        dataset_id=dataset_id,
        source_name=f"{dataset_id}.sav",
        source_format="SAV",
        rows=[{"name": "ok"}],
        variables=_string_variables(),
        operation_details=operation_details,
    )


def test_existing_normative_shape_drift_is_rejected(tmp_path):
    path = tmp_path / "shape-drift.sqlite"
    database = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=database)
    connection = sqlite3.connect(path)
    connection.execute(
        "alter table variable rename column display_alignment "
        "to incompatible_display_alignment"
    )
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedOperationError, match="structurally incompatible"):
        openstatspec.initialize_catalog(database_url=database)


def test_read_and_export_reject_obsolete_catalog_relations(tmp_path):
    path = tmp_path / "obsolete-read.sqlite"
    database = f"sqlite:///{path}"
    _create(database, "sample")
    connection = sqlite3.connect(path)
    connection.execute("create table dataset_catalog (dataset_id text primary key)")
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedOperationError, match="obsolete"):
        wide.read_wide_dataset(database_url=database, dataset_id="sample")
    with pytest.raises(UnsupportedOperationError, match="obsolete"):
        export_sav(
            database_url=database,
            dataset_id="sample",
            destination=tmp_path / "sample.sav",
        )


def test_missing_workflow_trigger_is_rejected_by_core_catalog_verification(
    tmp_path,
):
    path = tmp_path / "workflow-trigger-drift.sqlite"
    database = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=database)
    engine = create_engine(database)
    with engine.begin() as connection:
        create_workflow_catalog(connection, workflow_catalog(MetaData()))
        connection.exec_driver_sql(
            "drop trigger oss_transformation_run_no_delete"
        )

    with pytest.raises(UnsupportedOperationError, match="incompatible"):
        openstatspec.initialize_catalog(database_url=database)


def test_verified_workflow_profile_remains_catalog_owned(tmp_path):
    path = tmp_path / "workflow-owned.sqlite"
    database = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=database)
    _create(database, "first")
    engine = create_engine(database)
    with engine.begin() as connection:
        create_workflow_catalog(connection, workflow_catalog(MetaData()))

    imported = _create(database, "second")

    assert imported["case_count"] == 1


def test_engine_identity_is_persisted_as_non_loss_audit_metadata(tmp_path):
    path = tmp_path / "engine-audit.sqlite"
    database = f"sqlite:///{path}"
    engine_identity = {"name": "pyspssio", "commit": "abc123"}

    imported = _create(
        database,
        "audited",
        operation_details={"engine": engine_identity},
    )

    connection = sqlite3.connect(path)
    row = connection.execute(
        "select severity, event_code, detail_json from fidelity_event "
        "where operation_id = ?",
        (imported["operation_id"],),
    ).fetchone()
    connection.close()
    assert row[:2] == ("info", "operation-engine-identity")
    assert json.loads(row[2])["engine"] == engine_identity
    assert wide.read_fidelity_events(
        database_url=database, dataset_id="audited",
    ) == ()


def test_database_rows_are_preflighted_before_export_descriptor(tmp_path):
    path = tmp_path / "row-drift.sqlite"
    database = f"sqlite:///{path}"
    wide.create_wide_dataset(
        database_url=database,
        dataset_id="numeric",
        source_name="numeric.sav",
        source_format="SAV",
        rows=[{"score": 1.5}],
        variables=_numeric_variables(),
    )
    connection = sqlite3.connect(path)
    connection.execute("update data_numeric set score = 'not-a-number'")
    connection.commit()
    connection.close()

    with pytest.raises(TargetCapabilityExceededError) as caught:
        wide.read_wide_dataset(database_url=database, dataset_id="numeric")
    assert caught.value.details["reason"] == "numeric_value_type"


def test_import_uses_bounded_statement_batches(tmp_path, monkeypatch):
    path = tmp_path / "batches.sqlite"
    database = f"sqlite:///{path}"
    real_bounded_batches = wide._bounded_batches
    calls = []

    def observe(rows, variables, maximum):
        calls.append((len(rows), maximum))
        yield from real_bounded_batches(rows, variables, maximum)

    monkeypatch.setattr(wide, "_bounded_batches", observe)
    _create(database, "batched")

    assert calls == [(1, SQLITE.max_statement_bytes)]
