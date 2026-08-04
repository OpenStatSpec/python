"""Regression tests for persistent URLs and operation-bound audit writes."""

import json
import sqlite3
from contextlib import contextmanager

import pytest

from openstatspec.core import UnsupportedOperationError
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


def _create(database_url, dataset_id="sample"):
    return wide.create_wide_dataset(
        database_url=database_url,
        dataset_id=dataset_id,
        source_name=f"{dataset_id}.sav",
        source_format="SAV",
        rows=[{"name": "ok"}],
        variables=_variables(),
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite:///file:shared-memory?mode=memory&uri=true",
    ],
)
def test_import_rejects_ephemeral_sqlite_catalogs(database_url):
    with pytest.raises(UnsupportedOperationError, match="persistent SQLite"):
        _create(database_url)


def test_initialization_does_not_modify_an_existing_unverified_catalog(tmp_path):
    path = tmp_path / "partial.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("create table dataset_catalog (legacy_name text)")
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedOperationError, match="foreign"):
        wide.initialize_wide_catalog(database_url=f"sqlite:///{path}")

    connection = sqlite3.connect(path)
    tables = {
        row[0]
        for row in connection.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    connection.close()
    assert tables == {"dataset_catalog"}


def test_import_routes_catalog_and_dataset_mutations_through_binding_guard(
    tmp_path, monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'bound.sqlite'}"
    real_bound_transaction = wide._bound_catalog_transaction
    phases = []

    @contextmanager
    def observe(**kwargs):
        phases.append(kwargs["phase"])
        with real_bound_transaction(**kwargs) as connection:
            yield connection

    monkeypatch.setattr(wide, "_bound_catalog_transaction", observe)
    _create(database_url)

    assert phases == ["catalog initialization", "import"]


def test_export_audit_mutations_route_through_binding_guard(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'bound-export.sqlite'}"
    _create(database_url)
    real_bound_transaction = wide._bound_catalog_transaction
    phases = []

    @contextmanager
    def observe(**kwargs):
        phases.append(kwargs["phase"])
        with real_bound_transaction(**kwargs) as connection:
            yield connection

    monkeypatch.setattr(wide, "_bound_catalog_transaction", observe)
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
        database_url=database_url, operation_id=succeeded_id,
    )
    wide.record_export_backup_retained(
        database_url=database_url,
        operation_id=succeeded_id,
        destination="succeeded.sav",
        backup="succeeded.sav.backup",
        cleanup_error=RuntimeError("test"),
    )
    wide.record_export_cleanup_failure(
        database_url=database_url,
        destination="cleanup.sav",
        original_error=RuntimeError("export"),
        cleanup_error=RuntimeError("cleanup"),
        residual_object_inventory={},
        deterministic_recovery_evidence={},
    )

    assert phases == [
        "export audit creation",
        "export audit failure",
        "export audit creation",
        "export audit finalization",
        "export backup-retention audit",
        "export cleanup-failure audit",
    ]


def test_export_engine_identity_is_persisted_as_non_loss_audit_metadata(tmp_path):
    path = tmp_path / "export-audit.sqlite"
    database_url = f"sqlite:///{path}"
    _create(database_url)
    engine_identity = {"name": "pyspssio", "commit": "export-commit"}

    operation_id = wide.record_export_operation(
        database_url=database_url,
        dataset_id="sample",
        destination="out.sav",
        allowed_fidelity_events=(),
        operation_details={"engine": engine_identity},
    )

    connection = sqlite3.connect(path)
    row = connection.execute(
        "select severity, event_code, source_item, detail_json "
        "from fidelity_event where operation_id = ?",
        (operation_id,),
    ).fetchone()
    connection.close()
    assert row[:3] == ("info", "operation-engine-identity", "out.sav")
    assert json.loads(row[3])["engine"] == engine_identity
    assert wide.read_fidelity_events(
        database_url=database_url, dataset_id="sample",
    ) == ()
