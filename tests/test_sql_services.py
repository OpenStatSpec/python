"""Real-service conformance checks for PostgreSQL, MySQL, MariaDB, and Dolt."""

import os
from uuid import uuid4

import pandas as pd
import pyspssio
import pytest
from sqlalchemy import create_engine, inspect as inspect_database, text
from sqlalchemy.exc import DBAPIError

import openstatspec
import openstatspec.sql.wide as wide
from openstatspec.core import UnsupportedOperationError
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
    [("OPENSTATSPEC_POSTGRES_URL", "profile_pg"), ("OPENSTATSPEC_MYSQL_URL", "profile_mysql"), ("OPENSTATSPEC_MARIADB_URL", "profile_mariadb"), ("OPENSTATSPEC_DOLT_URL", "profile_dolt")],
)
def test_live_profile_import_validate_and_export(environment_name, dataset_id, source_sav, tmp_path):
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")
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
        assert connection.execute(text("SELECT COUNT(*) FROM variable_catalog WHERE dataset_id = :dataset_id"), {"dataset_id": runtime_dataset_id}).scalar_one() == 2
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
    [("OPENSTATSPEC_POSTGRES_URL", "semantics_pg"), ("OPENSTATSPEC_MYSQL_URL", "semantics_mysql"), ("OPENSTATSPEC_MARIADB_URL", "semantics_mariadb"), ("OPENSTATSPEC_DOLT_URL", "semantics_dolt")],
)
@pytest.mark.parametrize("suffix", [".sav", ".zsav"])
def test_live_profile_preserves_supported_sav_semantics(environment_name, dataset_id, suffix, tmp_path):
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")
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

def test_live_dolt_conservative_source_width_envelope(tmp_path) -> None:
    database_url = os.environ.get("OPENSTATSPEC_DOLT_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_DOLT_URL is not configured")
    token = uuid4().hex[:8]
    accepted_id = f"dolt_width_accepted_{token}"
    rejected_id = f"dolt_width_rejected_{token}"
    accepted_source = tmp_path / f"{accepted_id}.sav"
    rejected_source = tmp_path / f"{rejected_id}.sav"
    accepted_columns = [f"v{ordinal:03d}" for ordinal in range(1, 306)]
    rejected_columns = [*accepted_columns, "v306"]
    pyspssio.write_sav(
        str(accepted_source),
        pd.DataFrame([[float(ordinal) for ordinal in range(1, 306)]],
                     columns=accepted_columns),
    )
    pyspssio.write_sav(
        str(rejected_source),
        pd.DataFrame([[float(ordinal) for ordinal in range(1, 307)]],
                     columns=rejected_columns),
    )

    imported = openstatspec.import_sav(
        accepted_source, database_url=database_url, dataset_id=accepted_id,
    )
    assert imported["case_count"] == 1
    assert openstatspec.validate(
        database_url=database_url, dataset_id=accepted_id,
    )["variable_count"] == 305

    with pytest.raises(UnsupportedOperationError, match="Target capability exceeded"):
        openstatspec.import_sav(
            rejected_source, database_url=database_url, dataset_id=rejected_id,
        )

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text(
            "select count(*) from dataset where dataset_name = :name"
        ), {"name": rejected_id}).scalar_one() == 0
        assert connection.execute(text(
            "select count(*) from dataset_catalog where dataset_id = :name"
        ), {"name": rejected_id}).scalar_one() == 0
        mirror_event = connection.execute(text("""
            select f.dataset_id, f.direction, f.severity, f.code
              from fidelity_event_catalog f
              join operation_catalog o on o.operation_id = f.operation_id
             where o.source = :source
        """), {"source": rejected_source.name}).mappings().one()
        normative_event = connection.execute(text("""
            select f.dataset_id, f.direction, f.severity, f.event_code
              from fidelity_event f
             where f.source_item = :source
        """), {"source": rejected_source.name}).mappings().one()
    assert tuple(mirror_event.values()) == (
        None, "import", "error", "target_capability_exceeded",
    )
    assert tuple(normative_event.values()) == (
        None, "import", "error", "target_capability_exceeded",
    )
    assert f"data_{rejected_id}" not in inspect_database(engine).get_table_names()


def test_live_dolt_post_ddl_fault_has_complete_compensating_cleanup(
    tmp_path, monkeypatch,
) -> None:
    database_url = os.environ.get("OPENSTATSPEC_DOLT_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_DOLT_URL is not configured")
    dataset_id = f"dolt_cleanup_{uuid4().hex[:8]}"
    source = tmp_path / f"{dataset_id}.sav"
    pyspssio.write_sav(str(source), pd.DataFrame({"answer": [1.0]}))
    real_store = wide.store_normative_dataset

    def fail_after_normative_write(*args, **kwargs):
        real_store(*args, **kwargs)
        raise RuntimeError("injected Dolt post-DDL fault")

    monkeypatch.setattr(wide, "store_normative_dataset", fail_after_normative_write)

    with pytest.raises(RuntimeError, match="injected Dolt post-DDL fault"):
        openstatspec.import_sav(
            source, database_url=database_url, dataset_id=dataset_id,
        )

    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text(
            "select count(*) from dataset where dataset_name = :name"
        ), {"name": dataset_id}).scalar_one() == 0
        assert connection.execute(text(
            "select count(*) from dataset_catalog where dataset_id = :name"
        ), {"name": dataset_id}).scalar_one() == 0
        assert connection.execute(text(
            "select status, dataset_id from operation_catalog where source = :source"
        ), {"source": source.name}).one() == ("failed", None)
        assert connection.execute(text("""
            select f.dataset_id, f.direction, f.severity, f.code
              from fidelity_event_catalog f
              join operation_catalog o on o.operation_id = f.operation_id
             where o.source = :source
        """), {"source": source.name}).one() == (
            None, "import", "error", "import_failed",
        )
    assert f"data_{dataset_id}" not in inspect_database(engine).get_table_names()


def test_live_dolt_adapter_value_boundary_is_atomic() -> None:
    database_url = os.environ.get("OPENSTATSPEC_DOLT_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_DOLT_URL is not configured")
    token = uuid4().hex[:8]
    accepted_id = f"dolt_value_accepted_{token}"
    rejected_id = f"dolt_value_rejected_{token}"
    accepted_value = "é" * 32_752
    rejected_value = accepted_value + "x"
    assert len(accepted_value.encode("utf-8")) == 65_504
    assert len(rejected_value.encode("utf-8")) == 65_505
    variables = [{
        "ordinal": 1, "source_name": "value", "physical_name": "value",
        "storage_kind": "string", "string_width": 65_504, "label": "",
        "format": "A65504", "measure": "nominal", "alignment": "left",
        "display_width": 8, "value_labels": "{}", "missing_ranges": "[]",
    }]

    imported = wide.create_wide_dataset(
        database_url=database_url, dataset_id=accepted_id,
        source_name="accepted.sav", source_format="SAV",
        rows=[{"value": accepted_value}], variables=variables,
    )
    assert imported["case_count"] == 1

    with pytest.raises(UnsupportedOperationError) as caught:
        wide.create_wide_dataset(
            database_url=database_url, dataset_id=rejected_id,
            source_name="rejected.sav", source_format="SAV",
            rows=[{"value": rejected_value}], variables=variables,
        )
    assert caught.value.details["reason"] == "text_value_limit"

    engine = create_engine(database_url)
    accepted_table = wide.data_table_name(accepted_id)
    rejected_table = wide.data_table_name(rejected_id)
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as connection:
        assert connection.execute(text(
            f"SELECT OCTET_LENGTH(value) FROM {quote(accepted_table)}"
        )).scalar_one() == 65_504
        assert connection.execute(text(
            "SELECT COUNT(*) FROM dataset_catalog WHERE dataset_id = :dataset_id"
        ), {"dataset_id": rejected_id}).scalar_one() == 0
        assert connection.execute(text(
            "SELECT COUNT(*) FROM dataset WHERE dataset_name = :dataset_id"
        ), {"dataset_id": rejected_id}).scalar_one() == 0
    assert rejected_table not in inspect_database(engine).get_table_names()
    engine.dispose()


def test_live_dolt_published_storage_and_identifier_evidence() -> None:
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