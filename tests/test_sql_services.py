"""Real-service writes, round trips, failure boundaries, and candidate probes."""

import os
from uuid import uuid4

import pandas as pd
import pyspssio
import pytest
from sqlalchemy import MetaData, create_engine, inspect as inspect_database, text
from sqlalchemy.exc import DBAPIError

import openstatspec
from openstatspec.core import UnsupportedOperationError
from openstatspec.sql import inplace_transform, wide
from openstatspec.sql.normative import (
    catalog as normative_catalog,
    delete_dataset_representation,
)
from openstatspec.sql.wide import create_wide_dataset
from conformance import compare_sav_semantics, write_supported_semantics_fixture


pytestmark = pytest.mark.services
_REQUIRED_ENGINE_LOSS = []
_COMPAT_NAME_LOSS = [*_REQUIRED_ENGINE_LOSS, "compatible-variable-names-not-preserved"]


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
    before = (
        openstatspec.dolt_state_snapshot(database_url=database_url)["state"]
        if environment_name == "OPENSTATSPEC_DOLT_URL" else None
    )
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
    if before is not None:
        after = openstatspec.dolt_state_snapshot(database_url=database_url)["state"]
        assert after["head"] == before["head"]
        assert after["active_branch"] == before["active_branch"]


@pytest.mark.parametrize(
    ("environment_name", "dataset_id"),
    [("OPENSTATSPEC_POSTGRES_URL", "semantics_pg"), ("OPENSTATSPEC_MYSQL_URL", "semantics_mysql"), ("OPENSTATSPEC_MARIADB_URL", "semantics_mariadb"), ("OPENSTATSPEC_DOLT_URL", "semantics_dolt")],
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

def test_live_unknown_dolt_is_read_only_and_rejects_default_writes(
    tmp_path,
) -> None:
    database_url = os.environ.get("OPENSTATSPEC_UNSUPPORTED_DOLT_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_UNSUPPORTED_DOLT_URL is not configured")

    before = openstatspec.dolt_state_snapshot(database_url=database_url)
    assert before["read_only"] is True
    assert before["operational_write_enabled"] is False

    rejection = "not claimed supported"
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


def test_live_dolt_default_in_place_atomicity(source_sav, tmp_path, monkeypatch):
    database_url = os.environ.get("OPENSTATSPEC_DOLT_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_DOLT_URL is not configured")
    imported = openstatspec.import_sav(
        source_sav, database_url=database_url, dataset_id="apply_" + uuid4().hex[:8],
    )
    openstatspec.install_in_place_transformation_schema(database_url=database_url)
    engine = create_engine(database_url)
    # The caller, not the adapter, commits the imported baseline before apply.
    with engine.begin() as connection:
        connection.exec_driver_sql("CALL DOLT_ADD('.')")
        connection.exec_driver_sql(
            "CALL DOLT_COMMIT('-m', 'test baseline', '--author', 'Test <test@example.com>')"
        )
    before = openstatspec.dolt_state_snapshot(database_url=database_url)["state"]
    assert before["status"]["rows"] == []
    tables = inspect_database(engine).get_table_names()

    def contents():
        with engine.connect() as connection:
            return {
                table: tuple(connection.exec_driver_sql(
                    "SELECT * FROM " + connection.dialect.identifier_preparer.quote(table)
                )) for table in tables
            }

    original = contents()
    schema = openstatspec.VariableSchema((
        openstatspec.VariableDefinition("age", "numeric"),
        openstatspec.VariableDefinition("name", "string", declared_string_width=12),
    ))
    plan = openstatspec.compile_spss_syntax(
        "RECODE age (34 = 35). VARIABLE LABELS age 'Updated age'. "
        "VALUE LABELS age 35 'thirty-five'.", schema,
    ).plan
    arguments = dict(
        database_url=database_url, dataset_id=imported["dataset_id"],
        plan=plan, actor="service-test", expected_branch=before["active_branch"],
        expected_head=before["head"],
    )
    for key, value, code in (
        ("expected_branch", "wrong-branch", "dolt_branch_mismatch"),
        ("expected_head", "wrong-head", "dolt_head_mismatch"),
        ("expected_head", None, "dolt_context_required"),
    ):
        with pytest.raises(openstatspec.TransformationError) as caught:
            openstatspec.apply_transformation_plan_in_place(**{**arguments, key: value})
        assert caught.value.code == code
    create_plan = openstatspec.compile_spss_syntax(
        "RECODE age (34 = 35) INTO new_age.", schema,
    ).plan
    with pytest.raises(openstatspec.TransformationError) as caught:
        openstatspec.apply_transformation_plan_in_place(**{**arguments, "plan": create_plan})
    assert caught.value.code == "schema_change_not_atomic"
    for boundary in ("data", "catalog", "audit"):
        with monkeypatch.context() as patch:
            def fail(phase):
                if phase == boundary:
                    raise RuntimeError("injected " + phase)
            patch.setattr(inplace_transform, "_failure_boundary", fail)
            with pytest.raises(RuntimeError, match="injected " + boundary):
                openstatspec.apply_transformation_plan_in_place(**arguments)
        assert contents() == original
        assert inspect_database(engine).get_table_names() == tables
        assert openstatspec.dolt_state_snapshot(database_url=database_url)["state"] == before

    result = openstatspec.apply_transformation_plan_in_place(**arguments)
    assert result["dataset_id"] == imported["dataset_id"]
    assert result["physical_table_name"] == imported["data_table"]
    assert result["dolt_commit_performed"] is False
    assert inspect_database(engine).get_table_names() == tables
    assert contents()["dataset"] == original["dataset"]
    after = openstatspec.dolt_state_snapshot(database_url=database_url)["state"]
    assert after["head"] == before["head"]
    assert after["active_branch"] == before["active_branch"]
    assert after["status"]["rows"]
    with pytest.raises(openstatspec.TransformationError) as caught:
        openstatspec.apply_transformation_plan_in_place(**arguments)
    assert caught.value.code == "dolt_working_set_dirty"
    assert openstatspec.validate(database_url=database_url, dataset_id=imported["dataset_id"])["valid"]
    destination = tmp_path / "recoded.sav"
    openstatspec.export_sav(
        database_url=database_url, dataset_id=imported["dataset_id"], destination=destination,
    )
    frame, metadata = pyspssio.read_sav(str(destination), include_user_missing=True)
    assert frame["age"].iloc[0] == 35.0
    assert pd.isna(frame["age"].iloc[1])
    assert metadata["var_labels"]["age"] == "Updated age"
    assert metadata["var_value_labels"]["age"] == {35.0: "thirty-five"}
    assert openstatspec.dolt_state_snapshot(database_url=database_url)["state"] == after
    engine.dispose()


def test_live_dolt_default_import_failure_cleanup(source_sav, monkeypatch):
    database_url = os.environ.get("OPENSTATSPEC_DOLT_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_DOLT_URL is not configured")
    openstatspec.initialize_catalog(database_url=database_url)
    engine = create_engine(database_url)
    tables = inspect_database(engine).get_table_names()
    with engine.connect() as connection:
        datasets = tuple(connection.exec_driver_sql("SELECT * FROM dataset"))
    before = openstatspec.dolt_state_snapshot(database_url=database_url)["state"]
    store = wide.store_normative_dataset

    def fail(*args, **kwargs):
        store(*args, **kwargs)
        raise RuntimeError("injected import failure")

    monkeypatch.setattr(wide, "store_normative_dataset", fail)
    with pytest.raises(RuntimeError, match="injected import failure"):
        openstatspec.import_sav(
            source_sav, database_url=database_url, dataset_id="failed_" + uuid4().hex[:8],
        )
    assert inspect_database(engine).get_table_names() == tables
    with engine.connect() as connection:
        assert tuple(connection.exec_driver_sql("SELECT * FROM dataset")) == datasets
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM operation WHERE status = 'failed'"
        ).scalar_one() >= 1
    after = openstatspec.dolt_state_snapshot(database_url=database_url)["state"]
    assert after["head"] == before["head"]
    assert after["active_branch"] == before["active_branch"]
    engine.dispose()


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

def test_live_postgresql_in_place_create_rejects_exhausted_physical_slots() -> None:
    database_url = os.environ.get("OPENSTATSPEC_POSTGRES_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_POSTGRES_URL is not configured")

    token = uuid4().hex[:12]
    dataset_name = f"inplace_slots_{token}"
    base_variable = {
        "storage_kind": "numeric",
        "string_width": None,
        "label": "",
        "format": "F8.0",
        "print_format": "[5, 8, 0]",
        "write_format": "[5, 8, 0]",
        "measure": "scale",
        "role": "input",
        "alignment": "right",
        "display_width": 8,
        "attributes": "{}",
        "compat_name": None,
        "value_labels": "{}",
        "missing_ranges": "[]",
    }
    variables = [
        {
            **base_variable,
            "ordinal": ordinal,
            "source_name": f"v{ordinal}",
            "physical_name": f"v{ordinal}",
        }
        for ordinal in range(1, 1_600)
    ]
    created = None
    engine = create_engine(database_url)
    try:
        openstatspec.initialize_catalog(database_url=database_url)
        created = create_wide_dataset(
            database_url=database_url,
            dataset_id=dataset_name,
            source_name="slot-limit.sav",
            source_format="SAV",
            source_sha256="0" * 64,
            rows=[],
            variables=variables,
        )
        openstatspec.install_in_place_transformation_schema(
            database_url=database_url,
        )
        plan = openstatspec.TransformationPlan(
            (
                openstatspec.DeleteVariableOperation("v1599"),
                openstatspec.CreateVariableOperation("replacement", "numeric"),
            ),
            contract="openstatspec-transformation-plan-v0.3",
        )

        with pytest.raises(openstatspec.TransformationError) as caught:
            openstatspec.apply_transformation_plan_in_place(
                database_url=database_url,
                dataset_id=created["dataset_id"],
                plan=plan,
                actor="service-test",
            )

        assert caught.value.code == "source_variable_limit"
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT COUNT(*) FROM variable WHERE dataset_id = :dataset_id"
            ), {"dataset_id": created["dataset_id"]}).scalar_one() == 1_599
            assert connection.execute(text(
                "SELECT COUNT(*) FROM pg_attribute "
                "WHERE attrelid = to_regclass(:relation_name) AND attnum > 0"
            ), {"relation_name": created["data_table"]}).scalar_one() == 1_600
    finally:
        if created is not None:
            with engine.begin() as connection:
                delete_dataset_representation(
                    connection, normative_catalog(MetaData()), created["dataset_id"],
                )
                quote = connection.dialect.identifier_preparer.quote
                connection.execute(text(
                    f"DROP TABLE IF EXISTS {quote(created['data_table'])}"
                ))
        engine.dispose()
