"""Real-service conformance checks plus Dolt fail-closed and candidate probes."""

import os
from uuid import uuid4

import pandas as pd
import pyspssio
import pytest
from sqlalchemy import create_engine, inspect as inspect_database, text
from sqlalchemy.exc import DBAPIError

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.dolt_conformance import DoltConformanceSource
from conformance import compare_sav_semantics, write_supported_semantics_fixture


pytestmark = pytest.mark.services
_REQUIRED_ENGINE_LOSS = []
_COMPAT_NAME_LOSS = _REQUIRED_ENGINE_LOSS


@pytest.fixture
def source_sav(tmp_path):
    source = tmp_path / "service-fixture.sav"
    pyspssio.write_sav(
        str(source), pd.DataFrame({"age": [34.0, None], "name": ["Ada", ""]}),
        metadata={
            "var_types": {"name": 12}, "var_labels": {"age": "Age", "name": "Name"},
            "var_value_labels": {"age": {34.0: "thirty-four"}},
            "var_formats": {"age": "F8.0", "name": "A12"},
            "var_missing_values": {"age": {"values": [34.0]}},
        },
    )
    return source


@pytest.mark.parametrize(
    ("environment_name", "dataset_id"),
    [("OPENSTATSPEC_POSTGRES_URL", "profile_pg"), ("OPENSTATSPEC_MYSQL_URL", "profile_mysql"), ("OPENSTATSPEC_MARIADB_URL", "profile_mariadb")],
)
def test_live_profile_import_validate_and_export(environment_name, dataset_id, source_sav, tmp_path):
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")
    openstatspec.initialize_catalog(database_url=database_url)
    runtime_dataset_id = f"{dataset_id}_{uuid4().hex[:8]}"
    imported = openstatspec.import_sav(
        source_sav, database_url=database_url, dataset_id=runtime_dataset_id,
    )
    assert imported["case_count"] == 2
    assert openstatspec.validate(
        database_url=database_url, dataset_id=runtime_dataset_id,
    )["valid"] is True
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text(f"SELECT COUNT(*) FROM {imported['data_table']} ")).scalar_one() == 2
        assert connection.execute(text(
            "SELECT COUNT(*) FROM variable v JOIN dataset d ON d.dataset_id = v.dataset_id "
            "WHERE d.dataset_name = :dataset_name"
        ), {"dataset_name": runtime_dataset_id}).scalar_one() == 2
    destination = tmp_path / f"{dataset_id}.sav"
    exported = openstatspec.export_sav(database_url=database_url, dataset_id=runtime_dataset_id, destination=destination, allow_loss=_REQUIRED_ENGINE_LOSS)
    assert destination.exists()
    assert {diagnostic.code for diagnostic in exported.diagnostics} == set(_REQUIRED_ENGINE_LOSS)
    frame, metadata = pyspssio.read_sav(str(destination), convert_datetimes=False, include_user_missing=True)
    assert frame["age"].iloc[0] == 34.0
    assert pd.isna(frame["age"].iloc[1])
    assert frame["name"].tolist() == ["Ada", ""]
    assert metadata["var_labels"] == {"age": "Age", "name": "Name"}
    assert metadata["var_value_labels"] == {"age": {34.0: "thirty-four"}}


@pytest.mark.parametrize(
    ("environment_name", "dataset_id"),
    [("OPENSTATSPEC_POSTGRES_URL", "semantics_pg"), ("OPENSTATSPEC_MYSQL_URL", "semantics_mysql"), ("OPENSTATSPEC_MARIADB_URL", "semantics_mariadb")],
)
@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_live_profile_preserves_supported_sav_semantics(environment_name, dataset_id, suffix, tmp_path):
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")
    openstatspec.initialize_catalog(database_url=database_url)
    runtime_dataset_id = f"{dataset_id}_{suffix[1:]}_{uuid4().hex[:8]}"
    source = tmp_path / f"{runtime_dataset_id}{suffix}"
    destination = tmp_path / f"{runtime_dataset_id}-roundtrip{suffix}"
    write_supported_semantics_fixture(source)
    imported = openstatspec.import_sav(
        source, database_url=database_url, dataset_id=runtime_dataset_id,
    )
    assert imported["case_count"] == 4
    assert openstatspec.validate(
        database_url=database_url, dataset_id=runtime_dataset_id,
    )["valid"] is True
    openstatspec.export_sav(
        database_url=database_url, dataset_id=runtime_dataset_id,
        destination=destination, allow_loss=_COMPAT_NAME_LOSS,
    )
    assert compare_sav_semantics(source, destination) == {"equivalent": True, "differences": []}

def test_live_dolt_is_read_only_and_rejects_writes_without_declarations(
    tmp_path,
) -> None:
    database_url = os.environ.get("OPENSTATSPEC_DOLT_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_DOLT_URL is not configured")

    status = DoltConformanceSource.packaged().status()
    assert status["status"] == "blocked_no_concrete_declarations"
    assert status["declaration_count"] == 0
    assert status["write_enabled"] is False

    before = openstatspec.dolt_state_snapshot(database_url=database_url)
    assert before["read_only"] is True
    assert before["operational_write_enabled"] is False

    rejection = "no concrete declarations; write rejected before mutation"
    with pytest.raises(UnsupportedOperationError, match=rejection):
        openstatspec.initialize_catalog(database_url=database_url)
    after_initialize = openstatspec.dolt_state_snapshot(database_url=database_url)

    source = tmp_path / "blocked-write.sav"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    with pytest.raises(UnsupportedOperationError, match=rejection):
        openstatspec.import_sav(
            source, database_url=database_url, dataset_id="blocked_write",
        )
    after_import = openstatspec.dolt_state_snapshot(database_url=database_url)

    assert after_initialize["working_set_binding"] == before["working_set_binding"]
    assert after_import["working_set_binding"] == before["working_set_binding"]
    assert after_initialize["state"] == before["state"]
    assert after_import["state"] == before["state"]


@pytest.mark.candidate_evidence
def test_live_dolt_candidate_limit_probe_smoke() -> None:
    database_url = os.environ.get("OPENSTATSPEC_DOLT_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_DOLT_URL is not configured")
    engine = create_engine(database_url)
    token = uuid4().hex[:8]
    identifier_64 = f"evidence_{token}_" + "i" * (64 - len(f"evidence_{token}_"))
    identifier_65 = identifier_64 + "i"
    assert len(identifier_64.encode("utf-8")) == 64
    assert len(identifier_65.encode("utf-8")) == 65
    storage_table = f"evidence_storage_{token}"
    columns_306_table = f"evidence_columns_306_{token}"
    columns_307_table = f"evidence_columns_307_{token}"
    quote = engine.dialect.identifier_preparer.quote

    try:
        with engine.begin() as connection:
            connection.execute(text(
                f"CREATE TABLE {quote(identifier_64)} (value INTEGER)"
            ))
        assert identifier_64 in inspect_database(engine).get_table_names()
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text(
                    f"CREATE TABLE {quote(identifier_65)} (value INTEGER)"
                ))

        maximum_finite = float.fromhex("0x1.fffffffffffffp+1023")
        utf8_value = "é" * 32_752
        assert len(utf8_value.encode("utf-8")) == 65_504
        with engine.begin() as connection:
            connection.execute(text(
                f"CREATE TABLE {quote(storage_table)} "
                "(binary64_value DOUBLE NOT NULL, text_value LONGTEXT NOT NULL)"
            ))
            connection.execute(
                text(
                    f"INSERT INTO {quote(storage_table)} "
                    "(binary64_value, text_value) VALUES (:binary64, :text_value)"
                ),
                {"binary64": maximum_finite, "text_value": utf8_value},
            )
        with engine.connect() as connection:
            row = connection.execute(text(
                f"SELECT binary64_value, text_value, OCTET_LENGTH(text_value) "
                f"FROM {quote(storage_table)}"
            )).one()
        assert row[0] == maximum_finite
        assert row[1] == utf8_value
        assert row[2] == 65_504

        for column_count, table_name in (
            (306, columns_306_table), (307, columns_307_table),
        ):
            columns = ", ".join(
                f"{quote(f'c{ordinal:03d}')} INTEGER"
                for ordinal in range(1, column_count + 1)
            )
            with engine.begin() as connection:
                connection.execute(text(
                    f"CREATE TABLE {quote(table_name)} ({columns})"
                ))
            assert len(inspect_database(engine).get_columns(table_name)) == column_count
    finally:
        with engine.begin() as connection:
            for table_name in (
                identifier_64, identifier_65, storage_table,
                columns_306_table, columns_307_table,
            ):
                connection.execute(text(
                    f"DROP TABLE IF EXISTS {quote(table_name)}"
                ))
        engine.dispose()