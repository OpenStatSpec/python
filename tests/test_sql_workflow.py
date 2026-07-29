import hashlib
import json
from pathlib import Path

import rfc8785
import sqlite3
from uuid import UUID

import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

import openstatspec
import openstatspec.cli
from openstatspec.sql.workflow import (
    PROFILE_ID, PROFILE_SCHEMA_VERSION, TransformationError, _definition_hash,
    _assert_sqlite_server_version, _workflow_engine,
    transformation_capabilities, workflow_catalog,
)
from openstatspec.sql.wide import create_wide_dataset


def _variables():
    return [
        {
            "ordinal": 1, "source_name": "score", "physical_name": "score",
            "storage_kind": "numeric", "string_width": None, "label": "Score",
            "format": "F8.0", "print_format": "[5, 8, 0]",
            "write_format": "[5, 8, 0]", "measure": "scale", "role": "input",
            "alignment": "right", "display_width": 8, "attributes": "{}",
            "compat_name": None, "value_labels": "{}", "missing_ranges": "[]",
        },
        {
            "ordinal": 2, "source_name": "grp", "physical_name": "grp",
            "storage_kind": "string", "string_width": 8, "label": "Group",
            "format": "A8", "print_format": "[1, 8, 0]",
            "write_format": "[1, 8, 0]", "measure": "nominal", "role": "input",
            "alignment": "left", "display_width": 8, "attributes": "{}",
            "compat_name": None, "value_labels": "{}", "missing_ranges": "[]",
        },
    ]


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "workflow.sqlite"
    url = f"sqlite:///{path}"
    create_wide_dataset(
        database_url=url, dataset_id="source", source_name="source.sav",
        source_format="SAV", source_sha256="a" * 64,
        rows=[
            {"score": 1.0, "grp": "a"},
            {"score": 2.0, "grp": "b"},
            {"score": 3.0, "grp": "a"},
        ],
        case_weight_variable="score",
        variables=_variables(),
    )
    listed = openstatspec.list_datasets(database_url=url, kind="core")
    return url, path, listed["datasets"][0]["dataset_id"]


def _columns():
    return [
        {
            "name": "score", "storage_kind": "numeric", "source": "score",
            "expression_role": "identity", "label": "Score",
        },
        {
            "name": "grp", "storage_kind": "string", "source": "grp",
            "expression_role": "identity", "label": "Group",
        },
    ]


def test_materialized_sql_workflow_is_immutable_audited_and_queryable(catalog):
    url, path, parent_id = catalog
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql=(
            "SELECT score, grp FROM parent "
            "WHERE score >= :minimum ORDER BY score ASC NULLS LAST"
        ),
        columns=_columns(), parameters={"minimum": 2},
        transformation_name="filtered_scores", dataset_name="filtered",
    )
    UUID(result["derived_dataset_id"])
    UUID(result["transformation_run_id"])
    assert result["status"] == "succeeded"
    assert result["row_count"] == 2

    validated = openstatspec.validate_derived(
        database_url=url, derived_dataset_id=result["derived_dataset_id"],
    )
    assert validated["valid"] is True
    assert validated["row_count"] == 2
    assert validated["variable_count"] == 2

    listed = openstatspec.list_datasets(database_url=url)
    assert [(row["kind"], row["row_count"]) for row in listed["datasets"]] == [
        ("core", 3), ("derived", 2),
    ]
    shown = openstatspec.get_dataset(
        database_url=url, dataset_id=result["derived_dataset_id"], kind="derived",
    )
    assert shown["transformation_run"]["status"] == "succeeded"
    assert len(shown["lineage"]) == 2
    assert all(row["input_ordinal"] == 1 for row in shown["lineage"])

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select contract_id, core_contract_id from transformation_profile_identity"
    ).fetchone() == (
        PROFILE_ID, "openstatspec-strict-wide-table-v1",
    )
    run_row = connection.execute(
        "select status, parameters_hash, input_set_hash from transformation_run"
    ).fetchone()
    assert run_row[0] == "succeeded"
    envelope = connection.execute(
        "select parameter_ordinal, parameter_name, logical_type, value_envelope, "
        "value_hash from transformation_run_parameter"
    ).fetchone()
    assert envelope[:4] == (
        1, "minimum", "json", '{"t":"json","v":2}',
    )
    assert envelope[4] == hashlib.sha256(envelope[3].encode()).hexdigest()
    parameters_document = {
        "hash_kind": "parameter_set",
        "hash_version": "openstatspec-parameter-set-v1",
        "parameters": [{
            "parameter_ordinal": 1, "parameter_name": "minimum",
            "logical_type": "json", "value_envelope": {"t": "json", "v": 2},
        }],
    }
    assert run_row[1] == hashlib.sha256(
        rfc8785.dumps(parameters_document)
    ).hexdigest()
    assert len(run_row[2]) == 64
    relation = result["physical_relation_name"]
    assert connection.execute(
        f'select __row_ordinal, score, grp from "{relation}" order by __row_ordinal'
    ).fetchall() == [(1, 2.0, "b"), (2, 3.0, "a")]
    assert connection.execute("select count(*) from dataset").fetchone() == (1,)

    repeated = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql=(
            "SELECT score, grp FROM parent "
            "WHERE score >= :minimum ORDER BY score ASC NULLS LAST"
        ),
        columns=_columns(), parameters={"minimum": 2},
        transformation_name="filtered_scores",
    )
    assert repeated["status"] == "already_exists"
    assert repeated["derived_dataset_id"] == result["derived_dataset_id"]
    assert connection.execute("select count(*) from transformation_run").fetchone() == (1,)


def test_materialized_derived_parent_chain(catalog):
    url, _, parent_id = catalog
    materialized = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
            "expression_role": "identity",
        }],
        transformation_name="score_materialized",
    )
    chained = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=materialized["derived_dataset_id"],
        parent_kind="derived",
        query_sql="SELECT score FROM parent WHERE score > 1 ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
            "expression_role": "identity",
        }],
        transformation_name="score_chain",
    )
    assert chained["row_count"] == 2


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("DELETE FROM parent", "select_only"),
        ("SELECT score FROM dataset ORDER BY score ASC NULLS LAST", "undeclared_relation_access"),
        ("SELECT score FROM parent; DROP TABLE dataset", "unsafe_sql"),
        ("SELECT random() AS score FROM parent ORDER BY score ASC NULLS LAST", "volatile_sql"),
        ("SELECT score FROM parent", "deterministic_order_required"),
        ("SELECT score FROM parent -- comment\n ORDER BY score ASC NULLS LAST", "unsafe_sql"),
    ],
)
def test_sql_authoring_fails_closed(catalog, sql, code):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id, query_sql=sql,
            columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
            transformation_name="unsafe",
        )
    assert caught.value.code == code


def test_parameterized_view_is_rejected(catalog):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT score FROM parent WHERE score > :minimum ORDER BY score ASC NULLS LAST",
            columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
            output_mode="view", transformation_name="bad_view",
        )
    assert caught.value.code == "output_mode_not_supported"


def test_failed_run_is_audited_without_derived_or_physical_output(catalog):
    url, path, parent_id = catalog
    registered = openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql=(
            "SELECT grp AS expected FROM parent "
            "ORDER BY expected COLLATE BINARY ASC NULLS LAST"
        ),
        columns=[{"name": "expected", "storage_kind": "string", "source": "grp"}],
        transformation_name="shape_failure",
    )
    with pytest.raises(TransformationError) as caught:
        openstatspec.execute_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            transformation_version_id=registered["transformation_version_id"],
        )
    assert caught.value.code == "non_unique_order_key"
    connection = sqlite3.connect(path)
    assert connection.execute(
        "select status from transformation_run order by started_at desc limit 1"
    ).fetchone() == ("failed",)
    assert connection.execute("select count(*) from derived_dataset").fetchone() == (0,)
    assert connection.execute(
        "select event_code, execution_phase from transformation_event"
    ).fetchone() == ("non_unique_order_key", "query_validation")
    assert not [
        row for row in connection.execute(
            "select name from sqlite_master where type in ('table','view')"
        ) if row[0].startswith("derived_") and row[0] not in {
            "derived_dataset", "derived_variable", "derived_variable_lineage",
            "derived_dataset_weight_variable", "derived_dataset_disposition_event",
        }
    ]


def test_cli_catalog_and_derive_commands(catalog, capsys):
    url, _, parent_id = catalog
    assert openstatspec.cli.main([
        "catalog-list", "--database-url", url, "--kind", "core",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1

    assert openstatspec.cli.main([
        "derive", "--database-url", url, "--parent-dataset-id", parent_id,
        "--sql", "SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        "--columns-json", '[{"name":"score","storage_kind":"numeric","source":"score"}]',
        "--name", "cli_score",
    ]) == 0
    derived = json.loads(capsys.readouterr().out)
    assert derived["status"] == "succeeded"
    assert openstatspec.cli.main([
        "validate-derived", "--database-url", url,
        "--derived-dataset-id", derived["derived_dataset_id"],
    ]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_workflow_catalog_compiles_for_the_supported_sqlite_backend():
    tables = workflow_catalog(MetaData())
    ddl = [str(CreateTable(table).compile(dialect=sqlite.dialect())) for table in tables.all()]
    assert len(ddl) == 13
    assert all("CREATE TABLE" in statement for statement in ddl)


def test_stable_definition_publishes_monotonic_versions(catalog):
    url, _, parent_id = catalog
    first = openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
        transformation_name="versioned",
    )
    second = openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent WHERE score > 1 ORDER BY score ASC NULLS LAST",
        columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
        transformation_name="versioned",
    )
    assert first["transformation_id"] == second["transformation_id"]
    assert first["version_number"] == 1
    assert second["version_number"] == 2
    assert first["transformation_version_id"] != second["transformation_version_id"]


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("SELECT grp FROM parent ORDER BY grp COLLATE BINARY ASC NULLS LAST", "non_unique_order_key"),
        (
            "SELECT CASE WHEN score = 1 THEN NULL ELSE score END AS score "
            "FROM parent ORDER BY score ASC NULLS LAST",
            "null_order_key",
        ),
    ],
)
def test_order_key_is_runtime_validated_before_publication(catalog, query, code):
    url, path, parent_id = catalog
    with pytest.raises(TransformationError) as caught:
        openstatspec.derive_sql_dataset(
            database_url=url, parent_dataset_id=parent_id, query_sql=query,
            columns=[{
                "name": "grp" if "grp" in query else "score",
                "storage_kind": "string" if "grp" in query else "numeric",
                "lineage_kind": "identity" if "grp" in query else "computed",
                "lineage": [{
                    "input_alias": "parent",
                    "parent_column": "grp" if "grp" in query else "score",
                    "expression_role": (
                        "identity" if "grp" in query else "contributing"
                    ),
                }],
            }],
            transformation_name=f"order_{code}",
        )
    assert caught.value.code == code
    connection = sqlite3.connect(path)
    assert connection.execute(
        "select status from transformation_run order by started_at desc limit 1"
    ).fetchone() == ("failed",)
    assert connection.execute("select count(*) from derived_dataset").fetchone() == (0,)


def test_definition_hash_tampering_fails_closed(catalog):
    url, path, parent_id = catalog
    registered = openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
        transformation_name="tamper",
    )
    connection = sqlite3.connect(path)
    forged_id = str(UUID(int=98))
    connection.execute(
        "insert into transformation_version ("
        "transformation_version_id, transformation_id, version_number, query_sql, "
        "dialect_family, server_version_constraint, output_mode, row_semantics, "
        "metadata_policy, deterministic_order_json, output_schema_json, "
        "definition_hash, published_at) "
        "select ?, transformation_id, version_number + 1, ?, dialect_family, "
        "server_version_constraint, output_mode, row_semantics, metadata_policy, "
        "deterministic_order_json, output_schema_json, ?, published_at "
        "from transformation_version where transformation_version_id = ?",
        (
            forged_id,
            "SELECT grp FROM parent ORDER BY grp COLLATE BINARY ASC NULLS LAST",
            "0" * 64,
            registered["transformation_version_id"],
        ),
    )
    connection.commit()
    with pytest.raises(TransformationError) as caught:
        openstatspec.execute_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            transformation_version_id=forged_id,
        )
    assert caught.value.code == "definition_hash_mismatch"
    assert connection.execute("select count(*) from transformation_run").fetchone() == (0,)


def test_lineage_and_external_io_fail_preflight(catalog):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as lineage:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{
                "name": "score", "storage_kind": "numeric",
                "source": "missing_parent_column",
            }],
            transformation_name="bad_lineage",
        )
    assert lineage.value.code == "invalid_lineage"

    with pytest.raises(TransformationError) as external:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT readfile('/etc/passwd') AS score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{"name": "score", "storage_kind": "string"}],
            transformation_name="external_io",
        )
    assert external.value.code == "unsafe_sql"


def test_run_input_records_canonical_relation_snapshot_envelope(catalog):
    url, path, parent_id = catalog
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
        transformation_name="snapshot",
    )
    connection = sqlite3.connect(path)
    row = connection.execute(
        "select input_alias, input_kind, core_dataset_id, derived_dataset_id, "
        "physical_relation_schema_snapshot, physical_relation_name_snapshot, "
        "snapshot_hash_kind, snapshot_hash_algorithm, snapshot_hash_version, "
        "length(content_or_source_hash) from transformation_run_input "
        "where transformation_run_id = ?",
        (result["transformation_run_id"],),
    ).fetchone()
    assert row == (
        "parent", "core", parent_id, None, None, "data_source",
        "relation_snapshot", "sha256", "openstatspec-relation-snapshot-v1", 64,
    )


def test_canonical_json_is_rfc8785_not_python_sort_keys():
    from openstatspec.sql.workflow import _canonical_json

    assert _canonical_json({
        "number": 333333333.33333329,
        "control": "\u000f",
    }) == '{"control":"\\u000f","number":333333333.3333333}'


def test_append_only_retire_remove_and_reconcile_protocol(catalog):
    url, path, parent_id = catalog
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
        transformation_name="disposition",
    )
    assert openstatspec.retire_derived(
        database_url=url, derived_dataset_id=result["derived_dataset_id"],
        actor_identity="test", reason="superseded",
    )["status"] == "retired"
    assert openstatspec.remove_derived_physical_relation(
        database_url=url, derived_dataset_id=result["derived_dataset_id"],
        actor_identity="test", reason="retention expired",
    )["status"] == "physical_removed"

    connection = sqlite3.connect(path)
    assert connection.execute(
        "select event_kind from derived_dataset_disposition_event "
        "order by event_ordinal"
    ).fetchall() == [
        ("retired",), ("physical_removal_requested",), ("physical_removed",),
    ]
    assert connection.execute(
        "select count(*) from derived_dataset where derived_dataset_id = ?",
        (result["derived_dataset_id"],),
    ).fetchone() == (1,)
    assert connection.execute(
        "select count(*) from sqlite_master where name = ?",
        (result["physical_relation_name"],),
    ).fetchone() == (0,)
    assert openstatspec.reconcile_derived_removals(
        database_url=url,
    )["reconciled"] == 0


def test_sqlite_is_the_only_advertised_and_executable_workflow_backend():
    sqlite_capabilities = openstatspec.capability_matrix()["optional_profiles"][
        "sql_transformation_workflow"
    ]
    assert sqlite_capabilities["supported_dialect_families"] == ["sqlite"]
    postgres_url = "postgresql+psycopg://user:password@localhost/database"
    unavailable = transformation_capabilities(postgres_url)
    assert unavailable["status"] == "unsupported"
    assert unavailable["supported"] == {}
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=postgres_url,
            parent_dataset_id="11111111-1111-4111-8111-111111111111",
            query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{
                "name": "score", "storage_kind": "numeric", "source": "score",
            }],
            transformation_name="postgres_is_not_claimed",
        )
    assert caught.value.code == "dialect_not_supported"


def test_sqlite_foreign_keys_are_enabled_and_publication_is_fk_safe(catalog):
    url, _, parent_id = catalog
    engine = _workflow_engine(url, "sqlite")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    with engine.begin() as connection:
        connection.exec_driver_sql("SELECT 1")
        assert connection.connection.driver_connection.in_transaction is True
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
        }], transformation_name="foreign_key_publication",
    )
    assert result["status"] == "succeeded"


def test_scope_aware_ctes_merge_with_parent_and_shadowing_fails(catalog):
    url, _, parent_id = catalog
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql=(
            "WITH selected AS (SELECT score FROM parent WHERE score > 1) "
            "SELECT score FROM selected ORDER BY score ASC NULLS LAST"
        ),
        columns=[{
            "name": "score", "storage_kind": "numeric",
            "lineage_kind": "computed", "lineage": [{
                "input_alias": "parent", "parent_column": "score",
                "expression_role": "contributing",
            }],
        }], transformation_name="leading_cte",
    )
    assert result["row_count"] == 2
    for sql in (
        "WITH selected AS (SELECT score FROM other) SELECT score FROM selected ORDER BY score ASC NULLS LAST",
        "WITH parent AS (SELECT score FROM other) SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        "SELECT score FROM main.parent ORDER BY score ASC NULLS LAST",
    ):
        with pytest.raises(TransformationError) as caught:
            openstatspec.register_sql_transformation(
                database_url=url, parent_dataset_id=parent_id, query_sql=sql,
                columns=[{
                    "name": "score", "storage_kind": "numeric", "source": "score",
                }], transformation_name="scope_rejected",
            )
        assert caught.value.code == "undeclared_relation_access"


def test_definition_hash_matches_every_normative_conformance_vector():
    manifest = json.loads((
        Path(__file__).resolve().parents[2]
        / "specification/conformance/sql-transformation-workflow-0.1.json"
    ).read_text(encoding="utf-8"))
    for case in manifest["cases"]:
        actual = _definition_hash(
            transformation_id=case["transformation_id"],
            version_number=case["version_number"],
            query_sql=case["query_sql"].replace("\r\n", "\n").replace("\r", "\n"),
            dialect_family=case["dialect_family"],
            server_version_constraint=case["server_version_constraint"],
            output_mode=case["output_mode"],
            row_semantics=case["row_semantics"],
            metadata_policy=case["metadata_policy"],
            output_schema=case["declared_output_schema"],
            deterministic_order=case["order_key"],
            parameter_declarations=case["parameter_declarations"],
        )
        assert actual == case["expected"]["definition_hash"], case["id"]


def test_query_sql_is_lf_normalized_without_trimming(catalog):
    url, path, parent_id = catalog
    query = "  SELECT score FROM parent ORDER BY score ASC NULLS LAST\r\n"
    registered = openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id, query_sql=query,
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
        }], transformation_name="exact_sql_bytes",
    )
    stored = sqlite3.connect(path).execute(
        "select query_sql, deterministic_order_json from transformation_version "
        "where transformation_version_id = ?",
        (registered["transformation_version_id"],),
    ).fetchone()
    assert stored[0] == "  SELECT score FROM parent ORDER BY score ASC NULLS LAST\n"
    assert json.loads(stored[1]) == [{
        "expression": "score", "direction": "ASC", "nulls": "LAST",
        "collation": None,
    }]


def test_existing_workflow_identity_or_schema_drift_fails_closed(catalog):
    url, path, parent_id = catalog
    openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
        }], transformation_name="schema_owner",
    )
    connection = sqlite3.connect(path)
    connection.execute("alter table transformation_event add column drift text")
    connection.commit()
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{
                "name": "score", "storage_kind": "numeric", "source": "score",
            }], transformation_name="schema_drift",
        )
    assert caught.value.code == "profile_incompatible"


def test_only_unchanged_materialized_derived_datasets_can_be_parents(catalog):
    url, path, parent_id = catalog
    with pytest.raises(TransformationError) as view_error:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{
                "name": "score", "storage_kind": "numeric", "source": "score",
            }], output_mode="view", transformation_name="parent_view",
        )
    assert view_error.value.code == "output_mode_not_supported"
    materialized = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
        }], transformation_name="tampered_parent",
    )
    relation = materialized["physical_relation_name"]
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f'update "{relation}" set score = 99 where __row_ordinal = 1')
    connection.rollback()
    trigger_stem = (
        "oss_derived_relation_"
        + materialized["derived_dataset_id"].replace("-", "")
    )
    for operation in ("insert", "update", "delete"):
        connection.execute(f'drop trigger "{trigger_stem}_no_{operation}"')
    connection.execute(f'update "{relation}" set score = 99 where __row_ordinal = 1')
    connection.commit()
    with pytest.raises(TransformationError) as hash_error:
        openstatspec.derive_sql_dataset(
            database_url=url, parent_dataset_id=materialized["derived_dataset_id"],
            parent_kind="derived",
            query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{
                "name": "score", "storage_kind": "numeric", "source": "score",
            }], transformation_name="tampered_child",
        )
    assert hash_error.value.code == "derived_corrupt"


def test_computed_lineage_requires_and_publishes_all_contributors(catalog):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as missing:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT score + 1 AS score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{
                "name": "score", "storage_kind": "numeric",
                "lineage_kind": "computed", "lineage": [],
            }], transformation_name="missing_contributor",
        )
    assert missing.value.code == "invalid_lineage"
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score + length(grp) AS score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric",
            "lineage_kind": "computed", "lineage": [
                {"input_alias": "parent", "parent_column": "score", "expression_role": "contributing"},
                {"input_alias": "parent", "parent_column": "grp", "expression_role": "contributing"},
            ],
        }], transformation_name="all_contributors",
    )
    shown = openstatspec.get_dataset(
        database_url=url, dataset_id=result["derived_dataset_id"], kind="derived",
    )
    assert len(shown["lineage"]) == 2


def test_declared_non_null_output_is_enforced_and_audit_is_redacted(catalog):
    url, path, parent_id = catalog
    registered = openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql=(
            "SELECT score, CASE WHEN score = 1 THEN NULL ELSE grp END AS grp "
            "FROM parent ORDER BY score ASC NULLS LAST"
        ),
        columns=[
            {"name": "score", "storage_kind": "numeric", "source": "score"},
            {
                "name": "grp", "storage_kind": "string", "is_nullable": False,
            "lineage_kind": "computed", "lineage": [{
                "input_alias": "parent", "parent_column": "score",
                "expression_role": "contributing",
                }, {
                    "input_alias": "parent", "parent_column": "grp",
                    "expression_role": "contributing",
            }],
            },
        ], transformation_name="nonnull_output",
    )
    with pytest.raises(TransformationError) as caught:
        openstatspec.execute_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            transformation_version_id=registered["transformation_version_id"],
        )
    assert caught.value.code == "output_validation_failed"
    event = sqlite3.connect(path).execute(
        "select event_code, execution_phase, safe_detail_json from transformation_event"
    ).fetchone()
    assert event[:2] == ("output_validation_failed", "query_validation")
    safe = json.loads(event[2])
    assert set(safe) == {"error_code", "execution_phase", "correlation_id_hash"}
    assert "SELECT" not in event[2]


def test_started_run_and_reserved_staging_are_reconciled(catalog):
    url, path, parent_id = catalog
    registered = openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
        }], transformation_name="interrupted",
    )
    run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    connection = sqlite3.connect(path)
    connection.execute("pragma foreign_keys = on")
    connection.execute(
        "insert into transformation_run values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, registered["transformation_version_id"], "started", "test",
            run_id, "sqlite", "test", "sqlite", "{}", "test",
            registered["definition_hash"], "0" * 64, "1" * 64,
            "2000-01-01 00:00:00", None,
        ),
    )
    connection.execute(
        'create table "__oss_stage_aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa" (x integer)'
    )
    connection.commit()
    reconciled = openstatspec.reconcile_sql_transformation_runs(database_url=url)
    assert reconciled["reconciled"] == 1
    assert connection.execute(
        "select status from transformation_run where transformation_run_id = ?", (run_id,)
    ).fetchone() == ("failed",)
    assert connection.execute(
        "select count(*) from sqlite_master where name like '__oss_stage_%'"
    ).fetchone() == (0,)


def test_weight_propagation_requires_verified_identity_and_safe_rows(catalog):
    url, _, parent_id = catalog
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent WHERE score > 1 ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
        }], weight_variable="score", row_semantics="filter",
        transformation_name="verified_weight",
    )
    shown = openstatspec.get_dataset(
        database_url=url, dataset_id=result["derived_dataset_id"], kind="derived",
    )
    assert shown["weight_variable_id"] is not None
    with pytest.raises(TransformationError) as aggregate:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{
                "name": "score", "storage_kind": "numeric", "source": "score",
            }], weight_variable="score", row_semantics="aggregate",
            transformation_name="unsafe_weight_rows",
        )
    assert aggregate.value.code == "invalid_row_semantics"
    with pytest.raises(TransformationError) as wrong_source:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql=(
                "SELECT score, grp FROM parent "
                "ORDER BY score ASC NULLS LAST"
            ),
            columns=[
                {"name": "score", "storage_kind": "numeric", "source": "score"},
                {"name": "grp", "storage_kind": "string", "source": "grp"},
            ], weight_variable="grp", row_semantics="one_to_one",
            transformation_name="wrong_weight_source",
        )
    assert wrong_source.value.code == "invalid_weight"


def test_parameters_come_from_ast_and_hash_domain_fails_closed(catalog):
    url, _, parent_id = catalog
    literal = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql=(
            "SELECT score FROM parent WHERE grp <> ':not_a_parameter' "
            "ORDER BY score ASC NULLS LAST"
        ),
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
        }], transformation_name="literal_colon",
    )
    assert literal["row_count"] == 3
    registered = openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql=(
            "SELECT score FROM parent WHERE score > :minimum "
            "ORDER BY score ASC NULLS LAST"
        ),
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
        }], transformation_name="fractional_parameter",
    )
    with pytest.raises(TransformationError) as fractional:
        openstatspec.execute_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            transformation_version_id=registered["transformation_version_id"],
            parameters={"minimum": 1.5},
        )
    assert fractional.value.code == "parameter_type"


def test_order_uniqueness_uses_the_declared_collation(catalog):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as caught:
        openstatspec.derive_sql_dataset(
            database_url=url, parent_dataset_id=parent_id,
            query_sql=(
                "SELECT CASE WHEN score = 1 THEN 'a' WHEN score = 2 THEN 'A' "
                "ELSE 'b' END AS grp FROM parent "
                "ORDER BY grp COLLATE NOCASE ASC NULLS LAST"
            ),
            columns=[{
                "name": "grp", "storage_kind": "string",
                "lineage_kind": "computed", "lineage": [{
                    "input_alias": "parent", "parent_column": "score",
                    "expression_role": "contributing",
                }],
            }], transformation_name="nocase_tie",
        )
    assert caught.value.code == "non_unique_order_key"


def test_identity_weight_and_metadata_policies_are_ast_proven(catalog):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as rescaled:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql=(
                "SELECT score * 2 AS score FROM parent "
                "ORDER BY score ASC NULLS LAST"
            ),
            columns=[{
                "name": "score", "storage_kind": "numeric", "source": "score",
            }], weight_variable="score", row_semantics="one_to_one",
            transformation_name="rescaled_identity_weight",
        )
    assert rescaled.value.code == "invalid_lineage"
    with pytest.raises(TransformationError) as none_policy:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{
                "name": "score", "storage_kind": "numeric", "source": "score",
                "metadata": {"variable_label": "Forbidden"},
            }], metadata_policy="none", transformation_name="metadata_none",
        )
    assert none_policy.value.code == "invalid_metadata_policy"
    with pytest.raises(TransformationError) as identity_only:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql=(
                "SELECT score + 1 AS score FROM parent "
                "ORDER BY score ASC NULLS LAST"
            ),
            columns=[{
                "name": "score", "storage_kind": "numeric",
                "lineage_kind": "computed",
                "lineage": [{
                    "input_alias": "parent", "parent_column": "score",
                    "expression_role": "contributing",
                }],
                "metadata": {"variable_label": "Not passthrough"},
            }], metadata_policy="identity_only",
            transformation_name="metadata_identity_only",
        )
    assert identity_only.value.code == "invalid_metadata_policy"


def test_sqlite_triggers_enforce_append_only_rows_and_run_transitions(catalog):
    url, path, parent_id = catalog
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{
            "name": "score", "storage_kind": "numeric", "source": "score",
        }], transformation_name="trigger_guards",
    )
    connection = sqlite3.connect(path)
    connection.execute("pragma foreign_keys = on")
    assert connection.execute(
        "select count(*) from sqlite_master where type = 'trigger' and name like 'oss_%'"
    ).fetchone() == (29,)
    relation = result["physical_relation_name"]
    for statement in (
        f'insert into "{relation}" (__row_ordinal, score) values (4, 4)',
        f'update "{relation}" set score = 4 where __row_ordinal = 1',
        f'delete from "{relation}" where __row_ordinal = 1',
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)
        connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "update transformation_run set status = 'failed' "
            "where transformation_run_id = ?",
            (result["transformation_run_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "delete from derived_dataset where derived_dataset_id = ?",
            (result["derived_dataset_id"],),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("update transformation_version set version_number = 99")


def test_projection_lineage_exactly_matches_referenced_columns(catalog):
    url, _, parent_id = catalog
    cases = [
        ({
            "name": "score", "storage_kind": "numeric",
            "lineage_kind": "computed", "lineage": [{
                "input_alias": "parent", "parent_column": "score",
                "expression_role": "contributing",
            }],
        }, "SELECT score + length(grp) AS score FROM parent ORDER BY score ASC NULLS LAST"),
        ({
            "name": "score", "storage_kind": "numeric",
            "lineage_kind": "computed", "lineage": [
                {"input_alias": "parent", "parent_column": "score", "expression_role": "contributing"},
                {"input_alias": "parent", "parent_column": "grp", "expression_role": "contributing"},
            ],
        }, "SELECT score + 1 AS score FROM parent ORDER BY score ASC NULLS LAST"),
        ({
            "name": "score", "storage_kind": "numeric",
            "lineage_kind": "constant", "lineage": [],
        }, "SELECT score AS score FROM parent ORDER BY score ASC NULLS LAST"),
    ]
    for index, (column, query) in enumerate(cases):
        with pytest.raises(TransformationError) as caught:
            openstatspec.register_sql_transformation(
                database_url=url, parent_dataset_id=parent_id,
                query_sql=query, columns=[column],
                transformation_name=f"lineage_mismatch_{index}",
            )
        assert caught.value.code == "invalid_lineage"


def test_weight_rejects_aggregate_without_group(catalog):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql=(
                "SELECT score, COUNT(grp) AS n FROM parent "
                "ORDER BY score ASC NULLS LAST"
            ),
            columns=[
                {"name": "score", "storage_kind": "numeric", "source": "score"},
                {
                    "name": "n", "storage_kind": "numeric",
                    "lineage_kind": "aggregate", "lineage": [{
                        "input_alias": "parent", "parent_column": "grp",
                        "expression_role": "contributing",
                    }],
                },
            ],
            weight_variable="score", row_semantics="aggregate",
            transformation_name="aggregate_without_group_weight",
        )
    assert caught.value.code == "invalid_weight"


def test_already_exists_revalidates_disposition_and_physical_state(catalog):
    url, path, parent_id = catalog
    arguments = dict(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
        transformation_name="revalidate_existing",
    )
    result = openstatspec.derive_sql_dataset(**arguments)
    openstatspec.retire_derived(
        database_url=url, derived_dataset_id=result["derived_dataset_id"],
        actor_identity="test", reason="closure test",
    )
    with pytest.raises(TransformationError) as unavailable:
        openstatspec.derive_sql_dataset(**arguments)
    assert unavailable.value.code == "derived_unavailable"

    corrupt_arguments = {**arguments, "transformation_name": "revalidate_corrupt"}
    corrupt = openstatspec.derive_sql_dataset(**corrupt_arguments)
    relation = corrupt["physical_relation_name"]
    connection = sqlite3.connect(path)
    stem = "oss_derived_relation_" + corrupt["derived_dataset_id"].replace("-", "")
    for operation in ("insert", "update", "delete"):
        connection.execute(f'drop trigger "{stem}_no_{operation}"')
    connection.execute(f'update "{relation}" set score = 99 where __row_ordinal = 1')
    connection.commit()
    with pytest.raises(TransformationError) as corrupt_error:
        openstatspec.derive_sql_dataset(**corrupt_arguments)
    assert corrupt_error.value.code == "derived_corrupt"

    missing_arguments = {**arguments, "transformation_name": "revalidate_missing"}
    missing = openstatspec.derive_sql_dataset(**missing_arguments)
    connection.execute(f'drop table "{missing["physical_relation_name"]}"')
    connection.commit()
    with pytest.raises(TransformationError) as missing_error:
        openstatspec.derive_sql_dataset(**missing_arguments)
    assert missing_error.value.code == "derived_unavailable"


def test_static_trigger_body_drift_fails_closed(catalog):
    url, path, parent_id = catalog
    openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
        transformation_name="trigger_body_baseline",
    )
    connection = sqlite3.connect(path)
    connection.execute('drop trigger "oss_transformation_definition_no_update"')
    connection.execute(
        'create trigger "oss_transformation_definition_no_update" '
        'before update on "transformation_definition" begin select 1; end'
    )
    connection.commit()
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT grp FROM parent ORDER BY grp COLLATE BINARY ASC NULLS LAST",
            columns=[{"name": "grp", "storage_kind": "string", "source": "grp"}],
            transformation_name="trigger_body_drift",
        )
    assert caught.value.code == "profile_incompatible"


def test_server_version_constraint_is_exact_and_runtime_enforced(catalog):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as invalid:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
            columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
            server_version_constraint=">=3.0",
            transformation_name="invalid_server_range",
        )
    assert invalid.value.code == "server_version_constraint_invalid"

    class Result:
        @staticmethod
        def scalar_one():
            return "3.34.9"

    class Connection:
        @staticmethod
        def exec_driver_sql(_statement):
            return Result()

    with pytest.raises(TransformationError) as unsupported:
        _assert_sqlite_server_version(Connection(), "supported-profile")
    assert unsupported.value.code == "server_version_unsupported"


def test_normative_grouped_aggregate_lineage_executes(catalog):
    url, path, parent_id = catalog
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql=(
            "SELECT grp, CAST(COUNT(score) AS REAL) AS unweighted_n, "
            "SUM(score) AS total_score FROM parent GROUP BY grp "
            "ORDER BY grp COLLATE BINARY ASC NULLS LAST"
        ),
        columns=[
            {
                "name": "grp", "storage_kind": "string", "is_nullable": False,
                "lineage_kind": "identity", "lineage": [{
                    "input_alias": "parent", "parent_column": "grp",
                    "expression_role": "grouping",
                }],
            },
            {
                "name": "unweighted_n", "storage_kind": "numeric",
                "lineage_kind": "aggregate", "lineage": [{
                    "input_alias": "parent", "parent_column": "score",
                    "expression_role": "contributing",
                }],
            },
            {
                "name": "total_score", "storage_kind": "numeric",
                "lineage_kind": "aggregate", "lineage": [{
                    "input_alias": "parent", "parent_column": "score",
                    "expression_role": "contributing",
                }],
            },
        ],
        row_semantics="aggregate", transformation_name="normative_aggregate",
    )
    relation = result["physical_relation_name"]
    assert sqlite3.connect(path).execute(
        f'select grp, unweighted_n, total_score from "{relation}" order by __row_ordinal'
    ).fetchall() == [("a", 2.0, 4.0), ("b", 1.0, 2.0)]


def test_lineage_kind_and_roles_must_match_ast_semantics(catalog):
    url, _, parent_id = catalog
    cases = [
        (
            "SELECT COUNT(score) AS score FROM parent ORDER BY score ASC NULLS LAST",
            {
                "name": "score", "storage_kind": "numeric",
                "lineage_kind": "computed", "lineage": [{
                    "input_alias": "parent", "parent_column": "score",
                    "expression_role": "contributing",
                }],
            },
            "invalid_lineage",
        ),
        (
            "SELECT score + 1 AS score FROM parent ORDER BY score ASC NULLS LAST",
            {
                "name": "score", "storage_kind": "numeric",
                "lineage_kind": "aggregate", "lineage": [{
                    "input_alias": "parent", "parent_column": "score",
                    "expression_role": "contributing",
                }],
            },
            "invalid_lineage",
        ),
        (
            "SELECT score FROM parent ORDER BY score ASC NULLS LAST",
            {
                "name": "score", "storage_kind": "numeric",
                "lineage_kind": "identity", "lineage": [{
                    "input_alias": "parent", "parent_column": "score",
                    "expression_role": "grouping",
                }],
            },
            "invalid_expression_role",
        ),
    ]
    for index, (query, column, code) in enumerate(cases):
        with pytest.raises(TransformationError) as caught:
            openstatspec.register_sql_transformation(
                database_url=url, parent_dataset_id=parent_id,
                query_sql=query, columns=[column],
                transformation_name=f"semantic_mislabel_{index}",
            )
        assert caught.value.code == code

    with pytest.raises(TransformationError) as ordering:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql=(
                "SELECT score, grp FROM parent "
                "ORDER BY score ASC NULLS LAST"
            ),
            columns=[
                {"name": "score", "storage_kind": "numeric", "source": "score"},
                {
                    "name": "grp", "storage_kind": "string",
                    "lineage_kind": "identity", "lineage": [{
                        "input_alias": "parent", "parent_column": "grp",
                        "expression_role": "ordering",
                    }],
                },
            ], transformation_name="false_ordering_role",
        )
    assert ordering.value.code == "invalid_expression_role"


def test_validate_derived_requires_exact_relation_triggers(catalog):
    url, path, parent_id = catalog
    result = openstatspec.derive_sql_dataset(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
        transformation_name="validate_relation_trigger",
    )
    stem = "oss_derived_relation_" + result["derived_dataset_id"].replace("-", "")
    connection = sqlite3.connect(path)
    connection.execute(f'drop trigger "{stem}_no_update"')
    connection.commit()
    with pytest.raises(TransformationError) as caught:
        openstatspec.validate_derived(
            database_url=url, derived_dataset_id=result["derived_dataset_id"]
        )
    assert caught.value.code == "derived_corrupt"


@pytest.mark.parametrize(("table", "old_type", "new_type"), [
    ("transformation_definition", "transformation_id VARCHAR(36)", "transformation_id TEXT"),
    ("transformation_version", "version_number INTEGER", "version_number TEXT"),
    ("derived_dataset", "row_count BIGINT", "row_count TEXT"),
    ("transformation_definition", "created_at DATETIME", "created_at TEXT"),
])
def test_workflow_schema_rejects_type_drift(
    catalog, table, old_type, new_type,
):
    url, path, parent_id = catalog
    openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql="SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
        transformation_name="type_drift_baseline",
    )
    connection = sqlite3.connect(path)
    ddl = connection.execute(
        "select sql from sqlite_schema where type = 'table' and name = ?", (table,)
    ).fetchone()[0]
    assert old_type in ddl
    connection.execute("pragma writable_schema = on")
    connection.execute(
        "update sqlite_schema set sql = ? where type = 'table' and name = ?",
        (ddl.replace(old_type, new_type, 1), table),
    )
    version = connection.execute("pragma schema_version").fetchone()[0]
    connection.execute(f"pragma schema_version = {version + 1}")
    connection.execute("pragma writable_schema = off")
    connection.commit()
    connection.close()
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql="SELECT grp FROM parent ORDER BY grp COLLATE BINARY ASC NULLS LAST",
            columns=[{"name": "grp", "storage_kind": "string", "source": "grp"}],
            transformation_name="type_drift_detected",
        )
    assert caught.value.code == "profile_incompatible"


@pytest.mark.parametrize("aggregate_expression", ["COUNT(*)", "COUNT(1)"])
def test_relation_level_aggregate_lineage_fails_closed(
    catalog, aggregate_expression,
):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql=(
                f"SELECT {aggregate_expression} AS n FROM parent "
                "ORDER BY n ASC NULLS LAST"
            ),
            columns=[{
                "name": "n", "storage_kind": "numeric",
                "lineage_kind": "aggregate", "lineage": [],
            }],
            row_semantics="aggregate",
            transformation_name=f"unrepresentable_{aggregate_expression}",
        )
    assert caught.value.code == "aggregate_lineage_unrepresentable"


@pytest.mark.parametrize(("query", "row_semantics"), [
    (
        "SELECT score FROM parent ORDER BY score ASC NULLS LAST",
        "filter",
    ),
    (
        "SELECT score FROM parent WHERE score > 1 ORDER BY score ASC NULLS LAST",
        "one_to_one",
    ),
])
def test_simple_and_filter_row_semantics_mislabels_fail(
    catalog, query, row_semantics,
):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql=query,
            columns=[{"name": "score", "storage_kind": "numeric", "source": "score"}],
            row_semantics=row_semantics,
            transformation_name=f"row_mislabel_{row_semantics}",
        )
    assert caught.value.code == "invalid_row_semantics"


def test_column_aggregate_rejects_filter_row_semantics(catalog):
    url, _, parent_id = catalog
    with pytest.raises(TransformationError) as caught:
        openstatspec.register_sql_transformation(
            database_url=url, parent_dataset_id=parent_id,
            query_sql=(
                "SELECT COUNT(score) AS n FROM parent "
                "ORDER BY n ASC NULLS LAST"
            ),
            columns=[{
                "name": "n", "storage_kind": "numeric",
                "lineage_kind": "aggregate", "lineage": [{
                    "input_alias": "parent", "parent_column": "score",
                    "expression_role": "contributing",
                }],
            }],
            row_semantics="filter", transformation_name="aggregate_as_filter",
        )
    assert caught.value.code == "invalid_row_semantics"


def test_column_aggregate_with_aggregate_semantics_succeeds(catalog):
    url, _, parent_id = catalog
    registered = openstatspec.register_sql_transformation(
        database_url=url, parent_dataset_id=parent_id,
        query_sql=(
            "SELECT COUNT(score) AS n FROM parent "
            "ORDER BY n ASC NULLS LAST"
        ),
        columns=[{
            "name": "n", "storage_kind": "numeric",
            "lineage_kind": "aggregate", "lineage": [{
                "input_alias": "parent", "parent_column": "score",
                "expression_role": "contributing",
            }],
        }],
        row_semantics="aggregate", transformation_name="column_aggregate_valid",
    )
    assert registered["version_number"] == 1
