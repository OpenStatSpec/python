"""Regression tests for bound commit checks and export dataset linkage."""

import sqlite3

from sqlalchemy import create_engine

from openstatspec.sql import wide


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


def test_dolt_completion_identity_is_checked_before_transaction_commit(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    state = {
        "database": "catalog",
        "active_branch": "main",
        "head": "abc123",
    }
    transaction_states = []

    def capture(connection, **_kwargs):
        transaction_states.append(connection.in_transaction())
        return state

    monkeypatch.setattr(wide, "_capture_dolt_state", capture)
    with wide._bound_catalog_transaction(
        engine=engine,
        profile_name="dolt",
        active={
            "working_set_binding": {
                "database": "catalog",
                "active_branch": "main",
            },
        },
        audit_relations=set(),
        phase="test",
    ):
        pass

    assert transaction_states == [False, True]


def test_dolt_completion_rejects_unrelated_working_set_changes():
    before = {
        "database": "catalog", "active_branch": "main", "head": "abc123",
        "unrelated_sha256": "before",
    }
    after = {**before, "unrelated_sha256": "after"}

    import pytest
    from openstatspec.core import UnsupportedOperationError

    with pytest.raises(UnsupportedOperationError, match="unrelated working-set"):
        wide._require_dolt_success_identity(before, after, phase="export audit")


def test_export_lifecycle_events_remain_linked_to_the_dataset(tmp_path):
    path = tmp_path / "export-events.sqlite"
    database_url = f"sqlite:///{path}"
    wide.create_wide_dataset(
        database_url=database_url,
        dataset_id="sample",
        source_name="sample.sav",
        source_format="SAV",
        rows=[{"name": "ok"}],
        variables=_variables(),
    )

    failed_id = wide.record_export_operation(
        database_url=database_url,
        dataset_id="sample",
        destination="failed.sav",
        allowed_fidelity_events=(),
        terminal=False,
    )
    wide.fail_export_operation(
        database_url=database_url,
        operation_id=failed_id,
        failure_details={"reason": "test"},
    )
    succeeded_id = wide.record_export_operation(
        database_url=database_url,
        dataset_id="sample",
        destination="succeeded.sav",
        allowed_fidelity_events=(),
        terminal=False,
    )
    wide.finish_export_operation(
        database_url=database_url,
        operation_id=succeeded_id,
    )
    wide.record_export_backup_retained(
        database_url=database_url,
        operation_id=succeeded_id,
        destination="succeeded.sav",
        backup="succeeded.sav.backup",
        cleanup_error=RuntimeError("test"),
    )

    events = wide.read_fidelity_events(
        database_url=database_url,
        dataset_id="sample",
    )
    assert {event["code"] for event in events} == {
        "backup_retained",
        "export_failed",
    }
    assert wide.read_fidelity_events(
        database_url=database_url, dataset_id="sample", direction="import",
    ) == ()
    connection = sqlite3.connect(path)
    assert connection.execute(
        "select count(*) from fidelity_event "
        "where operation_id in (?, ?) and dataset_id is null",
        (failed_id, succeeded_id),
    ).fetchone() == (0,)
    connection.close()
