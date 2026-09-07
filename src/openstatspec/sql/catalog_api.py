"""Read-only queries over public core and optional workflow catalogs."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import MetaData, create_engine, inspect, select

from .normative import (
    CATALOG_CONTRACT_ID, CATALOG_SCHEMA_VERSION, catalog as core_catalog,
)
from .profiles import validate_connection_url
from .database_urls import require_existing_database_url
from .workflow import (
    PROFILE_ID, PROFILE_SCHEMA_VERSION, TransformationError, workflow_catalog,
)


def _uuid(value: str) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError as error:
        raise TransformationError("invalid_dataset_id", "dataset_id must be a UUID.") from error


def _assert_core(connection: Any, tables: Any) -> None:
    if not inspect(connection).has_table(tables.catalog_identity.name):
        raise TransformationError("catalog_missing", "The core OpenStatSpec catalog is absent.")
    rows = connection.execute(select(tables.catalog_identity)).mappings().all()
    if len(rows) != 1 or (
        rows[0]["catalog_identity_key"] != 1
        or rows[0]["contract_id"] != CATALOG_CONTRACT_ID
        or rows[0]["schema_version"] != CATALOG_SCHEMA_VERSION
    ):
        raise TransformationError("catalog_incompatible", "The core catalog identity is incompatible.")


def _assert_workflow(connection: Any, tables: Any) -> None:
    if validate_connection_url(str(connection.engine.url)).name != "sqlite":
        raise TransformationError(
            "dialect_not_supported", "Derived workflow catalog reads support SQLite only."
        )
    if not inspect(connection).has_table(tables.transformation_profile_identity.name):
        raise TransformationError("profile_missing", "The transformation profile is absent.")
    rows = connection.execute(select(tables.transformation_profile_identity)).mappings().all()
    if len(rows) != 1 or (
        rows[0]["profile_identity_key"] != 1
        or rows[0]["contract_id"] != PROFILE_ID
        or rows[0]["schema_version"] != PROFILE_SCHEMA_VERSION
        or rows[0]["core_contract_id"] != CATALOG_CONTRACT_ID
    ):
        raise TransformationError(
            "profile_incompatible", "The transformation profile identity is incompatible."
        )


def catalog_datasets(*, database_url: str, kind: str | None = None) -> dict[str, Any]:
    """List source and/or derived datasets without mutating either catalog."""
    require_existing_database_url(database_url)
    if kind not in {None, "core", "derived"}:
        raise ValueError("kind must be core, derived, or None.")
    engine = create_engine(database_url)
    core = core_catalog(MetaData())
    workflow = workflow_catalog(MetaData())
    datasets: list[dict[str, Any]] = []
    with engine.connect() as connection:
        _assert_core(connection, core)
        if kind in {None, "core"}:
            datasets.extend({
                "dataset_id": row["dataset_id"], "kind": "core",
                "dataset_name": row["dataset_name"], "dataset_label": row["dataset_label"],
                "physical_relation_schema": row["physical_table_schema"],
                "physical_relation_name": row["physical_table_name"],
                "row_count": row["source_case_count"], "source_format": row["source_format"],
                "source_hash": row["source_hash"],
            } for row in connection.execute(
                select(core.dataset).order_by(core.dataset.c.imported_at, core.dataset.c.dataset_id)
            ).mappings())
        if kind in {None, "derived"} and inspect(connection).has_table(
            workflow.transformation_profile_identity.name
        ):
            _assert_workflow(connection, workflow)
            datasets.extend({
                "dataset_id": row["derived_dataset_id"], "kind": "derived",
                "dataset_name": None, "dataset_label": None,
                "physical_relation_schema": row["physical_relation_schema"],
                "physical_relation_name": row["physical_relation_name"],
                "row_count": row["row_count"], "source_format": "SQL",
                "source_hash": row["content_hash"] or row["schema_hash"],
                "output_mode": row["output_mode"],
                "transformation_run_id": row["transformation_run_id"],
            } for row in connection.execute(
                select(workflow.derived_dataset)
                .order_by(workflow.derived_dataset.c.published_at, workflow.derived_dataset.c.derived_dataset_id)
            ).mappings())
    return {"datasets": datasets, "count": len(datasets)}


def catalog_dataset(*, database_url: str, dataset_id: str, kind: str) -> dict[str, Any]:
    """Return one public catalog record, variables, weight, and derived lineage."""
    require_existing_database_url(database_url)
    dataset_id = _uuid(dataset_id)
    if kind not in {"core", "derived"}:
        raise ValueError("kind must be core or derived.")
    engine = create_engine(database_url)
    core = core_catalog(MetaData())
    workflow = workflow_catalog(MetaData())
    with engine.connect() as connection:
        _assert_core(connection, core)
        if kind == "core":
            dataset = connection.execute(
                select(core.dataset).where(core.dataset.c.dataset_id == dataset_id)
            ).mappings().one_or_none()
            if dataset is None:
                raise TransformationError("dataset_not_found", "Core dataset was not found.")
            variables = connection.execute(
                select(core.variable).where(core.variable.c.dataset_id == dataset_id)
                .order_by(core.variable.c.source_ordinal)
            ).mappings().all()
            weight_id = connection.execute(
                select(core.dataset_weight_variable.c.variable_id)
                .where(core.dataset_weight_variable.c.dataset_id == dataset_id)
            ).scalar_one_or_none()
            return {
                "dataset": dict(dataset), "kind": "core",
                "variables": [dict(row) for row in variables],
                "weight_variable_id": weight_id, "lineage": [],
            }
        _assert_workflow(connection, workflow)
        if not inspect(connection).has_table(workflow.derived_dataset.name):
            raise TransformationError("dataset_not_found", "Derived dataset was not found.")
        dataset = connection.execute(
            select(workflow.derived_dataset)
            .where(workflow.derived_dataset.c.derived_dataset_id == dataset_id)
        ).mappings().one_or_none()
        if dataset is None:
            raise TransformationError("dataset_not_found", "Derived dataset was not found.")
        variables = connection.execute(
            select(workflow.derived_variable)
            .where(workflow.derived_variable.c.derived_dataset_id == dataset_id)
            .order_by(workflow.derived_variable.c.column_ordinal)
        ).mappings().all()
        variable_ids = [row["derived_variable_id"] for row in variables]
        lineage = [] if not variable_ids else connection.execute(
            select(workflow.derived_variable_lineage)
            .where(workflow.derived_variable_lineage.c.derived_variable_id.in_(variable_ids))
            .order_by(
                workflow.derived_variable_lineage.c.derived_variable_id,
                workflow.derived_variable_lineage.c.source_ordinal,
            )
        ).mappings().all()
        weight_id = connection.execute(
            select(workflow.derived_dataset_weight_variable.c.derived_variable_id)
            .where(workflow.derived_dataset_weight_variable.c.derived_dataset_id == dataset_id)
        ).scalar_one_or_none()
        run = connection.execute(
            select(workflow.transformation_run)
            .where(workflow.transformation_run.c.transformation_run_id == dataset["transformation_run_id"])
        ).mappings().one()
        return {
            "dataset": dict(dataset), "kind": "derived",
            "variables": [dict(row) for row in variables],
            "weight_variable_id": weight_id,
            "lineage": [dict(row) for row in lineage],
            "transformation_run": dict(run),
        }
