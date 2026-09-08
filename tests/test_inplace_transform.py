from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import openstatspec
import openstatspec.sql.inplace_transform as inplace_transform
from openstatspec.sql.inplace_transform import (
    InPlacePlanSubmission,
    _apply_plan_on_connection,
)
from openstatspec.sql.profiles import (
    DOLT, SQLITE, TargetCapabilityExceededError,
)
from openstatspec.sql.wide import (
    create_wide_dataset, read_wide_dataset, validate_wide_dataset,
)


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


def _plan(source_text: str):
    schema = openstatspec.VariableSchema((
        openstatspec.VariableDefinition(
            "score",
            "numeric",
            variable_label="Score",
        ),
    ))
    return openstatspec.compile_spss_syntax(source_text, schema).plan


def _submission(source_text: str) -> InPlacePlanSubmission:
    plan = _plan(source_text)
    return InPlacePlanSubmission(
        plan=plan,
        source_kind="canonical_plan",
        source_hash=plan.sha256(),
    )


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "in-place.sqlite"
    url = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=url)
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


def _publish_score(catalog):
    url, _path, dataset_id, _table_name = catalog
    openstatspec.apply_spss_in_place(
        database_url=url, dataset_id=dataset_id,
        source_text="COMPUTE other = score.", actor="test-setup",
    )
    return openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=dataset_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
            "expression_role": "identity",
        }],
        transformation_name="published_score",
    )


@pytest.mark.parametrize("surface", ["canonical", "spss"])
def test_shared_value_labels_preserve_sibling_until_last_owner(catalog, surface):
    url, path, dataset_id, table_name = catalog
    openstatspec.apply_spss_in_place(
        database_url=url, dataset_id=dataset_id,
        source_text="COMPUTE other = score. VALUE LABELS score 1 'Original'.",
        actor="test-setup",
    )
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        old_set = connection.execute(
            "SELECT value_label_set_id FROM variable_value_label_set"
        ).fetchone()[0]
        other_id = connection.execute(
            "SELECT variable_id FROM variable WHERE source_name = 'other'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO variable_value_label_set VALUES (?, ?)",
            (other_id, old_set),
        )
        original_set = connection.execute("SELECT * FROM value_label_set").fetchall()
        original_labels = connection.execute("SELECT * FROM value_label").fetchall()
    schema = openstatspec.VariableSchema(tuple(
        openstatspec.VariableDefinition(name, "numeric")
        for name in ("score", "other")
    ))
    for name in ("score", "other"):
        syntax = f"VALUE LABELS {name} 2 'Replacement'."
        if surface == "canonical":
            result = openstatspec.apply_transformation_plan_in_place(
                database_url=url, dataset_id=dataset_id, actor="test-agent",
                plan=openstatspec.compile_spss_syntax(syntax, schema).plan,
            )
        else:
            result = openstatspec.apply_spss_in_place(
                database_url=url, dataset_id=dataset_id, actor="test-agent",
                source_text=syntax,
            )
        assert (result["dataset_id"], result["physical_table_name"]) == (
            dataset_id, table_name,
        )
        _, variables, _rows = read_wide_dataset(database_url=url, dataset_id=dataset_id)
        assert {v["source_name"]: json.loads(v["value_labels"]) for v in variables} == {
            "score": {"2.0": "Replacement"},
            "other": {"1.0": "Original"} if name == "score" else {"2.0": "Replacement"},
        }
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                f'SELECT __case_ordinal, score, other FROM "{table_name}" '
                "ORDER BY __case_ordinal"
            ).fetchall() == [(1, 1.0, 1.0), (2, 2.0, 2.0), (3, 3.0, 3.0)]
            assert connection.execute(
                "SELECT dataset_id, physical_table_name FROM dataset"
            ).fetchall() == [(dataset_id, table_name)]
            assert connection.execute(
                "SELECT status FROM transformation_apply WHERE apply_id = ?",
                (result["apply_id"],),
            ).fetchone() == ("succeeded",)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute(
                "SELECT * FROM value_label_set WHERE value_label_set_id = ?", (old_set,),
            ).fetchall() == (original_set if name == "score" else [])
            assert connection.execute(
                "SELECT * FROM value_label WHERE value_label_set_id = ?", (old_set,),
            ).fetchall() == (original_labels if name == "score" else [])
            assert connection.execute(
                "SELECT * FROM variable_value_label_set WHERE value_label_set_id = ?",
                (old_set,),
            ).fetchall() == ([(other_id, old_set)] if name == "score" else [])


@pytest.mark.parametrize("surface", ["canonical", "spss"])
@pytest.mark.parametrize("recreate", [False, True], ids=["delete", "recreate"])
def test_published_lineage_blocks_original_deletion_before_mutation(
    catalog, monkeypatch, surface, recreate,
):
    url, path, dataset_id, _table_name = catalog
    _publish_score(catalog)
    with sqlite3.connect(path) as connection:
        before = tuple(connection.iterdump())
    reached = []
    monkeypatch.setattr(inplace_transform, "_failure_boundary", reached.append)
    syntax = (
        "COMPUTE temporary = score. VARIABLE LABELS score 'Must not change'. "
        "DELETE VARIABLES ScOrE."
    )
    if recreate:
        syntax += " COMPUTE score = temporary."
    with pytest.raises(openstatspec.TransformationError) as caught:
        if surface == "canonical":
            openstatspec.apply_transformation_plan_in_place(
                database_url=url, dataset_id=dataset_id, actor="test-agent",
                plan=_plan(syntax),
            )
        else:
            openstatspec.apply_spss_in_place(
                database_url=url, dataset_id=dataset_id, actor="test-agent",
                source_text=syntax,
            )
    assert caught.value.code == "variable_has_dependents"
    assert "score" in str(caught.value).casefold()
    assert reached == []
    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == before


def test_lineage_allows_local_deletes_and_unreferenced_original_recreation(catalog):
    url, path, dataset_id, table_name = catalog
    published = _publish_score(catalog)
    with sqlite3.connect(path) as connection:
        original_ids = dict(connection.execute(
            "SELECT source_name, variable_id FROM variable"
        ))
        lineage = connection.execute("SELECT * FROM derived_variable_lineage").fetchall()
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    result = openstatspec.apply_spss_in_place(
        database_url=url, dataset_id=dataset_id, actor="test-agent",
        source_text=(
            "COMPUTE tmp = score. DELETE VARIABLES tmp. "
            "RECODE score (2 = 9) (ELSE = COPY) INTO band. DELETE VARIABLES band. "
            "STRING note (A4). DELETE VARIABLES note. "
            "DELETE VARIABLES other. "
            "RECODE score (2 = 9) (ELSE = COPY) INTO other."
        ),
    )
    assert (result["dataset_id"], result["physical_table_name"]) == (dataset_id, table_name)
    with sqlite3.connect(path) as connection:
        variables = connection.execute(
            "SELECT source_name, variable_id, physical_name, source_ordinal "
            "FROM variable ORDER BY source_ordinal"
        ).fetchall()
        assert variables[0] == ("score", original_ids["score"], "score", 1)
        assert len(variables) == 2
        assert variables[1][0] == "other"
        assert variables[1][1] not in original_ids.values()
        assert variables[1][3] == 6
        assert connection.execute(
            f'SELECT __case_ordinal, score, "{variables[1][2]}" FROM "{table_name}" '
            "ORDER BY __case_ordinal"
        ).fetchall() == [(1, 1.0, 1.0), (2, 2.0, 9.0), (3, 3.0, 3.0)]
        assert [row[1] for row in connection.execute(
            f'PRAGMA table_info("{table_name}")'
        )] == ["__case_ordinal", "score", variables[1][2]]
        assert connection.execute(
            "SELECT dataset_id, physical_table_name FROM dataset"
        ).fetchall() == [(dataset_id, table_name)]
        assert connection.execute("SELECT * FROM derived_variable_lineage").fetchall() == lineage
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == tables
    assert openstatspec.validate_derived(
        database_url=url, derived_dataset_id=published["derived_dataset_id"],
    )["valid"] is True


def test_native_foreign_key_failure_rolls_back_schema_data_metadata_and_audit(catalog):
    url, path, dataset_id, _table_name = catalog
    openstatspec.apply_spss_in_place(
        database_url=url, dataset_id=dataset_id,
        source_text="VARIABLE LABELS score 'Before probe'.", actor="test-setup",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER invalid_variable_reference AFTER UPDATE OF variable_label "
            "ON variable BEGIN INSERT INTO missing_rule "
            "(missing_rule_id, variable_id, ordinal, rule_kind, code_kind, numeric_value) "
            "VALUES ('fk-probe', 'nonexistent-variable', 1, 'discrete', 'numeric', 99); END"
        )
        before = tuple(connection.iterdump())
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed") as caught:
        openstatspec.apply_transformation_plan_in_place(
            database_url=url, dataset_id=dataset_id, actor="test-agent",
            plan=_plan("COMPUTE temporary = score. VARIABLE LABELS score 'Trigger probe'."),
        )
    assert isinstance(caught.value.orig, sqlite3.IntegrityError)
    with sqlite3.connect(path) as connection:
        assert tuple(connection.iterdump()) == before


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
        result = _apply_plan_on_connection(
            connection,
            dataset_id=dataset_id,
            submission=_submission(
                "RECODE score (1,2 = 0) (3 = 1) INTO score_band. "
                "VARIABLE LABELS score_band 'Score band'. "
                "VALUE LABELS score_band 0 'Lower' 1 'Upper'."
            ),
            actor="test-agent",
            database_profile="sqlite",
            target_profile=inplace_transform.effective_profile(url)[0],
            allow_schema_change=True,
            allow_delete_variable=True,
            dolt_branch=None,
            dolt_head=None,
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
    assert "variable_catalog" not in tables
    assert connection.execute(
        "SELECT database_profile, dolt_branch, dolt_head_before, "
        "dolt_head_after, actor, status "
        "FROM transformation_apply"
    ).fetchone() == (
        "sqlite", None, None, None, "test-agent",
        "succeeded",
    )


def test_string_declaration_creates_column_and_catalog_variable(catalog) -> None:
    url, path, dataset_id, table_name = catalog

    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text="STRING note (A8).",
        actor="test-agent",
    )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select source_name, storage_kind, declared_string_width from variable "
        "where dataset_id = ? order by source_ordinal",
        (dataset_id,),
    ).fetchall() == [("score", "numeric", None), ("note", "string", 8)]
    assert connection.execute(
        f'SELECT note FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [("",), ("",), ("",)]
    note_column = next(
        row for row in connection.execute(f'PRAGMA table_info("{table_name}")')
        if row[1] == "note"
    )
    assert note_column[2:5] == ("TEXT", 1, "''")
    connection.close()
    assert validate_wide_dataset(
        database_url=url, dataset_id=dataset_id,
    )["valid"] is True


def test_delete_preserves_surviving_physical_bindings_and_ordinals(tmp_path) -> None:
    path = tmp_path / "collision-delete.sqlite"
    url = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=url)
    base = _variables()[0]
    variables = [
        {
            **base,
            "ordinal": ordinal,
            "source_name": source_name,
            "physical_name": physical,
        }
        for ordinal, (source_name, physical) in enumerate((
            ("a-b", "a_b"), ("a_b", "a_b_2"), ("keep", "keep"),
        ), start=1)
    ]
    create_wide_dataset(
        database_url=url,
        dataset_id="collision_source",
        source_name="collision.sav",
        source_format="SAV",
        source_sha256="c" * 64,
        rows=[
            {"a_b": 1.0, "a_b_2": 2.0, "keep": 3.0},
            {"a_b": 4.0, "a_b_2": 5.0, "keep": 6.0},
        ],
        variables=variables,
    )
    openstatspec.install_in_place_transformation_schema(database_url=url)
    connection = sqlite3.connect(path)
    dataset_id = connection.execute(
        "select dataset_id from dataset where dataset_name = 'collision_source'"
    ).fetchone()[0]
    connection.close()
    plan = openstatspec.TransformationPlan(
        (openstatspec.DeleteVariableOperation("a-b"),),
        contract="openstatspec-transformation-plan-v0.3",
    )

    openstatspec.apply_transformation_plan_in_place(
        database_url=url,
        dataset_id=dataset_id,
        plan=plan,
        actor="test-agent",
    )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select source_name, physical_name, source_ordinal from variable "
        "where dataset_id = ? order by source_ordinal",
        (dataset_id,),
    ).fetchall() == [("a_b", "a_b_2", 2), ("keep", "keep", 3)]
    assert connection.execute(
        "select a_b_2, keep from data_collision_source order by __case_ordinal"
    ).fetchall() == [(2.0, 3.0), (5.0, 6.0)]
    connection.close()
    assert validate_wide_dataset(
        database_url=url, dataset_id=dataset_id,
    )["valid"] is True


def test_delete_prunes_an_empty_spss_variable_set(catalog) -> None:
    url, path, dataset_id, _table_name = catalog
    connection = sqlite3.connect(path)
    variable_id = connection.execute(
        "select variable_id from variable where dataset_id = ?",
        (dataset_id,),
    ).fetchone()[0]
    variable_set_id = "00000000-0000-0000-0000-000000000002"
    connection.execute(
        "insert into variable_set "
        "(variable_set_id, dataset_id, source_ordinal, set_name) "
        "values (?, ?, 1, 'scores')",
        (variable_set_id, dataset_id),
    )
    connection.execute(
        "insert into variable_set_member "
        "(variable_set_id, variable_id, source_ordinal) values (?, ?, 1)",
        (variable_set_id, variable_id),
    )
    connection.commit()
    connection.close()

    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text="COMPUTE other = score. DELETE VARIABLES score.",
        actor="test-agent",
    )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select count(*) from variable_set where dataset_id = ?",
        (dataset_id,),
    ).fetchone() == (0,)
    assert connection.execute(
        "select count(*) from variable_set_member"
    ).fetchone() == (0,)
    connection.close()
    dataset, _variables, _rows = read_wide_dataset(
        database_url=url, dataset_id=dataset_id,
    )
    assert dataset["source_extensions"] == {}


def test_delete_prunes_an_empty_multiple_response_set(catalog) -> None:
    url, path, dataset_id, _table_name = catalog
    connection = sqlite3.connect(path)
    variable_id = connection.execute(
        "select variable_id from variable where dataset_id = ?",
        (dataset_id,),
    ).fetchone()[0]
    response_set_id = "00000000-0000-0000-0000-000000000001"
    connection.execute(
        "insert into multiple_response_set "
        "(multiple_response_set_id, dataset_id, source_ordinal, set_name, "
        "set_kind, counted_value_kind, counted_numeric_value) "
        "values (?, ?, 1, '$scores', 'MD', 'numeric', 1.0)",
        (response_set_id, dataset_id),
    )
    connection.execute(
        "insert into multiple_response_member "
        "(multiple_response_set_id, variable_id, source_ordinal) "
        "values (?, ?, 1)",
        (response_set_id, variable_id),
    )
    connection.commit()
    connection.close()

    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text="COMPUTE other = score. DELETE VARIABLES score.",
        actor="test-agent",
    )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select count(*) from multiple_response_set where dataset_id = ?",
        (dataset_id,),
    ).fetchone() == (0,)
    assert connection.execute(
        "select count(*) from multiple_response_member"
    ).fetchone() == (0,)
    connection.close()
    assert validate_wide_dataset(
        database_url=url, dataset_id=dataset_id,
    )["valid"] is True


def test_delete_then_recreate_same_name_resolves_operations_in_order(catalog) -> None:
    url, path, dataset_id, table_name = catalog

    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text=(
            "COMPUTE other = score. DELETE VARIABLES score. "
            "RECODE other (2 = 9) (ELSE = COPY) INTO score."
        ),
        actor="test-agent",
    )

    connection = sqlite3.connect(path)
    variables = connection.execute(
        "select source_name, physical_name, source_ordinal from variable "
        "where dataset_id = ? order by source_ordinal",
        (dataset_id,),
    ).fetchall()
    assert [(row[0], row[2]) for row in variables] == [
        ("other", 2), ("score", 3),
    ]
    physical = {row[0]: row[1] for row in variables}
    assert connection.execute(
        f'SELECT "{physical["other"]}", "{physical["score"]}" '
        f'FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0, 1.0), (2.0, 9.0), (3.0, 3.0)]
    connection.close()
    assert validate_wide_dataset(
        database_url=url, dataset_id=dataset_id,
    )["valid"] is True


def test_temporary_created_target_can_be_deleted_before_final_schema(catalog) -> None:
    url, path, dataset_id, table_name = catalog

    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text="COMPUTE tmp = score. DELETE VARIABLES tmp.",
        actor="test-agent",
    )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select source_name, source_ordinal from variable "
        "where dataset_id = ? order by source_ordinal",
        (dataset_id,),
    ).fetchall() == [("score", 1)]
    assert [
        row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    ] == ["__case_ordinal", "score"]
    connection.close()


def test_temporary_target_type_is_not_taken_from_same_name_recreation(catalog) -> None:
    url, path, dataset_id, table_name = catalog

    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text=(
            "COMPUTE tmp = score. DELETE VARIABLES tmp. STRING tmp (A4)."
        ),
        actor="test-agent",
    )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select source_name, storage_kind, declared_string_width "
        "from variable where dataset_id = ? order by source_ordinal",
        (dataset_id,),
    ).fetchall() == [("score", "numeric", None), ("tmp", "string", 4)]
    physical = dict(connection.execute(
        "select source_name, physical_name from variable where dataset_id = ?",
        (dataset_id,),
    ).fetchall())
    assert connection.execute(
        f'SELECT "{physical["score"]}", "{physical["tmp"]}" '
        f'FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0, ""), (2.0, ""), (3.0, "")]
    connection.close()
    assert validate_wide_dataset(
        database_url=url, dataset_id=dataset_id,
    )["valid"] is True


def test_public_apply_supports_non_dolt_without_building_undo(catalog) -> None:
    url, path, dataset_id, table_name = catalog
    plan = _plan("RECODE score (1 = 0).")
    result = openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text="RECODE score (1 = 0).",
        actor="test-agent",
    )
    assert result["dolt_branch"] is None
    assert result["dolt_commit_performed"] is False
    assert result["plan_hash"] == plan.sha256()
    assert sqlite3.connect(path).execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(0.0,), (2.0,), (3.0,)]
    audit = sqlite3.connect(path).execute(
        "SELECT source_kind, frontend_contract FROM transformation_apply"
    ).fetchone()
    assert audit == (
        "spss_syntax",
        "openstatspec-spss-syntax-frontend-v0.2",
    )


def test_schema_commands_record_the_v03_frontend_contract(catalog) -> None:
    url, path, dataset_id, _table_name = catalog

    openstatspec.apply_spss_in_place(
        database_url=url,
        dataset_id=dataset_id,
        source_text="STRING note (A4).",
        actor="test-agent",
    )

    audit = sqlite3.connect(path).execute(
        "SELECT source_kind, frontend_contract FROM transformation_apply"
    ).fetchone()
    assert audit == (
        "spss_syntax",
        "openstatspec-spss-syntax-frontend-v0.3",
    )


@pytest.mark.parametrize("as_mapping", [False, True])
def test_public_generic_plan_apply_accepts_object_and_mapping(
    catalog, as_mapping,
) -> None:
    url, path, dataset_id, table_name = catalog
    plan = _plan("RECODE score (1 = 7).")
    supplied = plan.as_dict() if as_mapping else plan
    result = openstatspec.apply_transformation_plan_in_place(
        database_url=url,
        dataset_id=dataset_id,
        plan=supplied,
        actor="test-agent",
    )
    assert result["dataset_id"] == dataset_id
    assert result["physical_table_name"] == table_name
    assert result["source_kind"] == "canonical_plan"
    assert result["source_hash"] == result["plan_hash"] == plan.sha256()
    connection = sqlite3.connect(path)
    assert connection.execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(7.0,), (2.0,), (3.0,)]
    assert connection.execute(
        "SELECT source_kind, source_hash, frontend_contract, plan_hash "
        "FROM transformation_apply"
    ).fetchone() == (
        "canonical_plan",
        plan.sha256(),
        None,
        plan.sha256(),
    )


def test_generic_string_width_is_rejected_before_ddl(
    catalog, monkeypatch,
) -> None:
    url, path, dataset_id, table_name = catalog
    monkeypatch.setattr(
        inplace_transform,
        "effective_profile",
        lambda _url, **_kwargs: (
            replace(SQLITE, max_text_value_bytes=3),
            {"server_version": "3.35.0"},
        ),
    )
    plan = openstatspec.TransformationPlan(
        (openstatspec.CreateVariableOperation("note", "string", 4),),
        contract="openstatspec-transformation-plan-v0.3",
    )

    with pytest.raises(TargetCapabilityExceededError, match="permits 3"):
        openstatspec.apply_transformation_plan_in_place(
            database_url=url,
            dataset_id=dataset_id,
            plan=plan,
            actor="test-agent",
        )

    connection = sqlite3.connect(path)
    assert [
        row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    ] == ["__case_ordinal", "score"]
    assert connection.execute(
        "select source_name from variable where dataset_id = ?",
        (dataset_id,),
    ).fetchall() == [("score",)]
    assert connection.execute(
        "select count(*) from transformation_apply"
    ).fetchone() == (0,)
    connection.close()


def test_generic_plan_is_bound_to_live_schema_before_mutation(catalog) -> None:
    url, path, dataset_id, table_name = catalog
    plan = openstatspec.TransformationPlan((
        openstatspec.SetVariableLabelOperation("missing", "Must fail"),
    ))
    with pytest.raises(openstatspec.TransformationFrontendError) as caught:
        openstatspec.apply_transformation_plan_in_place(
            database_url=url,
            dataset_id=dataset_id,
            plan=plan,
            actor="test-agent",
        )
    assert caught.value.code == "unknown_variable"
    connection = sqlite3.connect(path)
    assert connection.execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0,), (2.0,), (3.0,)]
    assert connection.execute(
        "SELECT COUNT(*) FROM transformation_apply"
    ).fetchone() == (0,)


@pytest.mark.parametrize("as_mapping", [False, True])
def test_string_create_target_is_rejected_before_any_mutation(
    catalog, as_mapping,
) -> None:
    url, path, dataset_id, table_name = catalog
    plan = openstatspec.TransformationPlan((
        openstatspec.SetVariableLabelOperation("score", "Must roll back"),
        openstatspec.RecodeOperation(
            source="score",
            target="band",
            target_mode="create",
            rules=(
                openstatspec.RecodeRule(
                    openstatspec.RecodeMatch(
                        "values",
                        values=(openstatspec.TypedValue.binary64(1),),
                    ),
                    openstatspec.RecodeResult(
                        "literal",
                        openstatspec.TypedValue.string("low"),
                    ),
                ),
            ),
            unmatched=openstatspec.RecodeResult(
                "literal",
                openstatspec.TypedValue.string("other"),
            ),
        ),
    ))
    supplied = plan.as_dict() if as_mapping else plan
    with pytest.raises(openstatspec.TransformationError) as caught:
        openstatspec.apply_transformation_plan_in_place(
            database_url=url,
            dataset_id=dataset_id,
            plan=supplied,
            actor="test-agent",
        )
    assert caught.value.code == "in_place_target_type_unsupported"
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT variable_label FROM variable WHERE source_name = 'score'"
    ).fetchone() == ("Score",)
    assert "band" not in {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    }
    assert connection.execute(
        "SELECT COUNT(*) FROM transformation_apply"
    ).fetchone() == (0,)


def test_string_source_can_create_numeric_target(tmp_path) -> None:
    path = tmp_path / "string-source.sqlite"
    url = f"sqlite:///{path}"
    openstatspec.initialize_catalog(database_url=url)
    variables = _variables()
    variables[0].update({
        "source_name": "color",
        "physical_name": "color",
        "storage_kind": "string",
        "string_width": 8,
        "label": "Color",
        "format": "A8",
        "print_format": "[1, 8, 0]",
        "write_format": "[1, 8, 0]",
    })
    create_wide_dataset(
        database_url=url,
        dataset_id="string_source",
        source_name="source.sav",
        source_format="SAV",
        source_sha256="e" * 64,
        rows=[{"color": "R"}, {"color": "B"}],
        variables=variables,
    )
    openstatspec.install_in_place_transformation_schema(database_url=url)
    connection = sqlite3.connect(path)
    dataset_id, table_name = connection.execute(
        "SELECT dataset_id, physical_table_name FROM dataset"
    ).fetchone()
    plan = openstatspec.TransformationPlan((
        openstatspec.RecodeOperation(
            source="color",
            target="is_red",
            target_mode="create",
            rules=(
                openstatspec.RecodeRule(
                    openstatspec.RecodeMatch(
                        "values",
                        values=(openstatspec.TypedValue.string("R"),),
                    ),
                    openstatspec.RecodeResult(
                        "literal",
                        openstatspec.TypedValue.binary64(1),
                    ),
                ),
            ),
            unmatched=openstatspec.RecodeResult(
                "literal",
                openstatspec.TypedValue.binary64(0),
            ),
        ),
    ))
    openstatspec.apply_transformation_plan_in_place(
        database_url=url,
        dataset_id=dataset_id,
        plan=plan,
        actor="test-agent",
    )
    assert sqlite3.connect(path).execute(
        f'SELECT color, is_red FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [("R", 1.0), ("B", 0.0)]


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


def test_delete_is_rejected_when_drop_column_is_unavailable(
    catalog, monkeypatch,
) -> None:
    url, path, dataset_id, table_name = catalog
    monkeypatch.setattr(
        inplace_transform,
        "effective_profile",
        lambda _url, **_kwargs: (
            SQLITE,
            {"server_version": "3.34.0"},
        ),
    )

    with pytest.raises(openstatspec.TransformationError) as caught:
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text="COMPUTE other = score. DELETE VARIABLES score.",
            actor="test-agent",
        )

    assert caught.value.code == "delete_variable_not_supported"
    connection = sqlite3.connect(path)
    assert connection.execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == [(1.0,), (2.0,), (3.0,)]
    assert [
        row[1] for row in connection.execute(f'PRAGMA table_info("{table_name}")')
    ] == ["__case_ordinal", "score"]
    connection.close()


def test_nontransactional_ddl_profile_rejects_create_before_mutation(
    catalog,
) -> None:
    url, path, dataset_id, table_name = catalog
    engine = create_engine(url)
    with pytest.raises(openstatspec.TransformationError) as caught:
        with engine.begin() as connection:
            _apply_plan_on_connection(
                connection,
                dataset_id=dataset_id,
                submission=_submission(
                    "VARIABLE LABELS score 'Changed'. "
                    "RECODE score (1 = 0) INTO score_band."
                ),
                actor="test-agent",
                database_profile="mysql",
                target_profile=inplace_transform.effective_profile(url)[0],
                allow_schema_change=False,
                allow_delete_variable=True,
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
        lambda _url, **_kwargs: (DOLT, {}),
    )
    states = iter([
        ("feature/recode", "abc123", 0),
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
        lambda _url, **_kwargs: (DOLT, {}),
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
        lambda _url, **_kwargs: (DOLT, {}),
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

def test_schema_install_fails_before_engine_or_ddl_when_profile_is_rejected(
    monkeypatch,
) -> None:
    def reject_profile(_database_url, **_kwargs):
        raise openstatspec.UnsupportedOperationError("Dolt declaration mismatch")

    def unexpected_engine(_database_url):
        raise AssertionError("engine creation would permit DDL")

    monkeypatch.setattr(inplace_transform, "effective_profile", reject_profile)
    monkeypatch.setattr(inplace_transform, "create_engine", unexpected_engine)

    with pytest.raises(
        openstatspec.UnsupportedOperationError,
        match="Dolt declaration mismatch",
    ):
        inplace_transform.install_in_place_transformation_schema(
            database_url="mysql+pymysql://user@host/database",
        )

def test_schema_install_forwards_explicit_dolt_conformance_source(
    monkeypatch,
) -> None:
    sentinel = object()
    captured = []

    def capture_profile(_database_url, *, dolt_conformance_source):
        captured.append(dolt_conformance_source)
        raise openstatspec.UnsupportedOperationError("stop after gate")

    monkeypatch.setattr(inplace_transform, "effective_profile", capture_profile)

    with pytest.raises(openstatspec.UnsupportedOperationError, match="stop after gate"):
        inplace_transform.install_in_place_transformation_schema(
            database_url="mysql+pymysql://user@host/database",
            dolt_conformance_source=sentinel,
        )

    assert captured == [sentinel]


def test_plan_apply_forwards_explicit_dolt_conformance_source(monkeypatch) -> None:
    sentinel = object()
    captured = []

    def capture_submission(**kwargs):
        captured.append(kwargs["dolt_conformance_source"])
        return {"ok": True}

    monkeypatch.setattr(
        inplace_transform, "_run_in_place_submission", capture_submission,
    )

    result = inplace_transform.apply_transformation_plan_in_place(
        database_url="sqlite://",
        dataset_id="synthetic",
        plan=_plan("RECODE score (1 = 2)."),
        actor="test-agent",
        dolt_conformance_source=sentinel,
    )

    assert result == {"ok": True}
    assert captured == [sentinel]

def test_in_place_audit_relation_remains_catalog_owned(catalog) -> None:
    url, _path, dataset_id, _table_name = catalog

    dataset = openstatspec.get_dataset(
        database_url=url,
        dataset_id=dataset_id,
        kind="core",
    )

    assert dataset["dataset"]["dataset_id"] == dataset_id

def test_schema_install_requires_initialized_verified_catalog(tmp_path) -> None:
    path = tmp_path / "empty.sqlite"
    url = f"sqlite:///{path}"

    with pytest.raises(
        openstatspec.UnsupportedOperationError,
        match="catalog is absent",
    ):
        openstatspec.install_in_place_transformation_schema(database_url=url)

    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall() == []


def test_public_apply_rejects_divergent_catalog_before_mutation(catalog) -> None:
    url, path, dataset_id, table_name = catalog
    connection = sqlite3.connect(path)
    connection.execute("UPDATE catalog_identity SET schema_version = 999")
    connection.commit()
    before = connection.execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall()

    with pytest.raises(
        RuntimeError,
        match="identity is incompatible",
    ):
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text="RECODE score (1 = 9).",
            actor="test-agent",
        )

    assert connection.execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == before
    assert connection.execute(
        "SELECT COUNT(*) FROM transformation_apply"
    ).fetchone() == (0,)

def test_schema_installer_upgrades_legacy_apply_audit_columns(catalog) -> None:
    url, path, dataset_id, _table_name = catalog
    connection = sqlite3.connect(path)
    connection.execute(
        "ALTER TABLE transformation_apply DROP COLUMN source_kind"
    )
    connection.execute(
        "ALTER TABLE transformation_apply DROP COLUMN frontend_contract"
    )
    connection.commit()
    connection.close()

    openstatspec.install_in_place_transformation_schema(database_url=url)

    connection = sqlite3.connect(path)
    columns = {
        row[1] for row in connection.execute(
            "PRAGMA table_info(transformation_apply)"
        )
    }
    connection.close()
    assert {"source_kind", "frontend_contract"} <= columns
    assert openstatspec.get_dataset(
        database_url=url, dataset_id=dataset_id, kind="core",
    )["dataset"]["dataset_id"] == dataset_id

def test_public_apply_rejects_divergent_variable_mapping_before_mutation(
    catalog,
) -> None:
    url, path, dataset_id, table_name = catalog
    connection = sqlite3.connect(path)
    deleted = connection.execute("DELETE FROM variable")
    assert deleted.rowcount > 0
    connection.commit()
    before = connection.execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall()

    with pytest.raises(
        openstatspec.TransformationError,
        match="no variables",
    ):
        openstatspec.apply_spss_in_place(
            database_url=url,
            dataset_id=dataset_id,
            source_text="VARIABLE LABELS score 'Changed'.",
            actor="test-agent",
        )

    assert connection.execute(
        f'SELECT score FROM "{table_name}" ORDER BY __case_ordinal'
    ).fetchall() == before
    assert connection.execute(
        "SELECT COUNT(*) FROM transformation_apply"
    ).fetchone() == (0,)


@pytest.mark.parametrize("profile_name", ["sqlite", "postgresql"])
def test_transactional_ddl_rollback_skips_unlocked_compensation(
    tmp_path, monkeypatch, profile_name,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'transactional-rollback.sqlite'}"
    openstatspec.initialize_catalog(database_url=database_url)
    monkeypatch.setattr(
        inplace_transform,
        "effective_profile",
        lambda _url, **_kwargs: (
            SimpleNamespace(name=profile_name),
            {"server_version": "3.35.0"},
        ),
    )

    def fail_after_schema_change(
        _connection, *, mutation_journal, **_kwargs,
    ):
        mutation_journal["added_columns"] = ["score_band"]
        raise RuntimeError("simulated transactional failure")

    monkeypatch.setattr(
        inplace_transform, "_apply_plan_on_connection", fail_after_schema_change,
    )
    monkeypatch.setattr(
        inplace_transform,
        "_compensate_failed_apply",
        lambda *_args, **_kwargs: pytest.fail(
            "Transactional rollback must not run unlocked compensation"
        ),
    )

    with pytest.raises(RuntimeError, match="simulated transactional failure"):
        inplace_transform._run_in_place_submission(
            database_url=database_url,
            dataset_id="dataset",
            actor="test-agent",
            prepare=lambda _connection, _dataset_id: _submission(
                "RECODE score (1 = 0) INTO score_band."
            ),
        )
