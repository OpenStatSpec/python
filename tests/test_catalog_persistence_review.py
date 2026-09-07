"""Regression tests for persistent URLs and operation-bound audit writes."""

import sqlite3
from contextlib import contextmanager

import pytest

from openstatspec.core import UnsupportedOperationError
from openstatspec.sql import wide
from openstatspec.sql.profiles import TargetCapabilityExceededError


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


def test_failed_import_preflight_routes_audit_through_binding_guard(
    tmp_path, monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'failed-preflight.sqlite'}"
    real_bound_transaction = wide._bound_catalog_transaction
    phases = []

    @contextmanager
    def observe(**kwargs):
        phases.append(kwargs["phase"])
        with real_bound_transaction(**kwargs) as connection:
            yield connection

    monkeypatch.setattr(wide, "_bound_catalog_transaction", observe)
    variables = _variables()
    variables[0].update({
        "storage_kind": "numeric", "string_width": None,
        "format": "F8.2", "alignment": "right",
    })
    with pytest.raises(TargetCapabilityExceededError):
        wide.create_wide_dataset(
            database_url=database_url,
            dataset_id="invalid",
            source_name="invalid.sav",
            source_format="SAV",
            rows=[{"name": "not-a-number"}],
            variables=variables,
        )

    assert phases == ["catalog initialization", "import preflight failure"]


def test_failed_import_preflight_rejects_catalog_drift_before_audit(
    tmp_path, monkeypatch,
):
    path = tmp_path / "failed-preflight-drift.sqlite"
    database_url = f"sqlite:///{path}"
    real_bound_transaction = wide._bound_catalog_transaction

    @contextmanager
    def inject_drift(**kwargs):
        if kwargs["phase"] == "import preflight failure":
            connection = sqlite3.connect(path)
            connection.execute("create view foreign_view as select 1 as value")
            connection.commit()
            connection.close()
        with real_bound_transaction(**kwargs) as connection:
            yield connection

    monkeypatch.setattr(wide, "_bound_catalog_transaction", inject_drift)
    variables = _variables()
    variables[0].update({
        "storage_kind": "numeric", "string_width": None,
        "format": "F8.2", "alignment": "right",
    })

    with pytest.raises(UnsupportedOperationError, match="foreign"):
        wide.create_wide_dataset(
            database_url=database_url,
            dataset_id="invalid",
            source_name="invalid.sav",
            source_format="SAV",
            rows=[{"name": "not-a-number"}],
            variables=variables,
        )

    connection = sqlite3.connect(path)
    assert connection.execute("select count(*) from operation").fetchone() == (0,)
    assert connection.execute("select count(*) from fidelity_event").fetchone() == (0,)
    connection.close()
