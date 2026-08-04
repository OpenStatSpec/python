from __future__ import annotations

import sqlite3

from types import SimpleNamespace
import pytest

import openstatspec
import openstatspec.sql.inplace_transform as inplace_transform
from openstatspec.sql.wide import create_wide_dataset


SYNTAX = """COMPUTE target = 0.
IF (source_a = 1 AND source_b = 1) target = 1.
VARIABLE LABELS target 'Example label'.
VALUE LABELS target 0 'No' 1 'Yes'.
FORMATS target (F1.0).
VARIABLE LEVEL target (NOMINAL).
EXECUTE."""


def _variable(ordinal: int, name: str) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "source_name": name,
        "physical_name": name,
        "storage_kind": "numeric",
        "string_width": None,
        "label": name,
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


@pytest.fixture
def conditional_catalog(tmp_path):
    path = tmp_path / "conditional.sqlite"
    url = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=url)
    create_wide_dataset(
        database_url=url,
        dataset_id="conditional_source",
        source_name="synthetic.sav",
        source_format="SAV",
        source_sha256="e" * 64,
        rows=[
            {"source_a": 1.0, "source_b": 1.0},
            {"source_a": 1.0, "source_b": 0.0},
            {"source_a": 0.0, "source_b": 1.0},
            {"source_a": 2.0, "source_b": 2.0},
        ],
        variables=[_variable(1, "source_a"), _variable(2, "source_b")],
    )
    openstatspec.install_in_place_transformation_schema(database_url=url)
    connection = sqlite3.connect(path)
    dataset_id, table_name = connection.execute(
        "SELECT dataset_id, physical_table_name FROM dataset"
    ).fetchone()
    connection.close()
    return url, path, dataset_id, table_name


def test_exact_bounded_program_compiles_to_stable_v02_plan() -> None:
    schema = openstatspec.VariableSchema((
        openstatspec.VariableDefinition("source_a", "numeric"),
        openstatspec.VariableDefinition("source_b", "numeric"),
    ))
    compilation = openstatspec.compile_spss_syntax(SYNTAX, schema)
    assert compilation.plan.contract == "openstatspec-transformation-plan-v0.2"
    assert [operation.op for operation in compilation.plan.operations] == [
        "assign",
        "conditional_assign",
        "set_variable_label",
        "replace_value_labels",
        "set_format",
        "set_measurement_level",
        "execute",
    ]
    restored = openstatspec.transformation_plan_from_dict(
        compilation.plan.as_dict()
    )
    assert restored.canonical_json() == compilation.plan.canonical_json()
    assert restored.sha256() == compilation.plan_hash
    assert compilation.plan_hash == "f57b176eb86027eeccd5fc2da5c421444f55b0f6cc70c93aa3e673f8cdbb2e90"


def test_exact_bounded_program_applies_data_and_both_catalogs(
    conditional_catalog,
) -> None:
    url, path, dataset_id, table_name = conditional_catalog
    result = openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text=SYNTAX,
        actor="synthetic-test",
    )
    assert result["status"] == "succeeded"
    assert result["dolt_commit_performed"] is False
    connection = sqlite3.connect(path)
    assert connection.execute(
        f'SELECT target FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0,), (0.0,), (0.0,), (0.0,)]
    assert connection.execute(
        "SELECT variable_label, print_format_family, print_format_width, "
        "print_format_decimals, write_format_family, write_format_width, "
        "write_format_decimals, measurement_level FROM variable "
        "WHERE source_name = 'target'"
    ).fetchone() == ("Example label", "F", 1, 0, "F", 1, 0, "nominal")
    assert connection.execute(
        "SELECT label, format, print_format, write_format, measure "
        "FROM variable_catalog WHERE source_name = 'target'"
    ).fetchone() == ("Example label", "F1.0", "[5, 1, 0]", "[5, 1, 0]", "nominal")
    assert connection.execute(
        "SELECT numeric_code, label FROM value_label ORDER BY ordinal"
    ).fetchall() == [(0.0, "No"), (1.0, "Yes")]
    assert connection.execute(
        "SELECT contract_id, source_kind, operation_count, status "
        "FROM transformation_apply"
    ).fetchone() == (
        "openstatspec-in-place-transformation-v0.2",
        "spss_syntax",
        7,
        "succeeded",
    )
    connection.close()


@pytest.mark.parametrize("boundary", ["schema", "data", "catalog", "audit"])
def test_injected_boundary_failure_leaves_no_partial_apply(
    conditional_catalog, monkeypatch, boundary,
) -> None:
    url, path, dataset_id, table_name = conditional_catalog
    raised = False
    connection = sqlite3.connect(path)
    connection.execute(f'ALTER TABLE "{table_name}" ADD COLUMN unrelated REAL')
    connection.execute(f'UPDATE "{table_name}" SET unrelated = 42')
    connection.commit()
    connection.close()

    def fail(selected: str) -> None:
        nonlocal raised
        if selected == boundary and not raised:
            raised = True
            raise RuntimeError(f"synthetic {boundary} failure")

    monkeypatch.setattr(inplace_transform, "_failure_boundary", fail)
    with pytest.raises(RuntimeError, match=f"synthetic {boundary} failure"):
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text=SYNTAX,
            actor="synthetic-test",
        )
    connection = sqlite3.connect(path)
    columns = {
        row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    }
    assert "target" not in columns
    assert connection.execute(
        "SELECT COUNT(*) FROM variable WHERE source_name = 'target'"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT COUNT(*) FROM variable_catalog WHERE source_name = 'target'"
    ).fetchone() == (0,)
    assert connection.execute(
        "SELECT COUNT(*) FROM transformation_apply"
    ).fetchone() == (0,)
    assert connection.execute(
        f'SELECT source_a, source_b FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0, 1.0), (1.0, 0.0), (0.0, 1.0), (2.0, 2.0)]

    assert connection.execute(
        f'SELECT unrelated FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(42.0,), (42.0,), (42.0,), (42.0,)]
    connection.close()

def test_failure_never_drops_a_preexisting_target(
    conditional_catalog, monkeypatch,
) -> None:
    url, path, dataset_id, table_name = conditional_catalog
    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text=SYNTAX,
        actor="synthetic-test",
    )

    def fail(selected: str) -> None:
        if selected == "data":
            raise RuntimeError("synthetic replace failure")

    monkeypatch.setattr(inplace_transform, "_failure_boundary", fail)
    with pytest.raises(RuntimeError, match="synthetic replace failure"):
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text="COMPUTE target = 9. EXECUTE.",
            actor="synthetic-test",
        )
    connection = sqlite3.connect(path)
    assert connection.execute(
        f'SELECT target FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0,), (0.0,), (0.0,), (0.0,)]
    assert connection.execute(
        "SELECT COUNT(*) FROM variable WHERE source_name = 'target'"
    ).fetchone() == (1,)
    assert connection.execute(
        "SELECT COUNT(*) FROM transformation_apply"
    ).fetchone() == (1,)
    connection.close()



def test_numeric_predicates_use_sql_three_valued_logic_and_ordered_execution(
    conditional_catalog,
) -> None:
    url, path, dataset_id, table_name = conditional_catalog
    connection = sqlite3.connect(path)
    connection.execute(
        f'UPDATE "{table_name}" SET source_a = NULL WHERE __case_ordinal = 2'
    )
    connection.commit()
    connection.close()
    source = (
        "COMPUTE target = 0. "
        "IF ((source_a < 1 OR source_a >= 2) AND source_b <= 2) target = 1. "
        "IF (target = 1 AND source_b > 1) target = 2. EXECUTE."
    )
    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text=source,
        actor="synthetic-test",
    )
    connection = sqlite3.connect(path)
    assert connection.execute(
        f'SELECT target FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(0.0,), (0.0,), (1.0,), (2.0,)]
    connection.close()


def test_dolt_mock_applies_exact_program_to_preexisting_target_without_schema_ddl(
    conditional_catalog, monkeypatch,
) -> None:
    url, path, dataset_id, table_name = conditional_catalog
    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text=SYNTAX,
        actor="provisioning-stage",
    )
    monkeypatch.setattr(
        inplace_transform,
        "effective_profile",
        lambda _url, **_kwargs: (SimpleNamespace(name="dolt"), {}),
    )
    states = iter([
        ("main", "abc123", 0),
        ("main", "abc123", 0),
        ("main", "abc123", 4),
    ])
    monkeypatch.setattr(
        inplace_transform, "_dolt_state", lambda _connection: next(states)
    )
    result = openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text=SYNTAX,
        actor="synthetic-test",
        expected_branch="main",
        expected_head="abc123",
    )
    assert result["dolt_commit_performed"] is False
    connection = sqlite3.connect(path)
    columns = [
        row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    ]
    assert columns.count("target") == 1
    assert connection.execute(
        "SELECT COUNT(*) FROM variable WHERE source_name = 'target'"
    ).fetchone() == (1,)
    connection.close()


def test_dolt_mock_rejects_create_target_before_schema_mutation(
    conditional_catalog, monkeypatch,
) -> None:
    url, path, dataset_id, table_name = conditional_catalog
    monkeypatch.setattr(
        inplace_transform,
        "effective_profile",
        lambda _url, **_kwargs: (SimpleNamespace(name="dolt"), {}),
    )
    monkeypatch.setattr(
        inplace_transform,
        "_dolt_state",
        lambda _connection: ("main", "abc123", 0),
    )
    with pytest.raises(openstatspec.TransformationError) as caught:
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text=SYNTAX,
            actor="synthetic-test",
            expected_branch="main",
            expected_head="abc123",
        )
    assert caught.value.code == "schema_change_not_atomic"
    assert "target" not in {
        row[1] for row in sqlite3.connect(path).execute(
            f'PRAGMA table_info("{table_name}")'
        )
    }


def test_dolt_mock_rechecks_clean_state_after_dataset_lock(
    conditional_catalog, monkeypatch,
) -> None:
    url, path, dataset_id, table_name = conditional_catalog
    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text=SYNTAX,
        actor="provisioning-stage",
    )
    connection = sqlite3.connect(path)
    before = connection.execute(
        f'SELECT target FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall()
    connection.close()

    monkeypatch.setattr(
        inplace_transform,
        "effective_profile",
        lambda _url, **_kwargs: (SimpleNamespace(name="dolt"), {}),
    )
    states = iter([
        ("main", "abc123", 0),
        ("main", "abc123", 1),
    ])
    observed_states = []

    def next_state(_connection):
        state = next(states)
        observed_states.append(state)
        return state

    monkeypatch.setattr(inplace_transform, "_dolt_state", next_state)

    with pytest.raises(openstatspec.TransformationError) as caught:
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text=SYNTAX,
            actor="synthetic-test",
            expected_branch="main",
            expected_head="abc123",
        )

    assert caught.value.code == "dolt_working_set_dirty"
    assert observed_states == [
        ("main", "abc123", 0),
        ("main", "abc123", 1),
    ]
    connection = sqlite3.connect(path)
    assert connection.execute(
        f'SELECT target FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == before
    assert connection.execute(
        "SELECT COUNT(*) FROM transformation_apply"
    ).fetchone() == (1,)
    connection.close()
