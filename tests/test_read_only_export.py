"""Read/export must work without database write privileges or write declarations.

Optional live check against a disposable local Dolt server:
OPENSTATSPEC_TEST_DOLT_ADMIN_URL=mysql+pymysql://root@127.0.0.1:PORT/ \
    python -m pytest tests/test_read_only_export.py
The fixture creates and removes its own database and SELECT-only user.
"""
import os
import sqlite3
from uuid import uuid4

import pandas as pd
import pyspssio
import pytest
from sqlalchemy import MetaData, Table, create_engine, event, select
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import Engine, make_url

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql import capabilities, wide
from openstatspec.sql.normative import catalog
from openstatspec.spss import sav


@pytest.fixture(params=["sqlite", "dolt"])
def seeded_catalog(request, tmp_path):
    admin_url = os.environ.get("OPENSTATSPEC_TEST_DOLT_ADMIN_URL")
    if request.param == "dolt" and not admin_url:
        pytest.skip("Live Dolt requires OPENSTATSPEC_TEST_DOLT_ADMIN_URL")
    source = tmp_path / "source.sav"
    pyspssio.write_sav(
        str(source), pd.DataFrame({"answer": [1.0, 2.0], "name": ["one", "two"]}),
        var_labels={"answer": "Answer"},
        var_value_labels={"answer": {1.0: "Yes", 2.0: "No"}},
        var_missing_values={"answer": {"values": [2.0]}},
    )
    path = tmp_path / "source.sqlite"
    url = f"sqlite:///{path}"
    openstatspec.import_sav(source, database_url=url, dataset_id="sample")
    source_engine = create_engine(url)
    logical = catalog(MetaData())
    with source_engine.connect() as connection:
        dataset = connection.execute(select(logical.dataset)).mappings().one()
    if request.param == "sqlite":
        def snapshot():
            with sqlite3.connect(path) as connection:
                return tuple(connection.iterdump())

        def read_only(connection, _record):
            if isinstance(connection, sqlite3.Connection):
                connection.execute("PRAGMA query_only = ON")

        event.listen(Engine, "connect", read_only)
        try:
            yield url, snapshot, source
        finally:
            event.remove(Engine, "connect", read_only)
            source_engine.dispose()
        return

    name = "export_" + uuid4().hex
    user = "reader_" + uuid4().hex[:16]
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    target = None
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f"CREATE DATABASE `{name}`")
            connection.exec_driver_sql(f"CREATE USER '{user}'@'%%' IDENTIFIED BY 'readonly'")
            connection.exec_driver_sql(f"GRANT SELECT ON `{name}`.* TO '{user}'@'%%'")
        target = create_engine(make_url(admin_url).set(database=name))
        # Seed existing data with administrative SQL, not a bypass of the
        # adapter's write guard. The exporter only receives the SELECT user.
        logical.catalog_identity.metadata.create_all(target)
        physical = Table(dataset["physical_table_name"], MetaData(), autoload_with=source_engine)
        physical.c.name.type = LONGTEXT()
        physical.create(target)
        with source_engine.connect() as origin, target.begin() as destination:
            for table in [*logical.catalog_identity.metadata.sorted_tables, physical]:
                rows = [dict(row) for row in origin.execute(select(table)).mappings()]
                if rows:
                    destination.execute(table.insert(), rows)
        with target.connect() as connection:
            connection.exec_driver_sql("CALL DOLT_ADD('.')")
            connection.exec_driver_sql(
                "CALL DOLT_COMMIT('-m', 'existing dataset', '--author', 'Test <test@example.com>')"
            )
            connection.commit()

        def snapshot():
            with target.connect() as connection:
                return (
                    tuple(connection.exec_driver_sql("SELECT * FROM dolt_status")),
                    tuple(connection.exec_driver_sql("SELECT commit_hash FROM dolt_log")),
                    tuple(
                        (table.name, tuple(connection.execute(select(table))))
                        for table in [*logical.catalog_identity.metadata.sorted_tables, physical]
                    ),
                )

        reader_url = make_url(admin_url).set(database=name, username=user, password="readonly")
        with admin.connect() as connection:
            grants = tuple(connection.exec_driver_sql(f"SHOW GRANTS FOR '{user}'@'%%'").scalars())
            assert any("GRANT SELECT ON" in grant for grant in grants)
            assert all(grant.startswith(("GRANT USAGE ON", "GRANT SELECT ON")) for grant in grants)
        yield reader_url.render_as_string(hide_password=False), snapshot, source
    finally:
        if target is not None:
            target.dispose()
        source_engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
            connection.exec_driver_sql(f"DROP USER IF EXISTS '{user}'@'%%'")
        admin.dispose()


@pytest.fixture
def read_catalog(seeded_catalog):
    forbidden = []

    def check(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().split()[0].upper() not in {"SELECT", "SHOW", "PRAGMA", "DESCRIBE"}:
            forbidden.append(statement)
            raise AssertionError("Read operation attempted a non-read SQL statement")

    event.listen(Engine, "before_cursor_execute", check)
    try:
        yield seeded_catalog
    finally:
        event.remove(Engine, "before_cursor_execute", check)
        assert forbidden == []


@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_export_and_reads_leave_no_database_trace(read_catalog, tmp_path, suffix):
    url, snapshot, source = read_catalog
    before = snapshot()
    if make_url(url).get_backend_name() == "mysql":
        # This is the real server identity and the empty packaged registry.
        with pytest.raises(UnsupportedOperationError):
            wide.initialize_wide_catalog(database_url=url)
    output = tmp_path / ("export" + suffix)
    result = openstatspec.export_sav(database_url=url, dataset_id="sample", destination=output)
    assert "operation_id" not in result
    expected, expected_meta = pyspssio.read_sav(str(source), include_user_missing=True)
    actual, actual_meta = pyspssio.read_sav(str(output), include_user_missing=True)
    pd.testing.assert_frame_equal(actual, expected)
    for key in ("var_labels", "var_value_labels", "var_missing_values", "var_formats", "var_measure_levels"):
        assert actual_meta[key] == expected_meta[key]
    openstatspec.validate(database_url=url, dataset_id="sample")
    wide.read_fidelity_events(database_url=url, dataset_id="sample")
    openstatspec.inspect(source)
    assert snapshot() == before
    assert not list(tmp_path.glob(".*.previous"))
    assert not list(tmp_path.glob(".*.staging.*"))


@pytest.mark.parametrize("failure", ["loss", "writer", "publish", "restore", "staging", "backup"])
def test_failed_export_never_writes_database(read_catalog, tmp_path, monkeypatch, failure):
    url, snapshot, _source = read_catalog
    before = snapshot()
    output = tmp_path / "existing.sav"
    output.write_bytes(b"previous file")

    def fail(*_args, **_kwargs):
        raise OSError("injected failure")

    if failure == "loss":
        monkeypatch.setattr(sav, "_export_loss_report", lambda *_a, **_k: (
            {"code": "test-loss", "detail": "Requires consent", "details": {}},
        ))
    elif failure == "writer":
        monkeypatch.setattr(sav, "_write_with_dictionary_bridge", fail)
    elif failure in {"publish", "restore"}:
        monkeypatch.setattr(sav.os, "link", fail)
        if failure == "restore":
            monkeypatch.setattr(sav, "_restore_export_destination", fail)
    elif failure == "staging":
        cleanup = sav.TemporaryDirectory.cleanup

        def fail_cleanup(self):
            cleanup(self)
            raise OSError("injected failure")

        monkeypatch.setattr(sav.TemporaryDirectory, "cleanup", fail_cleanup)
    else:
        unlink = type(output).unlink

        def fail_backup(self, *args, **kwargs):
            if self.suffix == ".previous":
                raise OSError("injected failure")
            return unlink(self, *args, **kwargs)

        monkeypatch.setattr(type(output), "unlink", fail_backup)
    with pytest.raises((OSError, UnsupportedOperationError)):
        openstatspec.export_sav(database_url=url, dataset_id="sample", destination=output)
    assert snapshot() == before
    if failure in {"restore", "backup"}:
        # If filesystem recovery itself fails, preserve the previous bytes in
        # the existing safety backup and report the error only to the caller.
        assert next(tmp_path.glob(".*.previous")).read_bytes() == b"previous file"
    else:
        assert output.read_bytes() == b"previous file"
    if failure == "loss":
        result = openstatspec.export_sav(
            database_url=url, dataset_id="sample", destination=output, allow_loss=["test-loss"],
        )
        assert {d.code for d in result.diagnostics} == {"test-loss"}
        assert snapshot() == before


def test_dolt_reads_do_not_enable_undeclared_writes(monkeypatch):
    url = "mysql+pymysql://reader@host/dataset"
    active = {
        "profile": "dolt", "raw_product_version": "2.3.0", "claimed_supported": False,
    }
    monkeypatch.setattr(capabilities, "active_connection", lambda *_a, **_k: active)
    assert capabilities.read_profile(url)[0].name == "dolt"
    with pytest.raises(UnsupportedOperationError):
        capabilities.effective_profile(url)
    with pytest.raises(UnsupportedOperationError):
        wide.initialize_wide_catalog(database_url=url)
    with pytest.raises(UnsupportedOperationError):
        wide.create_wide_dataset(
            database_url=url, dataset_id="blocked", source_name="source.sav",
            source_format="SAV", variables=[], rows=[],
        )


def test_derived_validation_does_not_initialize_catalog(tmp_path):
    url = f"sqlite:///{tmp_path / 'catalog.sqlite'}"
    openstatspec.initialize_catalog(database_url=url)
    with sqlite3.connect(tmp_path / "catalog.sqlite") as connection:
        before = tuple(connection.iterdump())
        with pytest.raises(UnsupportedOperationError):
            openstatspec.validate_derived(database_url=url, derived_dataset_id=str(uuid4()))
        assert tuple(connection.iterdump()) == before
