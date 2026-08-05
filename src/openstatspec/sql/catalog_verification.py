"""Strict ownership and structural verification for OpenStatSpec SQL catalogs."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select

from ..core import UnsupportedOperationError
from .normative import CATALOG_CONTRACT_ID, CATALOG_SCHEMA_VERSION


def _normalized_sql_type(inspector: Any, value: Any) -> str:
    compiled = " ".join(
        str(value.compile(dialect=inspector.bind.dialect)).strip().upper().split()
    )
    if inspector.bind.dialect.name in {"mysql", "mariadb"}:
        if compiled in {"BOOL", "BOOLEAN", "TINYINT(1)"}:
            return "BOOLEAN/TINYINT(1)"
        compiled = re.sub(
            r"\b(TINYINT|SMALLINT|MEDIUMINT|INTEGER|INT|BIGINT)\(\d+\)",
            r"\1",
            compiled,
        )
    return compiled


def _normalized_default(value: Any) -> str | None:
    if value is None:
        return None
    result = " ".join(str(value).strip().split())
    while result.startswith("(") and result.endswith(")"):
        result = result[1:-1].strip()
    return result


def _expected_unique_constraints(table: Table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if getattr(constraint, "__visit_name__", "") == "unique_constraint"
    }


def _actual_unique_constraints(inspector: Any, table_name: str) -> set[tuple[str, ...]]:
    constraints = {
        tuple(str(name) for name in item.get("column_names") or ())
        for item in inspector.get_unique_constraints(table_name)
    }
    constraints.update(
        tuple(str(name) for name in item.get("column_names") or ())
        for item in inspector.get_indexes(table_name)
        if item.get("unique")
    )
    constraints.discard(())
    return constraints


def _expected_foreign_keys(table: Table) -> set[tuple[Any, ...]]:
    return {
        (
            tuple(column.name for column in constraint.columns),
            next(iter(constraint.elements)).column.table.name,
            tuple(element.column.name for element in constraint.elements),
        )
        for constraint in table.foreign_key_constraints
    }


def _actual_foreign_keys(inspector: Any, table_name: str) -> set[tuple[Any, ...]]:
    return {
        (
            tuple(str(name) for name in item.get("constrained_columns") or ()),
            str(item.get("referred_table") or ""),
            tuple(str(name) for name in item.get("referred_columns") or ()),
        )
        for item in inspector.get_foreign_keys(table_name)
    }


def _normalized_check_sql(value: Any) -> str:
    result = " ".join(str(value).strip().casefold().split())
    result = result.replace("`", "").replace('"', "")
    result = re.sub(r"\[([^]]+)\]", r"\1", result)
    result = re.sub(r"(?<![a-z0-9])_[a-z0-9]+(?=')", "", result)
    result = re.sub(
        r"::\s*(?:character varying|varchar|text|integer|bigint|smallint|boolean)\b",
        "",
        result,
    )
    result = re.sub(r"\(([a-z_][a-z0-9_]*)\)", r"\1", result)
    while result.startswith("(") and result.endswith(")"):
        result = result[1:-1].strip()
    return result


def _expected_check_constraints(table: Table) -> set[str]:
    return {
        _normalized_check_sql(constraint.sqltext)
        for constraint in table.constraints
        if getattr(constraint, "__visit_name__", "")
        == "table_or_column_check_constraint"
    }


def _actual_check_constraints(inspector: Any, table_name: str) -> set[str]:
    return {
        _normalized_check_sql(item.get("sqltext") or "")
        for item in inspector.get_check_constraints(table_name)
    }


def _table_shape_valid(
    inspector: Any,
    table: Table,
    *,
    allowed_missing: Iterable[str] = (),
) -> bool:
    actual = {
        str(column["name"]): column
        for column in inspector.get_columns(table.name)
    }
    expected = {column.name: column for column in table.columns}
    missing = set(expected) - set(actual)
    if set(actual) - set(expected) or missing - set(allowed_missing):
        return False
    for name in set(actual) & set(expected):
        expected_column = expected[name]
        actual_column = actual[name]
        if (
            _normalized_sql_type(inspector, expected_column.type)
            != _normalized_sql_type(inspector, actual_column["type"])
            or bool(actual_column.get("nullable")) != bool(expected_column.nullable)
        ):
            return False
        expected_default = _normalized_default(
            expected_column.server_default.arg
            if expected_column.server_default is not None else None
        )
        if _normalized_default(actual_column.get("default")) != expected_default:
            return False
        if actual_column.get("identity") is not None or actual_column.get("computed") is not None:
            return False
    expected_pk = tuple(column.name for column in table.primary_key.columns)
    actual_pk = tuple(
        str(name)
        for name in inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
    )
    return (
        actual_pk == expected_pk
        and _actual_unique_constraints(inspector, table.name)
        == _expected_unique_constraints(table)
        and _actual_foreign_keys(inspector, table.name)
        == _expected_foreign_keys(table)
        and _actual_check_constraints(inspector, table.name)
        == _expected_check_constraints(table)
    )


def _reject(relations: Iterable[str] = ()) -> None:
    suffix = f": {', '.join(sorted(relations))}" if relations else ""
    raise UnsupportedOperationError(
        "The selected database catalog contains foreign, obsolete, or "
        f"structurally incompatible relations{suffix}. Remove them manually "
        "before continuing."
    )


def verify_catalog_relations(
    connection: Any,
    normative: Any,
    *,
    allowed_migrations: Mapping[str, set[str]] | None = None,
) -> None:
    """Accept only exact normative and validated optional OpenStatSpec profiles."""
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    existing_views = set(inspector.get_view_names())
    normative_tables = {table.name: table for table in normative.all()}
    if not set(normative_tables) <= existing_tables:
        _reject(set(normative_tables) - existing_tables)
    if any(
        not _table_shape_valid(inspector, table)
        for table in normative_tables.values()
    ):
        _reject(normative_tables)

    identities = connection.execute(select(normative.catalog_identity)).mappings().all()
    if len(identities) != 1 or (
        identities[0]["catalog_identity_key"] != 1
        or identities[0]["contract_id"] != CATALOG_CONTRACT_ID
        or identities[0]["schema_version"] != CATALOG_SCHEMA_VERSION
    ):
        _reject({normative.catalog_identity.name})

    owned_tables = set(normative_tables)
    owned_views: set[str] = set()
    owned_tables.update(
        str(name)
        for name in connection.execute(
            select(normative.dataset.c.physical_table_name)
        ).scalars()
        if name
    )

    from .workflow import (
        PROFILE_ID, PROFILE_SCHEMA_VERSION, TransformationError,
        _assert_trigger_definitions, _derived_trigger_sql,
        _validate_workflow_schema, workflow_catalog,
    )

    workflow = workflow_catalog(MetaData())
    workflow_tables = {table.name: table for table in workflow.all()}
    workflow_present = set(workflow_tables) & existing_tables
    if workflow_present:
        if set(workflow_tables) - existing_tables or any(
            not _table_shape_valid(inspector, table)
            for table in workflow_tables.values()
        ):
            _reject(workflow_present)
        try:
            _validate_workflow_schema(connection, workflow)
        except TransformationError:
            _reject(workflow_present)
        identity_rows = connection.execute(
            select(workflow.transformation_profile_identity)
        ).mappings().all()
        if len(identity_rows) != 1 or (
            identity_rows[0]["profile_identity_key"] != 1
            or identity_rows[0]["contract_id"] != PROFILE_ID
            or identity_rows[0]["schema_version"] != PROFILE_SCHEMA_VERSION
            or identity_rows[0]["core_contract_id"] != CATALOG_CONTRACT_ID
        ):
            _reject({workflow.transformation_profile_identity.name})
        owned_tables.update(workflow_tables)
        physically_removed = set(connection.execute(select(
            workflow.derived_dataset_disposition_event.c.derived_dataset_id
        ).where(
            workflow.derived_dataset_disposition_event.c.event_kind
            == "physical_removed"
        )).scalars())
        for row in connection.execute(select(
            workflow.derived_dataset.c.derived_dataset_id,
            workflow.derived_dataset.c.physical_relation_name,
            workflow.derived_dataset.c.output_mode,
        )).mappings():
            if row["derived_dataset_id"] in physically_removed:
                continue
            name = str(row["physical_relation_name"])
            if row["output_mode"] == "view":
                owned_views.add(name)
            else:
                owned_tables.add(name)
                try:
                    _assert_trigger_definitions(
                        connection,
                        _derived_trigger_sql(
                            connection, str(row["derived_dataset_id"]), name,
                        ),
                        code="derived_corrupt",
                    )
                except TransformationError:
                    _reject({name})

    from .inplace_transform import apply_audit_catalog

    audit = apply_audit_catalog(MetaData())
    if audit.name in existing_tables:
        if not _table_shape_valid(
            inspector,
            audit,
            allowed_missing=(allowed_migrations or {}).get(audit.name, set()),
        ):
            _reject({audit.name})
        owned_tables.add(audit.name)

    foreign = (existing_tables - owned_tables) | (existing_views - owned_views)
    missing = (owned_tables - existing_tables) | (owned_views - existing_views)
    collisions = (owned_tables & existing_views) | (owned_views & existing_tables)
    if foreign or missing or collisions:
        _reject(foreign | missing | collisions)
