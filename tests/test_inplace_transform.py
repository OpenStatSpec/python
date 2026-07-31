from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text

import openstatspec
import openstatspec.sql.inplace_transform as inplace_transform
from openstatspec.sql.inplace_transform import _apply_on_connection
from openstatspec.sql.wide import create_wide_dataset


def _variables() -> list[dict[str, object]]:
    return [{
        "ordinal": 1,
        "source_name": "score",
        "physical_name": "score",
        "storage_kind": "numeric",
        "string_width": None,
        "label": "Score",
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
    }]


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "in-place.sqlite"
    url = f"sqlite:///{path}"
    create_wide_dataset(
        database_url=url,
        dataset_id="in_place_source",
        source_name="source.sav",
        source_format="SAV",
        source_sha256="d" * 64,
        rows=[{"score": 1.0}, {"score": 2.0}, {"score": 3.0}],
        variables=_variables(),
    )
    openstatspec.install_in_place_transformation_schema(database_url=url)
    connection = sqlite3.connect(path)
    dataset_id, table_name = connection.execute(
        "SELECT dataset_id, physical_table_name FROM dataset"
    ).fetchone()
    return url, path, dataset_id, table_name


def test_plan_applies_to_same_dataset_and_physical_table_without_copy(
    catalog,
) -> None:
    url, path, dataset_id, table_name = catalog
    engine = create_engine(url)
    with engine.begin() as connection:
        before_datasets = connection.execute(text(
            "SELECT COUNT(*) FROM dataset"
        )).scalar_one()
        before_data_tables = {
            name for name in inspect(connection).get_table_names()
            if name.startswith("data_")
        }
        result = _apply_on_connection(
            connection,
            dataset_id=dataset_id,
            source_text=(
                "RECODE score (1,2 = 0) (3 = 1) INTO score_band. "
                "VARIABLE LABELS score_band 'Score band'. "
                "VALUE LABELS score_band 0 'Lower' 1 'Upper'."
            ),
            actor="test-agent",
            database_profile="sqlite",
            allow_schema_change=True,
            dolt_branch="feature/recode",
            dolt_head="abc123",
        )
        after_datasets = connection.execute(text(
            "SELECT COUNT(*) FROM dataset"
        )).scalar_one()
        after_data_tables = {
            name for name in inspect(connection).get_table_names()
            if name.startswith("data_")
        }
        tables = set(inspect(connection).get_table_names())

    assert result["dataset_id"] == dataset_id
    assert result["physical_table_name"] == table_name
    assert before_datasets == after_datasets == 1
    assert before_data_tables == after_data_tables == {table_name}
    assert not {
        name for name in tables
        if name.startswith("derived_")
        or "staging" in name
        or "rollback" in name
        or "snapshot" in name
        or name.startswith("transformation_plan_")
    }

    connection = sqlite3.connect(path)
    assert connection.execute(
        f'SELECT __case_ordinal, score, score_band FROM "{table_name}" '
        "ORDER BY __case_ordinal"
    ).fetchall() == [(1, 1.0, 0.0), (2, 2.0, 0.0), (3, 3.0, 1.0)]
    assert connection.execute(
        "SELECT dataset_id, physical_table_name FROM dataset"
    ).fetchone() == (dataset_id, table_name)
    assert connection.execute(
        "SELECT variable_label FROM variable WHERE source_name = 'score_band'"
    ).fetchone() == ("Score band",)
    assert connection.execute(
        "SELECT numeric_code, label FROM value_label ORDER BY ordinal"
    ).fetchall() == [(0.0, "Lower"), (1.0, "Upper")]
    assert connection.execute(
        "SELECT label FROM variable_catalog WHERE source_name = 'score_band'"
    ).fetchone() == ("Score band",)
    assert connection.execute(
        "SELECT database_profile, dolt_branch, dolt_head_before, "
        "dolt_head_after, actor, status "
        "FROM transformation_apply"
    ).fetchone() == (
        "sqlite", "feature/recode", "abc123", "abc123", "test-agent",
        "succeeded",
    )


def test_public_apply_supports_non_dolt_without_building_undo(catalog) -> None:
    url, path, dataset_id, table_name = catalog
    result = openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text="RECODE score (1 = 0).",
        actor="test-agent",
    )
    assert result["dolt_branch"] is None
    assert result["dolt_commit_performed"] is False
    assert sqlite3.connect(path).execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(0.0,), (2.0,), (3.0,)]


def test_missing_audit_schema_fails_before_mutation(catalog) -> None:
    url, path, dataset_id, table_name = catalog
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE transformation_apply")
    connection.commit()
    connection.close()
    with pytest.raises(openstatspec.TransformationError) as caught:
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text="RECODE score (1 = 0).",
            actor="test-agent",
        )
    assert caught.value.code == "in_place_audit_schema_missing"
    assert sqlite3.connect(path).execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0,), (2.0,), (3.0,)]


def test_nontransactional_ddl_profile_rejects_create_before_mutation(
    catalog,
) -> None:
    url, path, dataset_id, table_name = catalog
    engine = create_engine(url)
    with pytest.raises(openstatspec.TransformationError) as caught:
        with engine.begin() as connection:
            _apply_on_connection(
                connection,
                dataset_id=dataset_id,
                source_text=(
                    "VARIABLE LABELS score 'Changed'. "
                    "RECODE score (1 = 0) INTO score_band."
                ),
                actor="test-agent",
                database_profile="mysql",
                allow_schema_change=False,
                dolt_branch=None,
                dolt_head=None,
            )
    engine.dispose()
    assert caught.value.code == "schema_change_not_atomic"
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT variable_label FROM variable WHERE source_name = 'score'"
    ).fetchone() == ("Score",)
    assert "score_band" not in {
        row[1] for row in connection.execute(
            f'PRAGMA table_info("{table_name}")'
        )
    }


def test_public_apply_binds_expected_dolt_branch_and_head(
    catalog, monkeypatch,
) -> None:
    url, path, dataset_id, table_name = catalog
    monkeypatch.setattr(
        inplace_transform,
        "effective_profile",
        lambda _url: (SimpleNamespace(name="dolt"), {}),
    )
    states = iter([
        ("feature/recode", "abc123", 0),
        ("feature/recode", "abc123", 4),
    ])
    monkeypatch.setattr(
        inplace_transform, "_dolt_state", lambda _connection: next(states)
    )
    result = openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text="RECODE score (1 = 9).",
        actor="test-agent",
        expected_branch="feature/recode",
        expected_head="abc123",
    )
    assert result["dolt_commit_performed"] is False
    assert sqlite3.connect(path).execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(9.0,), (2.0,), (3.0,)]


def test_public_apply_rejects_dirty_dolt_working_set_before_mutation(
    catalog, monkeypatch,
) -> None:
    url, path, dataset_id, table_name = catalog
    monkeypatch.setattr(
        inplace_transform,
        "effective_profile",
        lambda _url: (SimpleNamespace(name="dolt"), {}),
    )
    monkeypatch.setattr(
        inplace_transform,
        "_dolt_state",
        lambda _connection: ("feature/recode", "abc123", 1),
    )
    with pytest.raises(openstatspec.TransformationError) as caught:
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text="RECODE score (1 = 9).",
            actor="test-agent",
            expected_branch="feature/recode",
            expected_head="abc123",
        )
    assert caught.value.code == "dolt_working_set_dirty"
    assert sqlite3.connect(path).execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0,), (2.0,), (3.0,)]


@pytest.mark.parametrize(
    ("state", "expected_error"),
    [
        (("main", "abc123", 0), "dolt_branch_mismatch"),
        (("feature/recode", "other-head", 0), "dolt_head_mismatch"),
    ],
)
def test_public_apply_rejects_dolt_context_mismatch_before_mutation(
    catalog, monkeypatch, state, expected_error,
) -> None:
    url, path, dataset_id, table_name = catalog
    monkeypatch.setattr(
        inplace_transform,
        "effective_profile",
        lambda _url: (SimpleNamespace(name="dolt"), {}),
    )
    monkeypatch.setattr(
        inplace_transform, "_dolt_state", lambda _connection: state
    )
    with pytest.raises(openstatspec.TransformationError) as caught:
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text="RECODE score (1 = 9).",
            actor="test-agent",
            expected_branch="feature/recode",
            expected_head="abc123",
        )
    assert caught.value.code == expected_error
    assert sqlite3.connect(path).execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0,), (2.0,), (3.0,)]


def test_capability_declares_dolt_owned_versioning() -> None:
    declaration = openstatspec.capability_matrix()["optional_profiles"][
        "spss_in_place_transformation"
    ]
    assert declaration["database_products"] == [
        "sqlite", "postgresql", "mysql", "mariadb", "dolt",
    ]
    assert declaration["status"] == "experimental"
    assert declaration["execution_evidence"]["sqlite"] == "local_conformance"
    assert declaration["execution_evidence"]["dolt"] == (
        "service_conformance_required"
    )
    assert declaration["creates_derived_dataset"] is False
    assert declaration["creates_persistent_data_copy"] is False
    assert declaration["openstatspec_rollback_or_version_history"] is False
    assert declaration["performs_dolt_commit"] is False
