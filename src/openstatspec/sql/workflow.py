"""Optional, strict SQL transformation workflow profile.

The source-faithful core catalog remains immutable. SQL-derived relations live
only in the optional transformation profile catalog defined in this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
from contextlib import contextmanager
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
    ForeignKeyConstraint, Integer, MetaData, String, Table, Text,
    UniqueConstraint, create_engine, event as sqlalchemy_event, insert, inspect,
    select, text, update,
)

import rfc8785
from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope

from ..core import UnsupportedOperationError
from .capabilities import SPECIFICATION_COMMIT
from .normative import (
    CATALOG_CONTRACT_ID, CATALOG_SCHEMA_VERSION, binary64_type,
    catalog as core_catalog,
)
from .profiles import preflight, validate_connection_url
from .wide import physical_name

PROFILE_ID = "openstatspec-sql-transformation-workflow-v0.1"
PROFILE_SCHEMA_VERSION = 2
SQLITE_SERVER_VERSION_CONSTRAINT = "supported-profile"
_SQLITE_MINIMUM_VERSION = (3, 35, 0)
_SQLITE_MAXIMUM_VERSION = (4, 0, 0)
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STAGING_PREFIX = "__openstatspec_staging_"
_FORBIDDEN = {
    "ALTER", "ANALYZE", "ATTACH", "CALL", "COPY", "CREATE", "DELETE",
    "DETACH", "DROP", "EXEC", "EXECUTE", "GRANT", "INSERT", "LOAD",
    "MERGE", "OUTFILE", "PRAGMA", "REPLACE", "REVOKE", "TRUNCATE",
    "UPDATE", "VACUUM",
}
_SCALAR_TYPES = (type(None), bool, int, float, str)


class TransformationError(UnsupportedOperationError):
    """The optional SQL transformation contract was violated."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"SQL transformation failed [{code}]: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WorkflowTables:
    transformation_profile_identity: Table
    transformation_definition: Table
    transformation_version: Table
    transformation_parameter: Table
    transformation_run: Table
    transformation_run_parameter: Table
    transformation_run_input: Table
    derived_dataset: Table
    derived_variable: Table
    derived_variable_lineage: Table
    derived_dataset_weight_variable: Table
    transformation_event: Table
    derived_dataset_disposition_event: Table

    def all(self) -> tuple[Table, ...]:
        return tuple(getattr(self, item.name) for item in fields(self))


def workflow_catalog(
    metadata: MetaData, *, _include_staging_relation_key: bool = True,
) -> WorkflowTables:
    core_catalog(metadata)
    identity = Table(
        "transformation_profile_identity", metadata,
        Column("profile_identity_key", Integer, primary_key=True, autoincrement=False),
        Column("contract_id", String(128), nullable=False, unique=True),
        Column("schema_version", Integer, nullable=False),
        Column("core_contract_id", String(128), nullable=False),
        Column("created_at", DateTime, nullable=False),
        CheckConstraint("profile_identity_key = 1"),
        CheckConstraint(f"contract_id = '{PROFILE_ID}'"),
        CheckConstraint(f"core_contract_id = '{CATALOG_CONTRACT_ID}'"),
    )
    definition = Table(
        "transformation_definition", metadata,
        Column("transformation_id", String(36), primary_key=True),
        Column("stable_name", String(255), nullable=False, unique=True),
        Column("title", Text, nullable=False),
        Column("description", Text),
        Column("created_at", DateTime, nullable=False),
    )
    version = Table(
        "transformation_version", metadata,
        Column("transformation_version_id", String(36), primary_key=True),
        Column("transformation_id", String(36), ForeignKey("transformation_definition.transformation_id"), nullable=False),
        Column("version_number", Integer, nullable=False),
        Column("query_sql", Text, nullable=False),
        Column("dialect_family", String(32), nullable=False),
        Column("server_version_constraint", String(128), nullable=False),
        Column("output_mode", String(16), nullable=False),
        Column("row_semantics", String(32), nullable=False),
        Column("metadata_policy", String(32), nullable=False),
        Column("deterministic_order_json", Text, nullable=False),
        Column("output_schema_json", Text, nullable=False),
        Column("definition_hash", String(64), nullable=False, unique=True),
        Column("published_at", DateTime, nullable=False),
        UniqueConstraint("transformation_id", "version_number"),
        UniqueConstraint("transformation_version_id", "definition_hash"),
    )
    parameter = Table(
        "transformation_parameter", metadata,
        Column("transformation_version_id", String(36), ForeignKey("transformation_version.transformation_version_id"), primary_key=True),
        Column("parameter_ordinal", Integer, primary_key=True),
        Column("parameter_name", String(255), nullable=False),
        Column("logical_type", String(16), nullable=False),
        Column("is_nullable", Boolean, nullable=False),
        Column("default_canonical_json", Text),
        Column("is_sensitive", Boolean, nullable=False),
        UniqueConstraint("transformation_version_id", "parameter_name"),
    )
    run = Table(
        "transformation_run", metadata,
        Column("transformation_run_id", String(36), primary_key=True),
        Column("transformation_version_id", String(36), nullable=False),
        Column("status", String(16), nullable=False),
        Column("executor_identity", String(128), nullable=False),
        Column("correlation_id", String(36), nullable=False),
        *(
            [Column("staging_relation_key", String(512))]
            if _include_staging_relation_key else []
        ),
        Column("engine_name", String(64), nullable=False),
        Column("engine_version", String(128), nullable=False),
        Column("dialect_profile", String(32), nullable=False),
        Column("capability_snapshot_json", Text, nullable=False),
        Column("specification_commit", String(64), nullable=False),
        Column("definition_hash", String(64), nullable=False),
        Column("parameters_hash", String(64), nullable=False),
        Column("input_set_hash", String(64), nullable=False),
        Column("started_at", DateTime, nullable=False),
        Column("completed_at", DateTime),
        UniqueConstraint("transformation_run_id", "status"),
        CheckConstraint("status IN ('started', 'succeeded', 'failed')"),
        ForeignKeyConstraint(
            ["transformation_version_id", "definition_hash"],
            ["transformation_version.transformation_version_id",
             "transformation_version.definition_hash"],
        ),
    )
    run_parameter = Table(
        "transformation_run_parameter", metadata,
        Column("transformation_run_id", String(36), ForeignKey("transformation_run.transformation_run_id"), primary_key=True),
        Column("parameter_ordinal", Integer, primary_key=True),
        Column("parameter_name", String(255), nullable=False),
        Column("logical_type", String(16), nullable=False),
        Column("value_envelope", Text, nullable=False),
        Column("value_hash", String(64), nullable=False),
        Column("is_sensitive", Boolean, nullable=False),
        UniqueConstraint("transformation_run_id", "parameter_name"),
    )
    run_input = Table(
        "transformation_run_input", metadata,
        Column("transformation_run_id", String(36), ForeignKey("transformation_run.transformation_run_id"), primary_key=True),
        Column("input_ordinal", Integer, primary_key=True),
        Column("input_alias", String(255), nullable=False),
        Column("input_kind", String(16), nullable=False),
        Column("core_dataset_id", String(36), ForeignKey("dataset.dataset_id")),
        Column("derived_dataset_id", String(36), ForeignKey("derived_dataset.derived_dataset_id")),
        Column("physical_relation_schema_snapshot", String(255)),
        Column("physical_relation_name_snapshot", String(255), nullable=False),
        Column("physical_relation_key_snapshot", String(512), nullable=False),
        Column("schema_hash", String(64), nullable=False),
        Column("content_or_source_hash", String(128), nullable=False),
        Column("snapshot_hash_kind", String(32), nullable=False),
        Column("snapshot_hash_algorithm", String(32), nullable=False),
        Column("snapshot_hash_version", String(64), nullable=False),
        UniqueConstraint("transformation_run_id", "input_alias"),
        CheckConstraint(
            "(input_kind = 'core' AND core_dataset_id IS NOT NULL AND derived_dataset_id IS NULL) OR "
            "(input_kind = 'derived' AND core_dataset_id IS NULL AND derived_dataset_id IS NOT NULL)"
        ),
    )
    derived = Table(
        "derived_dataset", metadata,
        Column("derived_dataset_id", String(36), primary_key=True),
        Column("transformation_run_id", String(36), nullable=False, unique=True),
        Column("run_status", String(16), nullable=False),
        Column("physical_relation_schema", String(255)),
        Column("physical_relation_name", String(255), nullable=False),
        Column("physical_relation_key", String(512), nullable=False, unique=True),
        Column("output_mode", String(16), nullable=False),
        Column("row_count", BigInteger, nullable=False),
        Column("schema_hash", String(64), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("content_hash_policy", String(64), nullable=False),
        Column("content_hash_kind", String(32), nullable=False),
        Column("content_hash_algorithm", String(32), nullable=False),
        Column("content_hash_version", String(64), nullable=False),
        Column("published_at", DateTime, nullable=False),
        CheckConstraint("run_status = 'succeeded'"),
        CheckConstraint("output_mode IN ('materialized', 'view')"),
        ForeignKeyConstraint(
            ["transformation_run_id", "run_status"],
            ["transformation_run.transformation_run_id", "transformation_run.status"],
        ),
    )
    variable = Table(
        "derived_variable", metadata,
        Column("derived_variable_id", String(36), primary_key=True),
        Column("derived_dataset_id", String(36), ForeignKey("derived_dataset.derived_dataset_id"), nullable=False),
        Column("column_ordinal", Integer, nullable=False),
        Column("physical_name", String(255), nullable=False),
        Column("logical_storage_kind", String(16), nullable=False),
        Column("is_nullable", Boolean, nullable=False),
        Column("variable_label", Text),
        Column("metadata_json", Text),
        Column("metadata_hash", String(64)),
        Column("lineage_kind", String(16), nullable=False),
        UniqueConstraint("derived_dataset_id", "column_ordinal"),
        UniqueConstraint("derived_dataset_id", "physical_name"),
        UniqueConstraint("derived_dataset_id", "derived_variable_id"),
        CheckConstraint("lineage_kind IN ('identity', 'computed', 'aggregate', 'constant')"),
    )
    lineage = Table(
        "derived_variable_lineage", metadata,
        Column("derived_variable_id", String(36), ForeignKey("derived_variable.derived_variable_id"), primary_key=True),
        Column("source_ordinal", Integer, primary_key=True),
        Column("transformation_run_id", String(36), nullable=False),
        Column("input_ordinal", Integer, nullable=False),
        Column("core_variable_id", String(36), ForeignKey("variable.variable_id")),
        Column("parent_derived_variable_id", String(36), ForeignKey("derived_variable.derived_variable_id")),
        Column("expression_role", String(32), nullable=False),
        ForeignKeyConstraint(
            ["transformation_run_id", "input_ordinal"],
            ["transformation_run_input.transformation_run_id",
             "transformation_run_input.input_ordinal"],
        ),
        CheckConstraint(
            "(core_variable_id IS NOT NULL AND parent_derived_variable_id IS NULL) OR "
            "(core_variable_id IS NULL AND parent_derived_variable_id IS NOT NULL)"
        ),
        CheckConstraint("expression_role IN ('identity', 'contributing', 'grouping', 'ordering')"),
    )
    weight = Table(
        "derived_dataset_weight_variable", metadata,
        Column("derived_dataset_id", String(36), ForeignKey("derived_dataset.derived_dataset_id"), primary_key=True),
        Column("derived_variable_id", String(36), nullable=False, unique=True),
        Column("derivation_kind", String(16), nullable=False),
        Column("meaning", Text),
        ForeignKeyConstraint(
            ["derived_dataset_id", "derived_variable_id"],
            ["derived_variable.derived_dataset_id", "derived_variable.derived_variable_id"],
        ),
        CheckConstraint("derivation_kind IN ('identity', 'computed')"),
    )
    event = Table(
        "transformation_event", metadata,
        Column("transformation_event_id", String(36), primary_key=True),
        Column("transformation_run_id", String(36), ForeignKey("transformation_run.transformation_run_id"), nullable=False),
        Column("event_ordinal", Integer, nullable=False),
        Column("severity", String(16), nullable=False),
        Column("event_code", String(128), nullable=False),
        Column("execution_phase", String(32), nullable=False),
        Column("safe_detail_json", Text, nullable=False),
        Column("created_at", DateTime, nullable=False),
        UniqueConstraint("transformation_run_id", "event_ordinal"),
    )
    disposition = Table(
        "derived_dataset_disposition_event", metadata,
        Column("disposition_event_id", String(36), primary_key=True),
        Column("derived_dataset_id", String(36), ForeignKey("derived_dataset.derived_dataset_id"), nullable=False),
        Column("event_ordinal", Integer, nullable=False),
        Column("event_kind", String(32), nullable=False),
        Column("actor_identity", String(255), nullable=False),
        Column("reason", Text, nullable=False),
        Column("prior_content_hash", String(64), nullable=False),
        Column("created_at", DateTime, nullable=False),
        UniqueConstraint("derived_dataset_id", "event_ordinal"),
        CheckConstraint(
            "event_kind IN ('retired', 'physical_removal_requested', 'physical_removed')"
        ),
    )
    return WorkflowTables(
        identity, definition, version, parameter, run, run_parameter, run_input,
        derived, variable, lineage, weight, event, disposition,
    )


def transformation_capabilities(database_url: str | None = None) -> dict[str, Any]:
    """Machine-readable implementation boundary for the optional profile."""
    dialect = "sqlite"
    if database_url is not None:
        dialect = validate_connection_url(database_url).name
    declaration = {
        "contract_id": PROFILE_ID, "schema_version": PROFILE_SCHEMA_VERSION,
        "status": "available" if dialect == "sqlite" else "unsupported",
        "dialect_family": dialect,
        "supported_dialect_families": ["sqlite"],
        "supported": {} if dialect != "sqlite" else {
            "inputs": "single_parent_alias",
            "parent_kinds": ["core", "derived_materialized"],
            "sql_validation": "sqlglot_ast_allowlist",
            "parameters": "named_driver_bound_json_scalars_without_fractional_numbers",
            "output_modes": ["materialized"],
            "lineage": "declared_single_parent_columns",
            "relation_snapshot_hash": "openstatspec-relation-snapshot-v1",
            "parameter_set_hash": "openstatspec-parameter-set-v1",
            "input_set_hash": "openstatspec-input-set-v1",
            "append_only_disposition": [
                "retired", "physical_removal_requested", "physical_removed",
            ],
            "maximum_concurrent_executions": 1,
            "atomic_failure_audit": True,
        },
        "unsupported": [
            "postgresql_workflow", "mysql_workflow", "mariadb_workflow",
            "multiple_inputs", "immutable_lookup_relations", "sensitive_parameters",
            "non_json_parameter_logical_types",
            "multiple_output_relations",
        ],
    }
    if dialect != "sqlite":
        declaration["reason"] = {
            "code": "dialect_not_supported",
            "detail": "SQL transformation execution is implemented only for SQLite.",
        }
    return declaration


def _workflow_profile(database_url: str):
    profile = validate_connection_url(database_url)
    if profile.name != "sqlite":
        raise TransformationError(
            "dialect_not_supported",
            "SQL transformation execution is implemented only for SQLite.",
        )
    return profile


def _workflow_engine(database_url: str, dialect_family: str):
    if dialect_family != "sqlite":
        raise TransformationError(
            "dialect_not_supported", "The workflow engine supports SQLite only."
        )
    engine = create_engine(database_url, isolation_level=None)

    @sqlalchemy_event.listens_for(engine, "connect")
    def enable_sqlite_contract(dbapi_connection, _connection_record):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        if cursor.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")
        cursor.close()

    @sqlalchemy_event.listens_for(engine, "begin")
    def begin_repeatable_snapshot(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def _assert_sqlite_server_version(connection: Any, constraint: str) -> None:
    if constraint != SQLITE_SERVER_VERSION_CONSTRAINT:
        raise TransformationError(
            "server_version_constraint_invalid",
            f"server_version_constraint must be {SQLITE_SERVER_VERSION_CONSTRAINT!r}.",
        )
    raw = str(connection.exec_driver_sql("SELECT sqlite_version()").scalar_one())
    try:
        parts = tuple(int(part) for part in raw.split(".")[:3])
        version = parts + (0,) * (3 - len(parts))
    except ValueError as error:
        raise TransformationError(
            "server_version_unsupported", "SQLite returned an invalid server version."
        ) from error
    if not (_SQLITE_MINIMUM_VERSION <= version < _SQLITE_MAXIMUM_VERSION):
        raise TransformationError(
            "server_version_unsupported",
            "The SQLite server is outside the supported >=3.35.0,<4.0.0 range.",
        )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _assert_core_identity(connection: Any, tables: Any) -> None:
    if not inspect(connection).has_table(tables.catalog_identity.name):
        raise TransformationError("catalog_missing", "The core OpenStatSpec catalog is absent.")
    rows = connection.execute(select(tables.catalog_identity)).mappings().all()
    if len(rows) != 1:
        raise TransformationError("catalog_incompatible", "The core catalog needs one identity row.")
    identity = dict(rows[0])
    if (
        identity["catalog_identity_key"] != 1
        or identity["contract_id"] != CATALOG_CONTRACT_ID
        or identity["schema_version"] != CATALOG_SCHEMA_VERSION
    ):
        raise TransformationError("catalog_incompatible", "The core catalog identity is incompatible.")


def _normalized_type_signature(value: Any, dialect: Any) -> tuple[str, str]:
    affinity = getattr(value, "_type_affinity", type(value))
    rendered = re.sub(
        r"\s+", " ", str(value.compile(dialect=dialect)).strip().upper()
    )
    return affinity.__name__, rendered


def _constraint_signature(connection: Any, table: Table) -> dict[str, Any]:
    unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    foreign = {
        (
            tuple(element.parent.name for element in constraint.elements),
            constraint.elements[0].column.table.name,
            tuple(element.column.name for element in constraint.elements),
        )
        for constraint in table.foreign_key_constraints
    }
    return {
        "columns": tuple((
            column.name, bool(column.nullable),
            _normalized_type_signature(column.type, connection.dialect),
        ) for column in table.columns),
        "primary_key": tuple(column.name for column in table.primary_key.columns),
        "unique": unique,
        "foreign": foreign,
        "checks": {
            " ".join(str(constraint.sqltext).split()).lower()
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        },
    }


def _actual_constraint_signature(connection: Any, table: Table) -> dict[str, Any]:
    inspector = inspect(connection)
    return {
        "columns": tuple(
            (
                column["name"], bool(column["nullable"]),
                _normalized_type_signature(column["type"], connection.dialect),
            )
            for column in inspector.get_columns(table.name)
        ),
        "primary_key": tuple(
            inspector.get_pk_constraint(table.name).get("constrained_columns") or ()
        ),
        "unique": {
            tuple(item.get("column_names") or ())
            for item in inspector.get_unique_constraints(table.name)
        },
        "foreign": {
            (
                tuple(item.get("constrained_columns") or ()),
                item.get("referred_table"),
                tuple(item.get("referred_columns") or ()),
            )
            for item in inspector.get_foreign_keys(table.name)
        },
        "checks": {
            " ".join(str(item.get("sqltext") or "").split()).lower()
            for item in inspector.get_check_constraints(table.name)
        },
    }


def _normalize_trigger_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().rstrip(";")).casefold()


def _workflow_trigger_sql(connection: Any, tables: WorkflowTables) -> dict[str, str]:
    quote = connection.dialect.identifier_preparer.quote
    immutable = [table for table in tables.all() if table.name != "transformation_run"]
    statements = {
        f"oss_{table.name}_no_{operation.lower()}": (
            f"CREATE TRIGGER {quote(f'oss_{table.name}_no_{operation.lower()}')} "
            f"BEFORE {operation} ON {quote(table.name)} BEGIN "
            "SELECT RAISE(ABORT, 'OpenStatSpec workflow rows are append-only'); END"
        )
        for table in immutable
        for operation in ("UPDATE", "DELETE")
    }
    run = tables.transformation_run
    stable_columns = [
        column.name for column in run.columns
        if column.name not in {"status", "completed_at"}
    ]
    unchanged = " AND ".join(
        f"NEW.{quote(name)} IS OLD.{quote(name)}" for name in stable_columns
    )
    statements["oss_transformation_run_update_guard"] = (
        f"CREATE TRIGGER {quote('oss_transformation_run_update_guard')} "
        f"BEFORE UPDATE ON {quote(run.name)} WHEN NOT ("
        "OLD.status = 'started' AND NEW.status IN ('succeeded', 'failed') "
        "AND OLD.completed_at IS NULL AND NEW.completed_at IS NOT NULL "
        f"AND {unchanged}) BEGIN "
        "SELECT RAISE(ABORT, 'Invalid OpenStatSpec run transition'); END"
    )
    statements["oss_transformation_run_no_delete"] = (
        f"CREATE TRIGGER {quote('oss_transformation_run_no_delete')} "
        f"BEFORE DELETE ON {quote(run.name)} BEGIN "
        "SELECT RAISE(ABORT, 'OpenStatSpec workflow runs are append-only'); END"
    )
    return statements


def _create_workflow_triggers(connection: Any, tables: WorkflowTables) -> None:
    for statement in _workflow_trigger_sql(connection, tables).values():
        connection.exec_driver_sql(statement)


def _derived_trigger_sql(
    connection: Any, derived_dataset_id: str, relation_name: str,
) -> dict[str, str]:
    quote = connection.dialect.identifier_preparer.quote
    stem = "oss_derived_relation_" + derived_dataset_id.replace("-", "")
    return {
        f"{stem}_no_{operation.lower()}": (
            f"CREATE TRIGGER {quote(f'{stem}_no_{operation.lower()}')} "
            f"BEFORE {operation} ON {quote(relation_name)} BEGIN "
            "SELECT RAISE(ABORT, 'OpenStatSpec derived relations are immutable'); END"
        )
        for operation in ("INSERT", "UPDATE", "DELETE")
    }


def _create_derived_triggers(
    connection: Any, derived_dataset_id: str, relation_name: str,
) -> None:
    for statement in _derived_trigger_sql(
        connection, derived_dataset_id, relation_name
    ).values():
        connection.exec_driver_sql(statement)


def _assert_trigger_definitions(
    connection: Any, expected: Mapping[str, str], *, code: str,
) -> None:
    rows = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
    ).all()
    actual = {str(name): str(sql) for name, sql in rows if name in expected}
    if set(actual) != set(expected) or any(
        _normalize_trigger_sql(actual[name]) != _normalize_trigger_sql(statement)
        for name, statement in expected.items()
    ):
        raise TransformationError(code, "Required immutable trigger definitions are incompatible.")


def _validate_workflow_schema(connection: Any, tables: WorkflowTables) -> None:
    inspector = inspect(connection)
    missing = [table.name for table in tables.all() if not inspector.has_table(table.name)]
    if missing:
        raise TransformationError(
            "profile_incompatible", f"Workflow catalog relations are missing: {missing!r}."
        )
    for table in tables.all():
        expected = _constraint_signature(connection, table)
        actual = _actual_constraint_signature(connection, table)
        if actual != expected:
            raise TransformationError(
                "profile_incompatible",
                f"Workflow relation {table.name!r} does not match schema version {PROFILE_SCHEMA_VERSION}.",
            )
    _assert_trigger_definitions(
        connection, _workflow_trigger_sql(connection, tables),
        code="profile_incompatible",
    )


def _staging_relation_key(relation_name: str) -> str:
    return f"sqlite:main.{relation_name}"


def _owned_staging_relation_name(value: Any) -> str:
    prefix = "sqlite:main."
    if not isinstance(value, str) or not value.startswith(prefix):
        raise TransformationError(
            "reconciliation_ownership_unverified",
            "The recorded staging relation key is missing or is not a SQLite main relation.",
        )
    relation_name = value[len(prefix):]
    if (
        _TOKEN.fullmatch(relation_name) is None
        or not relation_name.startswith(_STAGING_PREFIX)
    ):
        raise TransformationError(
            "reconciliation_ownership_unverified",
            "The recorded staging relation is outside the profile-owned staging namespace.",
        )
    return relation_name


def _migrate_staging_relation_key(
    connection: Any, tables: WorkflowTables,
) -> None:
    """Upgrade pre-recovery v2 without claiming unrecorded staging objects."""
    legacy_tables = workflow_catalog(
        MetaData(), _include_staging_relation_key=False
    )
    _validate_workflow_schema(connection, legacy_tables)
    quote = connection.dialect.identifier_preparer.quote
    for trigger_name in _workflow_trigger_sql(
        connection, legacy_tables
    ):
        connection.exec_driver_sql(
            f"DROP TRIGGER {quote(trigger_name)}"
        )

    migration_name = "__oss_migrating_transformation_run"
    migration_metadata = MetaData()
    tables.transformation_version.to_metadata(migration_metadata)
    migration_run = tables.transformation_run.to_metadata(
        migration_metadata, name=migration_name
    )
    migration_run.create(connection)
    target_columns = [column.name for column in tables.transformation_run.columns]
    target_sql = ", ".join(quote(name) for name in target_columns)
    source_sql = ", ".join(
        "NULL" if name == "staging_relation_key" else quote(name)
        for name in target_columns
    )
    connection.exec_driver_sql(
        f"INSERT INTO {quote(migration_name)} ({target_sql}) "
        f"SELECT {source_sql} FROM {quote(tables.transformation_run.name)}"
    )
    connection.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    connection.exec_driver_sql(
        f"DROP TABLE {quote(tables.transformation_run.name)}"
    )
    connection.exec_driver_sql(
        f"ALTER TABLE {quote(migration_name)} "
        f"RENAME TO {quote(tables.transformation_run.name)}"
    )
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise TransformationError(
            "profile_incompatible",
            "Workflow migration would violate a foreign-key ownership invariant.",
        )
    _create_workflow_triggers(connection, tables)


def create_workflow_catalog(connection: Any, tables: WorkflowTables) -> None:
    """Create or validate the additive optional-profile catalog."""
    if connection.dialect.name != "sqlite":
        raise TransformationError("dialect_not_supported", "Workflow catalog creation supports SQLite only.")
    core = core_catalog(MetaData())
    _assert_core_identity(connection, core)
    existing = set(inspect(connection).get_table_names())
    names = {item.name for item in tables.all()}
    if tables.transformation_profile_identity.name not in existing:
        conflicts = sorted(existing & names)
        if conflicts:
            raise RuntimeError(
                "The selected namespace contains unowned transformation-profile relations: "
                + ", ".join(conflicts)
            )
        tables.transformation_profile_identity.metadata.create_all(
            connection, tables=list(tables.all())
        )
        connection.execute(insert(tables.transformation_profile_identity).values(
            profile_identity_key=1, contract_id=PROFILE_ID,
            schema_version=PROFILE_SCHEMA_VERSION, core_contract_id=CATALOG_CONTRACT_ID, created_at=_now(),
        ))
        _create_workflow_triggers(connection, tables)
        _validate_workflow_schema(connection, tables)
        return
    rows = connection.execute(
        select(tables.transformation_profile_identity)
    ).mappings().all()
    if len(rows) != 1 or dict(rows[0])["profile_identity_key"] != 1:
        raise TransformationError("profile_incompatible", "The workflow profile needs one identity row.")
    identity = dict(rows[0])
    if identity["contract_id"] != PROFILE_ID or identity["core_contract_id"] != CATALOG_CONTRACT_ID or identity["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise TransformationError("profile_incompatible", "The workflow profile identity is incompatible.")
    run_columns = {
        str(column["name"]) for column in inspect(connection).get_columns(
            tables.transformation_run.name
        )
    }
    if "staging_relation_key" not in run_columns:
        _migrate_staging_relation_key(connection, tables)
    _validate_workflow_schema(connection, tables)


def _canonical_json(value: Any) -> str:
    return rfc8785.dumps(value).decode("utf-8")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _uuid(value: str, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError as error:
        raise TransformationError("invalid_dataset_id", f"{label} must be a UUID.") from error


def _mask_and_validate_sql(query_sql: str, dialect_family: str) -> str:
    if not isinstance(query_sql, str) or not query_sql.strip():
        raise TransformationError("empty_sql", "query_sql must be a non-empty SELECT.")
    sql = query_sql.replace("\r\n", "\n").replace("\r", "\n")
    masked: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if quote:
            masked.append(" ")
            if char == quote:
                if next_char == quote and quote in {"'", '"'}:
                    masked.append(" ")
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            masked.append(" ")
        elif char == "[":
            quote = "]"
            masked.append(" ")
        elif char == ";" or (char == "-" and next_char == "-") or (char == "/" and next_char == "*"):
            raise TransformationError("unsafe_sql", "Semicolons and SQL comments are not allowed.")
        else:
            masked.append(char)
        index += 1
    if quote:
        raise TransformationError("invalid_sql", "SQL contains an unterminated quoted value.")
    masked_sql = "".join(masked)
    tokens = [item.upper() for item in _TOKEN.findall(masked_sql)]
    forbidden = sorted(set(tokens) & _FORBIDDEN)
    if forbidden:
        raise TransformationError("select_only", f"Forbidden SQL keyword: {forbidden[0]}.")
    if "?" in masked_sql or re.search(r"(?<![A-Za-z0-9_])(?:[$][0-9]+|%s)", masked_sql):
        raise TransformationError("parameter_style", "Only named :parameter bindings are allowed.")
    try:
        statements = parse(sql, read="sqlite")
    except ParseError as error:
        raise TransformationError("invalid_sql", f"SQL parser rejected the query: {error}.") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise TransformationError("select_only", "Only one SELECT query is allowed.")
    tree = statements[0]
    if any(cte.alias.casefold() == "parent" for cte in tree.find_all(exp.CTE)):
        raise TransformationError(
            "undeclared_relation_access", "The reserved parent alias cannot be shadowed."
        )
    unresolved: set[str] = set()
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                qualified = source.args.get("db") is not None or source.args.get("catalog") is not None
                if qualified or source.name.casefold() != "parent":
                    unresolved.add(source.sql(dialect="sqlite"))
    if unresolved:
        raise TransformationError(
            "undeclared_relation_access",
            f"Query relations must resolve only to parent; received {sorted(unresolved)!r}.",
        )
    volatile = {"CURRENT_TIMESTAMP", "NOW", "RAND", "RANDOM", "UUID", "UUID_SHORT"}
    safe_functions = {
        "ABS", "AVG", "CASE", "CAST", "CEIL", "COALESCE", "COLLATE", "COUNT", "FLOOR",
        "IF", "IFNULL", "LENGTH", "LOWER", "LTRIM", "MAX", "MIN", "NULLIF",
        "ROUND", "RTRIM", "SUBSTR", "SUBSTRING", "SUM", "TOTAL", "TRIM",
        "UPPER",
    }
    function_names = {
        function.sql_name().upper() for function in tree.find_all(exp.Func)
    }
    used_volatile = sorted(function_names & volatile)
    if used_volatile:
        raise TransformationError(
            "volatile_sql", f"Volatile function is not allowed: {used_volatile[0]}."
        )
    unknown_functions = sorted(function_names - safe_functions)
    if unknown_functions:
        raise TransformationError(
            "unsafe_sql",
            f"SQL function is outside the deterministic allowlist: {unknown_functions[0]}.",
        )
    if tree.args.get("order") is None:
        raise TransformationError(
            "deterministic_order_required",
            "The outer SELECT must define a deterministic ORDER BY.",
        )
    for ordered in tree.args["order"].expressions:
        if "NULLS FIRST" not in ordered.sql(dialect="sqlite").upper() and "NULLS LAST" not in ordered.sql(dialect="sqlite").upper():
            raise TransformationError(
                "deterministic_order_invalid",
                "Every ORDER BY item must declare NULLS FIRST or NULLS LAST explicitly.",
            )
    return sql


def _parameter_names(sql: str) -> list[str]:
    names = (
        str(node.this) for node in parse(sql, read="sqlite")[0].walk()
        if isinstance(node, exp.Placeholder)
    )
    return list(dict.fromkeys(names))


def _deterministic_order(sql: str, dialect_family: str) -> list[dict[str, Any]]:
    tree = parse(sql, read="sqlite")[0]
    descriptors = []
    for ordered in tree.args["order"].expressions:
        expression = ordered.this
        collation = None
        if isinstance(expression, exp.Collate):
            collation = expression.expression.name
            expression = expression.this
        if not isinstance(expression, exp.Column) or expression.table:
            raise TransformationError(
                "deterministic_order_invalid",
                "ORDER BY items must be unqualified output columns.",
            )
        if collation is not None:
            collation = collation.upper()
            if collation not in {"BINARY", "NOCASE", "RTRIM"}:
                raise TransformationError(
                    "deterministic_order_invalid", "SQLite collation is not fixed by this profile."
                )
        descriptors.append({
            "expression": expression.name,
            "direction": "DESC" if ordered.args.get("desc") else "ASC",
            "nulls": "FIRST" if ordered.args.get("nulls_first") else "LAST",
            "collation": collation,
        })
    if not descriptors:
        raise TransformationError(
            "deterministic_order_invalid", "ORDER BY needs at least one output column."
        )
    return descriptors


def _normalize_columns(
    columns: Sequence[Mapping[str, Any]], weight_variable: str | None = None,
) -> dict[str, Any]:
    if not columns:
        raise TransformationError("empty_schema", "At least one output column is required.")
    variables: list[dict[str, Any]] = []
    seen: set[str] = set()
    used = {"__row_ordinal"}
    source_to_physical: dict[str, str] = {}
    for ordinal, raw in enumerate(columns, start=1):
        name = str(raw.get("name", ""))
        kind = str(raw.get("storage_kind", ""))
        if not name or name in seen or name.startswith("__"):
            raise TransformationError(
                "invalid_output_column",
                "Output names must be unique, non-empty, and not reserved.",
            )
        if kind not in {"numeric", "string"}:
            raise TransformationError(
                "invalid_storage_kind", f"Column {name!r} must be numeric or string."
            )
        seen.add(name)
        output_name = physical_name(name, used)
        source_to_physical[name] = output_name
        lineage_kind = str(raw.get(
            "lineage_kind", "identity" if raw.get("source") else "computed"
        ))
        if lineage_kind not in {"identity", "computed", "aggregate", "constant"}:
            raise TransformationError("invalid_lineage_kind", "Invalid output lineage_kind.")
        raw_lineage = raw.get("lineage")
        if raw_lineage is None:
            source = raw.get("source")
            raw_lineage = [] if source is None else [{
                "input_alias": "parent", "parent_column": str(source),
                "expression_role": str(raw.get("expression_role", "identity")),
            }]
        if not isinstance(raw_lineage, Sequence) or isinstance(raw_lineage, (str, bytes)):
            raise TransformationError("invalid_lineage", "lineage must be an ordered array.")
        lineage = []
        for entry in raw_lineage:
            if not isinstance(entry, Mapping):
                raise TransformationError("invalid_lineage", "Every lineage entry must be an object.")
            normalized = {
                "input_alias": str(entry.get("input_alias", "")),
                "parent_column": str(entry.get("parent_column", "")),
                "expression_role": str(entry.get("expression_role", "")),
            }
            if normalized["input_alias"] != "parent" or not normalized["parent_column"]:
                raise TransformationError("invalid_lineage", "Lineage must resolve through parent.")
            if normalized["expression_role"] not in {"identity", "contributing", "grouping", "ordering"}:
                raise TransformationError("invalid_expression_role", "Output lineage expression_role is invalid.")
            lineage.append(normalized)
        if lineage_kind == "identity" and (
            len(lineage) != 1
            or lineage[0]["expression_role"] not in {"identity", "grouping", "ordering"}
        ):
            raise TransformationError(
                "invalid_lineage", "Identity output needs exactly one direct parent role."
            )
        if lineage_kind == "constant" and lineage:
            raise TransformationError(
                "invalid_lineage", "Constant output cannot declare a parent column."
            )
        if lineage_kind == "computed" and not lineage:
            raise TransformationError(
                "invalid_lineage", "Computed outputs need all referenced contributors."
            )
        if not isinstance(raw.get("is_nullable", True), bool):
            raise TransformationError("invalid_nullability", "is_nullable must be boolean.")
        metadata = raw.get("metadata")
        if metadata is None and (
            raw.get("label") is not None or raw.get("measurement_level") is not None
        ):
            metadata = {
                "variable_label": raw.get("label"),
                "measurement_level": raw.get("measurement_level"),
            }
        variables.append({
            "column_ordinal": ordinal, "physical_name": output_name,
            "logical_storage_kind": kind,
            "is_nullable": raw.get("is_nullable", True),
            "lineage_kind": lineage_kind, "lineage": lineage,
            "metadata": metadata,
        })
    if weight_variable is not None and weight_variable not in source_to_physical:
        raise TransformationError("weight_not_found", "weight_variable is not an output column.")
    weight = None
    if weight_variable is not None:
        descriptor = next(
            item for item in variables
            if item["physical_name"] == source_to_physical[weight_variable]
        )
        if descriptor["lineage_kind"] != "identity" or len(descriptor["lineage"]) != 1:
            raise TransformationError(
                "invalid_weight",
                "This implementation supports only one exact identity-passthrough weight.",
            )
        weight = {
            "physical_name": source_to_physical[weight_variable],
            "derivation_kind": "identity", "meaning": None,
        }
    return {"variables": variables, "weight": weight}


def _definition_document(
    *, transformation_id: str, version_number: int, query_sql: str,
    dialect_family: str, server_version_constraint: str, output_mode: str,
    row_semantics: str, metadata_policy: str, output_schema: Mapping[str, Any],
    deterministic_order: Sequence[Mapping[str, Any]],
    parameter_declarations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "contract": PROFILE_ID,
        "transformation_id": transformation_id,
        "version_number": version_number,
        "query_sql": query_sql,
        "dialect_family": dialect_family,
        "server_version_constraint": server_version_constraint,
        "output_mode": output_mode,
        "row_semantics": row_semantics,
        "metadata_policy": metadata_policy,
        "output_schema": output_schema,
        "deterministic_order": list(deterministic_order),
        "parameter_declarations": list(parameter_declarations),
    }


def _definition_hash(**values: Any) -> str:
    return _sha(_canonical_json(_definition_document(**values)))


def _validate_declared_semantics(
    query_sql: str, schema_document: Mapping[str, Any],
    *, row_semantics: str | None, metadata_policy: str,
) -> str:
    tree = parse(query_sql, read="sqlite")[0]
    aggregate_shape = bool(
        next(tree.find_all(exp.AggFunc), None)
        or tree.args.get("group")
        or tree.args.get("distinct")
        or tree.args.get("having")
    )
    join_shape = bool(tree.args.get("joins"))
    ambiguous_shape = bool(
        not isinstance(tree, exp.Select)
        or tree.args.get("with_")
        or tree.args.get("qualify")
        or tree.args.get("limit")
        or tree.args.get("offset")
        or tree.args.get("sample")
        or next(tree.find_all(exp.Subquery), None)
        or next(tree.find_all(exp.Window), None)
        or (join_shape and aggregate_shape)
    )
    if ambiguous_shape:
        actual_row_semantics = "other"
    elif aggregate_shape:
        actual_row_semantics = "aggregate"
    elif join_shape:
        actual_row_semantics = "join"
    elif tree.args.get("where"):
        actual_row_semantics = "filter"
    else:
        actual_row_semantics = "one_to_one"
    if row_semantics is None:
        row_semantics = actual_row_semantics
    elif row_semantics != actual_row_semantics:
        raise TransformationError(
            "invalid_row_semantics",
            f"row_semantics must be {actual_row_semantics!r} for this AST shape.",
        )
    from_expression = tree.args.get("from_")
    direct_parent = bool(
        from_expression
        and isinstance(from_expression.this, exp.Table)
        and from_expression.this.name.casefold() == "parent"
        and from_expression.this.args.get("db") is None
    )
    if len(tree.expressions) != len(schema_document["variables"]):
        raise TransformationError(
            "output_schema_mismatch",
            "The outer SELECT projection count must match the declared schema.",
        )
    group_columns = {
        column.name
        for expression in ((tree.args.get("group") or exp.Group()).expressions)
        for column in expression.find_all(exp.Column)
    }
    order_columns = {
        column.name
        for ordered in ((tree.args.get("order") or exp.Order()).expressions)
        for column in ordered.find_all(exp.Column)
    }
    identity_by_name: dict[str, bool] = {}
    for projected, descriptor in zip(tree.expressions, schema_document["variables"]):
        expression = projected.this if isinstance(projected, exp.Alias) else projected
        lineage = descriptor["lineage"]
        referenced_columns = {
            column.name for column in expression.find_all(exp.Column)
        }
        declared_columns = {item["parent_column"] for item in lineage}
        has_aggregate = next(expression.find_all(exp.AggFunc), None) is not None
        direct_column = isinstance(expression, exp.Column)
        if has_aggregate:
            if not referenced_columns:
                raise TransformationError(
                    "aggregate_lineage_unrepresentable",
                    "Aggregates without a referenced contributor column are unsupported.",
                )
            actual_kind = "aggregate"
        elif not referenced_columns:
            actual_kind = "constant"
        elif direct_column and direct_parent:
            actual_kind = "identity"
        else:
            actual_kind = "computed"
        if descriptor["lineage_kind"] != actual_kind:
            raise TransformationError(
                "invalid_lineage", "Declared lineage_kind does not match the projected expression."
            )
        exact_identity = bool(
            descriptor["lineage_kind"] == "identity"
            and len(lineage) == 1
            and isinstance(expression, exp.Column)
            and expression.name == lineage[0]["parent_column"]
            and (not expression.table or expression.table.casefold() == "parent")
            and direct_parent
        )
        identity_by_name[descriptor["physical_name"]] = exact_identity
        if descriptor["lineage_kind"] == "identity" and not exact_identity:
            raise TransformationError(
                "invalid_lineage",
                "Identity lineage requires a direct, unchanged parent-column projection.",
            )
        roles = {item["expression_role"] for item in lineage}
        if actual_kind in {"computed", "aggregate"} and roles != {"contributing"}:
            raise TransformationError(
                "invalid_expression_role",
                "Computed and aggregate projection references must be contributing.",
            )
        if actual_kind == "identity":
            parent_column = lineage[0]["parent_column"]
            role = lineage[0]["expression_role"]
            expected_role = "grouping" if parent_column in group_columns else "identity"
            if role == "ordering":
                ordered = (
                    parent_column in order_columns
                    or descriptor["physical_name"] in order_columns
                )
                if not ordered or parent_column in group_columns:
                    raise TransformationError(
                        "invalid_expression_role",
                        "Ordering lineage must match an ordered, non-grouping identity column.",
                    )
            elif role != expected_role:
                raise TransformationError(
                    "invalid_expression_role",
                    "Identity lineage role must match actual grouping semantics.",
                )
        if (
            descriptor["lineage_kind"] in {"computed", "aggregate"}
            and declared_columns != referenced_columns
        ):
            raise TransformationError(
                "invalid_lineage",
                "Computed and aggregate lineage must exactly cover projected column references.",
            )
        if descriptor["lineage_kind"] == "constant" and referenced_columns:
            raise TransformationError(
                "invalid_lineage",
                "Constant lineage cannot project an expression that references columns.",
            )
        if metadata_policy == "none" and descriptor["metadata"] is not None:
            raise TransformationError(
                "invalid_metadata_policy", "metadata_policy=none forbids output metadata."
            )
        if (
            metadata_policy == "identity_only"
            and descriptor["metadata"] is not None
            and not exact_identity
        ):
            raise TransformationError(
                "invalid_metadata_policy",
                "identity_only metadata requires a proven identity projection.",
            )
    weight = schema_document.get("weight")
    if weight is not None:
        unsafe_shape = (
            not isinstance(tree, exp.Select)
            or not direct_parent
            or any(tree.args.get(name) for name in (
                "with_", "joins", "group", "having", "distinct", "qualify",
                "limit", "offset", "sample",
            ))
            or next(tree.find_all(exp.AggFunc), None) is not None
            or next(tree.find_all(exp.Window), None) is not None
            or next(tree.find_all(exp.Subquery), None) is not None
        )
        if (
            row_semantics not in {"one_to_one", "filter"}
            or not identity_by_name.get(weight["physical_name"], False)
            or unsafe_shape
        ):
            raise TransformationError(
                "invalid_weight",
                "Weight propagation requires a direct identity projection without row multiplication or aggregation.",
            )
    return row_semantics


def _relation_snapshot_hash(
    connection: Any, *, relation_schema: str | None, relation_name: str,
    variables: Sequence[Mapping[str, Any]], ordinal_name: str,
    schema_hash: str,
) -> str:
    """Hash the normative typed relation-snapshot envelope without buffering."""
    quote = connection.dialect.identifier_preparer.quote
    relation = _quote_relation(connection, relation_schema, relation_name)
    projection = ", ".join(quote(str(item["physical_name"])) for item in variables)
    statement = text(
        f"SELECT {quote(ordinal_name)}, {projection} FROM {relation} "
        f"ORDER BY {quote(ordinal_name)}"
    )

    def typed(value: Any, kind: str) -> dict[str, str]:
        if value is None:
            return {"t": "null"}
        if kind == "integer":
            return {"t": "i", "v": str(int(value))}
        if kind == "numeric":
            return {"t": "f64", "v": struct.pack(">d", float(value)).hex()}
        return {"t": "s", "v": str(value)}

    digest = hashlib.sha256()
    digest.update(
        (
            '{"hash_kind":"relation_snapshot",'
            '"hash_version":"openstatspec-relation-snapshot-v1","rows":['
        ).encode("utf-8")
    )
    first = True
    for row in connection.execution_options(stream_results=True).execute(statement):
        encoded = [typed(row[0], "integer")]
        encoded.extend(
            typed(value, variable["storage_kind"])
            for value, variable in zip(tuple(row)[1:], variables)
        )
        if not first:
            digest.update(b",")
        digest.update(_canonical_json(encoded).encode("utf-8"))
        first = False
    digest.update(
        ('],"schema_hash":' + _canonical_json(schema_hash) + "}").encode("utf-8")
    )
    return digest.hexdigest()


def _parent_snapshot(connection: Any, parent_kind: str, parent_dataset_id: str) -> dict[str, Any]:
    parent_dataset_id = _uuid(parent_dataset_id, "parent_dataset_id")
    core = core_catalog(MetaData())
    workflow = workflow_catalog(MetaData())
    _assert_core_identity(connection, core)
    verified_derived_hash: str | None = None
    if parent_kind == "core":
        dataset = connection.execute(
            select(core.dataset).where(core.dataset.c.dataset_id == parent_dataset_id)
        ).mappings().one_or_none()
        if dataset is None:
            raise TransformationError("parent_not_found", "Core parent dataset was not found.")
        variables = connection.execute(
            select(core.variable).where(core.variable.c.dataset_id == parent_dataset_id)
            .order_by(core.variable.c.source_ordinal)
        ).mappings().all()
        relation_schema = dataset["physical_table_schema"]
        relation_name = dataset["physical_table_name"]
        converted = [{
            "variable_id": row["variable_id"], "source_name": row["source_name"],
            "physical_name": row["physical_name"],
            "storage_kind": row["storage_kind"],
        } for row in variables]
        weight_variable_id = connection.execute(
            select(core.dataset_weight_variable.c.variable_id).where(
                core.dataset_weight_variable.c.dataset_id == parent_dataset_id
            )
        ).scalar_one_or_none()
    elif parent_kind == "derived":
        create_workflow_catalog(connection, workflow)
        dataset = connection.execute(
            select(workflow.derived_dataset)
            .where(workflow.derived_dataset.c.derived_dataset_id == parent_dataset_id)
        ).mappings().one_or_none()
        if dataset is None:
            raise TransformationError("parent_not_found", "Derived parent dataset was not found.")
        if dataset["output_mode"] != "materialized":
            raise TransformationError(
                "input_not_immutable", "Only materialized derived datasets may be parents."
            )
        if dataset["content_hash_kind"] != "relation_snapshot" or dataset["content_hash_algorithm"] != "sha256" or dataset["content_hash_version"] != "openstatspec-relation-snapshot-v1":
            raise TransformationError("input_hash_mismatch", "Derived parent hash policy is incompatible.")
        variables, verified_derived_hash = _assert_derived_integrity(connection, workflow, dataset)
        relation_schema = dataset["physical_relation_schema"]
        relation_name = dataset["physical_relation_name"]
        converted = [{
            "variable_id": row["derived_variable_id"], "source_name": row["physical_name"],
            "physical_name": row["physical_name"],
            "storage_kind": row["logical_storage_kind"],
        } for row in variables]
        weight_variable_id = connection.execute(
            select(workflow.derived_dataset_weight_variable.c.derived_variable_id).where(
                workflow.derived_dataset_weight_variable.c.derived_dataset_id == parent_dataset_id
            )
        ).scalar_one_or_none()
    else:
        raise TransformationError("invalid_parent_kind", "parent_kind must be core or derived.")
    schema_hash = (
        str(dataset["schema_hash"])
        if parent_kind == "derived"
        else _sha(_canonical_json([
            {key: row[key] for key in ("source_name", "physical_name", "storage_kind")}
            for row in converted
        ]))
    )
    dataset_hash = (
        verified_derived_hash
        if parent_kind == "derived"
        else _relation_snapshot_hash(
            connection, relation_schema=relation_schema, relation_name=relation_name,
            variables=converted, ordinal_name="__case_ordinal", schema_hash=schema_hash,
        )
    )
    weight_source_name = next((
        row["source_name"] for row in converted
        if row["variable_id"] == weight_variable_id
    ), None)
    return {
        "kind": parent_kind, "dataset_id": parent_dataset_id,
        "dataset_hash": dataset_hash, "schema_hash": schema_hash,
        "relation_schema": relation_schema, "relation_name": relation_name,
        "relation_key": _physical_relation_key(
            connection, relation_schema, relation_name
        ),
        "variables": converted,
        "weight_variable_id": weight_variable_id,
        "weight_source_name": weight_source_name,
    }


def _quote_relation(connection: Any, schema: str | None, relation: str) -> str:
    quote = connection.dialect.identifier_preparer.quote
    return f"{quote(schema)}.{quote(relation)}" if schema else quote(relation)


def _physical_relation_key(
    connection: Any, schema: str | None, relation: str,
) -> str:
    return _canonical_json({
        "dialect_family": connection.dialect.name,
        "database": str(connection.engine.url.database or ""),
        "qualified_relation": _quote_relation(connection, schema, relation),
    })


def _query(parent: Mapping[str, Any], connection: Any, query_sql: str) -> str:
    quote = connection.dialect.identifier_preparer.quote
    projection = ", ".join(
        f"{quote(row['physical_name'])} AS {quote(row['source_name'])}"
        for row in parent["variables"]
    )
    relation = _quote_relation(connection, parent["relation_schema"], parent["relation_name"])
    parent_wrapper = parse(
        f"WITH parent AS (SELECT {projection} FROM {relation}) SELECT 1",
        read="sqlite",
    )[0]
    parent_cte = parent_wrapper.args["with_"].expressions[0]
    tree = parse(query_sql, read="sqlite")[0].copy()
    existing = tree.args.get("with_")
    if existing is None:
        tree.set("with_", exp.With(expressions=[parent_cte], recursive=False))
    else:
        existing.set("expressions", [parent_cte, *existing.expressions])
    return tree.sql(dialect="sqlite")


def register_transformation(
    *, database_url: str, parent_dataset_id: str, query_sql: str,
    columns: Sequence[Mapping[str, Any]], parent_kind: str = "core",
    output_mode: str = "materialized", transformation_name: str | None = None,
    row_semantics: str | None = None, metadata_policy: str = "declared",
    server_version_constraint: str = SQLITE_SERVER_VERSION_CONSTRAINT,
    weight_variable: str | None = None,
) -> dict[str, Any]:
    """Register one immutable, monotonically versioned SELECT definition."""
    profile = _workflow_profile(database_url)
    canonical_sql = _mask_and_validate_sql(query_sql, profile.name)
    parameter_names = _parameter_names(canonical_sql)
    stable_name = str(transformation_name or "").strip()
    if not stable_name:
        raise TransformationError(
            "stable_name_required", "transformation_name is required."
        )
    if server_version_constraint != SQLITE_SERVER_VERSION_CONSTRAINT:
        raise TransformationError(
            "server_version_constraint_invalid",
            f"server_version_constraint must be {SQLITE_SERVER_VERSION_CONSTRAINT!r}.",
        )
    if row_semantics is not None and row_semantics not in {"one_to_one", "filter", "aggregate", "join", "reshape", "other"}:
        raise TransformationError("invalid_row_semantics", "row_semantics is not declared by the profile.")
    if metadata_policy not in {"none", "identity_only", "declared"}:
        raise TransformationError("invalid_metadata_policy", "metadata_policy is not declared by the profile.")
    if output_mode != "materialized":
        raise TransformationError(
            "output_mode_not_supported", "This milestone supports materialized outputs only."
        )
    schema_document = _normalize_columns(columns, weight_variable)
    row_semantics = _validate_declared_semantics(
        canonical_sql, schema_document,
        row_semantics=row_semantics, metadata_policy=metadata_policy,
    )
    deterministic_order = _deterministic_order(canonical_sql, profile.name)
    output_names = {item["physical_name"] for item in schema_document["variables"]}
    if any(item["expression"] not in output_names for item in deterministic_order):
        raise TransformationError(
            "deterministic_order_invalid",
            "Every ORDER BY column must be present in the declared output schema.",
        )
    kinds_by_name = {
        item["physical_name"]: item["logical_storage_kind"]
        for item in schema_document["variables"]
    }
    if any(
        item["collation"] is not None
        and kinds_by_name[item["expression"]] != "string"
        for item in deterministic_order
    ):
        raise TransformationError(
            "deterministic_order_invalid",
            "Non-text ORDER BY expressions cannot declare a collation.",
        )
    if any(
        item["collation"] is None
        and kinds_by_name[item["expression"]] == "string"
        for item in deterministic_order
    ):
        raise TransformationError(
            "deterministic_order_invalid",
            "Text ORDER BY expressions require an explicit fixed dialect collation.",
        )
    parameters_document = [{
        "parameter_ordinal": ordinal, "parameter_name": name,
        "logical_type": "json", "is_nullable": True,
        "default_canonical_json": None, "is_sensitive": False,
    } for ordinal, name in enumerate(parameter_names, start=1)]
    definition_id = str(uuid5(NAMESPACE_URL, "openstatspec-definition:" + stable_name))
    engine = _workflow_engine(database_url, profile.name)
    tables = workflow_catalog(MetaData())
    with engine.begin() as connection:
        _assert_sqlite_server_version(connection, server_version_constraint)
        parent = _parent_snapshot(connection, parent_kind, parent_dataset_id)
        parent_columns = {item["source_name"] for item in parent["variables"]}
        declared_sources = {
            lineage["parent_column"]
            for variable in schema_document["variables"]
            for lineage in variable["lineage"]
        }
        unknown_sources = sorted(declared_sources - parent_columns)
        if unknown_sources:
            raise TransformationError(
                "lineage_source_not_found",
                f"Lineage source is not present in parent: {unknown_sources[0]!r}.",
            )
        if schema_document["weight"] is not None:
            if row_semantics not in {"one_to_one", "filter"}:
                raise TransformationError(
                    "invalid_weight", "Identity weight propagation requires one_to_one or filter rows."
                )
            weight_name = schema_document["weight"]["physical_name"]
            descriptor = next(
                item for item in schema_document["variables"]
                if item["physical_name"] == weight_name
            )
            parent_column = descriptor["lineage"][0]["parent_column"]
            if (
                parent["weight_variable_id"] is None
                or parent_column != parent["weight_source_name"]
            ):
                raise TransformationError(
                    "invalid_weight",
                    "The output weight must be the parent's verified identity weight.",
                )
        create_workflow_catalog(connection, tables)
        definition = connection.execute(
            select(tables.transformation_definition)
            .where(tables.transformation_definition.c.stable_name == stable_name)
        ).mappings().one_or_none()
        if definition is None:
            connection.execute(insert(tables.transformation_definition).values(
                transformation_id=definition_id, stable_name=stable_name,
                title=stable_name, created_at=_now(),
            ))
            versions = []
        else:
            definition_id = str(definition["transformation_id"])
            versions = connection.execute(
                select(tables.transformation_version)
                .where(tables.transformation_version.c.transformation_id == definition_id)
                .order_by(tables.transformation_version.c.version_number)
            ).mappings().all()
        schema_json = _canonical_json(schema_document)
        matching = next((
            row for row in versions
            if row["query_sql"] == canonical_sql
            and row["dialect_family"] == profile.name
            and row["server_version_constraint"] == server_version_constraint
            and row["output_mode"] == output_mode
            and row["row_semantics"] == row_semantics
            and row["metadata_policy"] == metadata_policy
            and row["output_schema_json"] == schema_json
        ), None)
        if matching is not None:
            version_id = str(matching["transformation_version_id"])
            definition_hash = str(matching["definition_hash"])
            version_number = int(matching["version_number"])
        else:
            version_number = 1 + max(
                (int(row["version_number"]) for row in versions), default=0
            )
            definition_hash = _definition_hash(
                transformation_id=definition_id, version_number=version_number,
                query_sql=canonical_sql, dialect_family=profile.name,
                server_version_constraint=server_version_constraint,
                output_mode=output_mode, row_semantics=row_semantics,
                metadata_policy=metadata_policy, output_schema=schema_document,
                deterministic_order=deterministic_order,
                parameter_declarations=parameters_document,
            )
            version_id = str(uuid5(NAMESPACE_URL, "openstatspec-version:" + definition_hash))
            connection.execute(insert(tables.transformation_version).values(
                transformation_version_id=version_id,
                transformation_id=definition_id, version_number=version_number,
                query_sql=canonical_sql, dialect_family=profile.name,
                server_version_constraint=server_version_constraint,
                output_mode=output_mode, row_semantics=row_semantics,
                metadata_policy=metadata_policy,
                deterministic_order_json=_canonical_json(deterministic_order),
                output_schema_json=schema_json,
                definition_hash=definition_hash, published_at=_now(),
            ))
            if parameters_document:
                connection.execute(insert(tables.transformation_parameter), [{
                    "transformation_version_id": version_id, **item,
                } for item in parameters_document])
    return {
        "profile_id": PROFILE_ID, "transformation_id": definition_id,
        "transformation_version_id": version_id, "version_number": version_number,
        "definition_hash": definition_hash, "parameters": parameter_names,
        "output_mode": output_mode,
    }


def _validated_parameters(
    declarations: Sequence[Mapping[str, Any]], parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], str, dict[str, str]]:
    values = dict(parameters)
    expected = [str(row["parameter_name"]) for row in declarations]
    if set(values) != set(expected):
        raise TransformationError(
            "parameter_mismatch",
            f"Expected parameters {sorted(expected)!r}; received {sorted(values)!r}.",
        )
    if any(not isinstance(value, _SCALAR_TYPES) for value in values.values()):
        raise TransformationError("parameter_type", "Parameters must be JSON scalar values.")
    if any(isinstance(value, float) for value in values.values()):
        raise TransformationError(
            "parameter_type",
            "Fractional JSON numbers are unsupported; use a future typed binary64 parameter.",
        )
    if any(
        isinstance(value, int) and not isinstance(value, bool)
        and abs(value) > 9_007_199_254_740_991
        for value in values.values()
    ):
        raise TransformationError(
            "parameter_type", "Integer parameters must be in the RFC 8785 safe domain."
        )
    envelopes = []
    encoded_by_name: dict[str, str] = {}
    for row in declarations:
        name = str(row["parameter_name"])
        envelope = {"t": str(row["logical_type"]), "v": values[name]}
        encoded = _canonical_json(envelope)
        encoded_by_name[name] = encoded
        envelopes.append({
            "parameter_ordinal": int(row["parameter_ordinal"]),
            "parameter_name": name,
            "logical_type": str(row["logical_type"]),
            "value_envelope": envelope,
        })
    hash_envelope = {
        "hash_kind": "parameter_set",
        "hash_version": "openstatspec-parameter-set-v1",
        "parameters": envelopes,
    }
    return values, _sha(_canonical_json(hash_envelope)), encoded_by_name


def _next_run_event_ordinal(
    connection: Any, tables: WorkflowTables, run_id: str,
) -> int:
    ordinals = connection.execute(
        select(tables.transformation_event.c.event_ordinal)
        .where(tables.transformation_event.c.transformation_run_id == run_id)
    ).scalars().all()
    return max((int(value) for value in ordinals), default=0) + 1


def _append_run_event(
    connection: Any, tables: WorkflowTables, *, run_id: str,
    code: str, phase: str,
) -> None:
    connection.execute(insert(tables.transformation_event).values(
        transformation_event_id=str(uuid4()), transformation_run_id=run_id,
        event_ordinal=_next_run_event_ordinal(connection, tables, run_id),
        severity="error", event_code=code, execution_phase=phase,
        safe_detail_json=_canonical_json({
            "error_code": code, "execution_phase": phase,
            "correlation_id_hash": _sha(run_id),
        }),
        created_at=_now(),
    ))


def _record_failure(
    engine: Any, tables: WorkflowTables, run_id: str, code: str, phase: str,
) -> None:
    with engine.begin() as connection:
        changed = connection.execute(
            update(tables.transformation_run)
            .where(
                tables.transformation_run.c.transformation_run_id == run_id,
                tables.transformation_run.c.status == "started",
            )
            .values(status="failed", completed_at=_now())
        ).rowcount
        if changed != 1:
            raise TransformationError(
                "publication_failed", "Run failure transition was not started -> failed."
            )
        _append_run_event(
            connection, tables, run_id=run_id, code=code, phase=phase
        )


def _record_cleanup_failure(
    engine: Any, tables: WorkflowTables, run_id: str,
) -> None:
    with engine.begin() as connection:
        status = connection.execute(
            select(tables.transformation_run.c.status).where(
                tables.transformation_run.c.transformation_run_id == run_id
            )
        ).scalar_one()
        if status != "started":
            raise TransformationError(
                "publication_failed",
                "Cleanup failure may only quarantine a started run.",
            )
        _append_run_event(
            connection, tables, run_id=run_id,
            code="cleanup_failed", phase="cleanup",
        )


def _relation_kind(connection: Any, relation_name: str) -> str | None:
    inspector = inspect(connection)
    if relation_name in inspector.get_view_names():
        return "view"
    if relation_name in inspector.get_table_names():
        return "table"
    return None


def _drop_relation_if_present(
    connection: Any, relation_name: str,
) -> None:
    relation_kind = _relation_kind(connection, relation_name)
    if relation_kind is None:
        return
    quote = connection.dialect.identifier_preparer.quote
    connection.exec_driver_sql(
        f"DROP {relation_kind.upper()} {quote(relation_name)}"
    )


def _assert_relation_absent(connection: Any, relation_name: str) -> None:
    if _relation_kind(connection, relation_name) is not None:
        raise TransformationError(
            "cleanup_failed",
            "The recorded profile-owned relation is still present after cleanup.",
        )


def _assert_staging_key_uniquely_owned(
    connection: Any, tables: WorkflowTables, run_id: str, relation_key: str,
) -> None:
    owners = connection.execute(
        select(tables.transformation_run.c.transformation_run_id).where(
            tables.transformation_run.c.staging_relation_key == relation_key
        )
    ).scalars().all()
    if [str(value) for value in owners] != [run_id]:
        raise TransformationError(
            "reconciliation_ownership_unverified",
            "The recorded staging relation key is not uniquely owned by this run.",
        )


def _cleanup_execution_relations(
    engine: Any, tables: WorkflowTables, run_id: str,
) -> None:
    with engine.begin() as connection:
        relation_key = connection.execute(
            select(tables.transformation_run.c.staging_relation_key).where(
                tables.transformation_run.c.transformation_run_id == run_id
            )
        ).scalar_one()
        staging_name = _owned_staging_relation_name(relation_key)
        _assert_staging_key_uniquely_owned(
            connection, tables, run_id, relation_key
        )
        _drop_relation_if_present(connection, staging_name)
        _assert_relation_absent(connection, staging_name)


@contextmanager
def _sqlite_read_authorizer(
    connection: Any, allowed_relations: set[tuple[str, str]],
):
    """Defense in depth: SQLite may read only the declared physical input."""
    raw = connection.connection.driver_connection
    allowed_actions = {
        sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
        getattr(sqlite3, "SQLITE_RECURSIVE", -1),
    }

    def authorize(action, arg1, arg2, database, _trigger):
        if action not in allowed_actions:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and (
            str(database or "main"), str(arg1)
        ) not in allowed_relations:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    raw.set_authorizer(authorize)
    try:
        yield
    finally:
        raw.set_authorizer(None)


def _validate_order_key(
    connection: Any, query_sql: str,
    order_descriptors: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any], allowed_relations: set[tuple[str, str]],
) -> None:
    quote = connection.dialect.identifier_preparer.quote
    keys = ", ".join(
        quote(str(item["expression"]))
        + (f" COLLATE {item['collation']}" if item["collation"] else "")
        for item in order_descriptors
    )
    null_predicate = " OR ".join(
        f"{quote(str(item['expression']))} IS NULL"
        for item in order_descriptors
    )
    wrapped = f"({query_sql}) ordered_result"
    with _sqlite_read_authorizer(connection, allowed_relations):
        has_null = connection.exec_driver_sql(
            f"SELECT 1 FROM {wrapped} WHERE {null_predicate} LIMIT 1",
            dict(parameters),
        ).first()
        has_duplicate = connection.exec_driver_sql(
            (
                f"SELECT 1 FROM {wrapped} GROUP BY {keys} "
                f"HAVING COUNT(*) > 1 LIMIT 1"
            ),
            dict(parameters),
        ).first()
    if has_null:
        raise TransformationError(
            "null_order_key", "Deterministic ORDER BY values must be non-null."
        )
    if has_duplicate:
        raise TransformationError(
            "non_unique_order_key", "Deterministic ORDER BY tuple must be unique."
        )


def _validate_nullability(
    connection: Any, query_sql: str, schema: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any], allowed_relations: set[tuple[str, str]],
) -> None:
    quote = connection.dialect.identifier_preparer.quote
    required = [item["name"] for item in schema if not item["is_nullable"]]
    if not required:
        return
    predicate = " OR ".join(f"{quote(item)} IS NULL" for item in required)
    with _sqlite_read_authorizer(connection, allowed_relations):
        violation = connection.exec_driver_sql(
            f"SELECT 1 FROM ({query_sql}) output_result WHERE {predicate} LIMIT 1",
            dict(parameters),
        ).first()
    if violation:
        raise TransformationError(
            "output_validation_failed",
            "A declared non-null output column produced NULL.",
        )


def _input_set_hash(parent: Mapping[str, Any]) -> str:
    envelope = {
        "hash_kind": "input_set",
        "hash_version": "openstatspec-input-set-v1",
        "inputs": [{
            "input_ordinal": 1, "input_alias": "parent",
            "input_kind": parent["kind"], "dataset_id": parent["dataset_id"],
            "physical_relation_key": parent["relation_key"],
            "schema_hash": parent["schema_hash"],
            "snapshot_hash_kind": "relation_snapshot",
            "snapshot_hash_algorithm": "sha256",
            "snapshot_hash_version": "openstatspec-relation-snapshot-v1",
            "snapshot_hash_value": parent["dataset_hash"],
        }],
    }
    return _sha(_canonical_json(envelope))


def _assert_derived_integrity(
    connection: Any, tables: WorkflowTables, dataset: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], str]:
    derived_id = str(dataset["derived_dataset_id"])
    disposition = connection.execute(
        select(tables.derived_dataset_disposition_event.c.event_kind).where(
            tables.derived_dataset_disposition_event.c.derived_dataset_id == derived_id,
            tables.derived_dataset_disposition_event.c.event_kind.in_((
                "retired", "physical_removal_requested", "physical_removed",
            )),
        )
    ).first()
    if disposition is not None:
        raise TransformationError(
            "derived_unavailable",
            "The deterministic derived result is retired or pending/finished removal.",
        )
    relation_name = str(dataset["physical_relation_name"])
    relation_schema = dataset["physical_relation_schema"]
    inspector = inspect(connection)
    if (
        relation_name not in inspector.get_table_names(schema=relation_schema)
        or relation_name in inspector.get_view_names(schema=relation_schema)
    ):
        raise TransformationError(
            "derived_unavailable", "The deterministic derived relation is unavailable."
        )
    try:
        _assert_trigger_definitions(
            connection,
            _derived_trigger_sql(connection, derived_id, relation_name),
            code="derived_corrupt",
        )
        variables = connection.execute(
            select(tables.derived_variable)
            .where(tables.derived_variable.c.derived_dataset_id == derived_id)
            .order_by(tables.derived_variable.c.column_ordinal)
        ).mappings().all()
        actual_hash = _relation_snapshot_hash(
            connection,
            relation_schema=relation_schema,
            relation_name=relation_name,
            variables=[{
                "physical_name": row["physical_name"],
                "storage_kind": row["logical_storage_kind"],
            } for row in variables],
            ordinal_name="__row_ordinal",
            schema_hash=str(dataset["schema_hash"]),
        )
    except TransformationError:
        raise
    except Exception as error:
        raise TransformationError(
            "derived_corrupt", "The deterministic derived relation cannot be validated."
        ) from error
    if (
        dataset["content_hash_kind"] != "relation_snapshot"
        or dataset["content_hash_algorithm"] != "sha256"
        or dataset["content_hash_version"] != "openstatspec-relation-snapshot-v1"
        or actual_hash != dataset["content_hash"]
    ):
        raise TransformationError(
            "derived_corrupt", "The deterministic derived relation content is corrupt."
        )
    return variables, actual_hash


def execute_transformation(
    *, database_url: str, transformation_version_id: str,
    parent_dataset_id: str, parameters: Mapping[str, Any] | None = None,
    parent_kind: str = "core", dataset_name: str | None = None,
    weight_variable: str | None = None,
) -> dict[str, Any]:
    """Execute one registered version atomically and return an immutable dataset."""
    profile = _workflow_profile(database_url)
    version_id = _uuid(transformation_version_id, "transformation_version_id")
    engine = _workflow_engine(database_url, profile.name)
    tables = workflow_catalog(MetaData())
    with engine.begin() as connection:
        create_workflow_catalog(connection, tables)
        version = connection.execute(
            select(tables.transformation_version)
            .where(tables.transformation_version.c.transformation_version_id == version_id)
        ).mappings().one_or_none()
        if version is None:
            raise TransformationError("version_not_found", "Transformation version was not found.")
        if version["dialect_family"] != profile.name:
            raise TransformationError("dialect_mismatch", "Transformation was registered for another SQL dialect.")
        _assert_sqlite_server_version(connection, version["server_version_constraint"])
        if version["output_mode"] != "materialized":
            raise TransformationError(
                "output_mode_not_supported", "This milestone supports materialized outputs only."
            )
        parameter_rows = connection.execute(
            select(tables.transformation_parameter)
            .where(tables.transformation_parameter.c.transformation_version_id == version_id)
            .order_by(tables.transformation_parameter.c.parameter_ordinal)
        ).mappings().all()
        expected = [row["parameter_name"] for row in parameter_rows]
        computed_definition_hash = _definition_hash(
            transformation_id=version["transformation_id"],
            version_number=version["version_number"],
            query_sql=version["query_sql"],
            dialect_family=version["dialect_family"],
            server_version_constraint=version["server_version_constraint"],
            output_mode=version["output_mode"],
            row_semantics=version["row_semantics"],
            metadata_policy=version["metadata_policy"],
            output_schema=json.loads(version["output_schema_json"]),
            deterministic_order=json.loads(version["deterministic_order_json"]),
            parameter_declarations=[{
                key: row[key] for key in (
                    "parameter_ordinal", "parameter_name", "logical_type",
                    "is_nullable", "default_canonical_json", "is_sensitive",
                )
            } for row in parameter_rows],
        )
        if computed_definition_hash != version["definition_hash"]:
            raise TransformationError(
                "definition_hash_mismatch",
                "Published transformation version does not match its definition hash.",
            )
        values, parameters_hash, parameter_envelopes = _validated_parameters(
            parameter_rows, parameters or {}
        )
        parent = _parent_snapshot(connection, parent_kind, parent_dataset_id)
        schema_document = json.loads(version["output_schema_json"])
        _validate_declared_semantics(
            version["query_sql"], schema_document,
            row_semantics=version["row_semantics"],
            metadata_policy=version["metadata_policy"],
        )
        schema = []
        for descriptor in schema_document["variables"]:
            lineage = descriptor.get("lineage") or []
            schema.append({
                **descriptor,
                "ordinal": descriptor["column_ordinal"],
                "name": descriptor["physical_name"],
                "storage_kind": descriptor["logical_storage_kind"],
                "lineage": lineage,
                "lineage_kind": descriptor["lineage_kind"],
                "label": (descriptor.get("metadata") or {}).get("variable_label"),
                "measurement_level": (descriptor.get("metadata") or {}).get("measurement_level"),
            })
        declared_weight = schema_document.get("weight")
        declared_weight_name = None if declared_weight is None else declared_weight["physical_name"]
        if weight_variable is not None:
            normalized_requested = next(
                (item["physical_name"] for item in schema if item["name"] == weight_variable),
                weight_variable,
            )
            if normalized_requested != declared_weight_name:
                raise TransformationError(
                    "weight_mismatch", "Run weight must match the immutable version schema."
                )
        weight_variable = declared_weight_name
        input_hash = _input_set_hash(parent)
        identity_payload = {
            "version_id": version_id, "parameters_hash": parameters_hash,
            "input_hash": input_hash,
        }
        derived_id = str(uuid5(NAMESPACE_URL, "openstatspec-derived:" + _sha(_canonical_json(identity_payload))))
        existing = connection.execute(
            select(tables.derived_dataset)
            .where(tables.derived_dataset.c.derived_dataset_id == derived_id)
        ).mappings().one_or_none()
        if existing is not None:
            _assert_derived_integrity(connection, tables, existing)
            return {
                "derived_dataset_id": derived_id,
                "transformation_run_id": existing["transformation_run_id"],
                "physical_relation_name": existing["physical_relation_name"],
                "row_count": existing["row_count"], "output_mode": existing["output_mode"],
                "status": "already_exists",
            }
        run_id = str(uuid4())
        relation_name = "derived_" + UUID(derived_id).hex
        staging_name = _STAGING_PREFIX + UUID(run_id).hex
        staging_key = _staging_relation_key(staging_name)
        connection.execute(insert(tables.transformation_run).values(
            transformation_run_id=run_id, transformation_version_id=version_id,
            status="started", executor_identity="openstatspec-python",
            correlation_id=run_id, staging_relation_key=staging_key,
            engine_name=connection.dialect.name,
            engine_version=str(getattr(connection.dialect, "server_version_info", "unknown")),
            dialect_profile=profile.name, capability_snapshot_json=_canonical_json({"dialect_family": profile.name}),
            specification_commit=SPECIFICATION_COMMIT or "unreleased", definition_hash=version["definition_hash"],
            parameters_hash=parameters_hash, input_set_hash=input_hash, started_at=_now(),
        ))
        if expected:
            connection.execute(insert(tables.transformation_run_parameter), [{
                "transformation_run_id": run_id, "parameter_name": name,
                "parameter_ordinal": ordinal, "logical_type": "json",
                "value_envelope": parameter_envelopes[name],
                "value_hash": _sha(parameter_envelopes[name]), "is_sensitive": False,
            } for ordinal, name in enumerate(expected, start=1)])
        connection.execute(insert(tables.transformation_run_input).values(
            transformation_run_id=run_id, input_ordinal=1, input_alias="parent",
            input_kind=parent_kind,
            core_dataset_id=parent["dataset_id"] if parent_kind == "core" else None,
            derived_dataset_id=parent["dataset_id"] if parent_kind == "derived" else None,
            physical_relation_schema_snapshot=parent["relation_schema"],
            physical_relation_name_snapshot=parent["relation_name"],
            physical_relation_key_snapshot=parent["relation_key"],
            schema_hash=parent["schema_hash"], content_or_source_hash=parent["dataset_hash"],
            snapshot_hash_kind="relation_snapshot", snapshot_hash_algorithm="sha256",
            snapshot_hash_version="openstatspec-relation-snapshot-v1",
        ))
    schema_hash = _sha(version["output_schema_json"])
    phase = "input_validation"
    try:
        with engine.begin() as connection:
            parent = _parent_snapshot(connection, parent_kind, parent_dataset_id)
            if _input_set_hash(parent) != input_hash:
                raise TransformationError(
                    "input_hash_mismatch",
                    "Parent relation changed between run registration and execution.",
                )
            phase = "query_validation"
            full_query = _query(parent, connection, version["query_sql"])
            quote = connection.dialect.identifier_preparer.quote
            output_names = [row["name"] for row in schema]
            parent_reads = {(
                str(parent["relation_schema"] or "main"),
                str(parent["relation_name"]),
            )}
            with _sqlite_read_authorizer(
                connection, parent_reads,
            ):
                shape_result = connection.exec_driver_sql(
                    full_query, values,
                )
            actual_names = list(shape_result.keys())
            shape_result.close()
            if actual_names != output_names:
                raise TransformationError(
                    "output_schema_mismatch",
                    f"Query returned {actual_names!r}; expected {output_names!r}.",
                )
            output_mode = str(version["output_mode"])
            order_descriptors = json.loads(version["deterministic_order_json"])
            _validate_order_key(
                connection, full_query, order_descriptors, values, parent_reads,
            )
            _validate_nullability(
                connection, full_query, schema, values, parent_reads,
            )
            variables = [{
                "ordinal": item["ordinal"], "source_name": item["name"],
                "physical_name": item["physical_name"], "storage_kind": item["storage_kind"],
                "string_width": None,
            } for item in schema]
            preflight(profile, variables)
            row_count = 0
            phase = "staging"
            if output_mode == "materialized":
                relation = Table(
                    staging_name, MetaData(),
                    Column("__row_ordinal", BigInteger, primary_key=True, nullable=False),
                    *(Column(
                        item["physical_name"],
                        binary64_type() if item["storage_kind"] == "numeric" else Text(),
                        nullable=item["is_nullable"],
                    ) for item in schema),
                )
                relation.create(connection)
                with _sqlite_read_authorizer(connection, parent_reads):
                    query_result = connection.exec_driver_sql(
                        full_query, values,
                        execution_options={"stream_results": True},
                    )
                while True:
                    chunk = query_result.fetchmany(1000)
                    if not chunk:
                        break
                    rows = []
                    for raw in chunk:
                        row_count += 1
                        output = {"__row_ordinal": row_count}
                        for item, value in zip(schema, tuple(raw)):
                            if value is not None:
                                value = float(value) if item["storage_kind"] == "numeric" else str(value)
                            output[item["physical_name"]] = value
                        rows.append(output)
                    connection.execute(insert(relation), rows)
                relation_query = (
                    f"SELECT {', '.join(quote(item['physical_name']) for item in schema)} "
                    f"FROM {quote(staging_name)}"
                )
                _validate_order_key(
                    connection, relation_query, order_descriptors, {}, {("main", staging_name)}
                )
                connection.exec_driver_sql(
                    f"ALTER TABLE {quote(staging_name)} RENAME TO {quote(relation_name)}"
                )
                _create_derived_triggers(
                    connection, derived_id, relation_name
                )
            else:
                projected = ", ".join(
                    f"q.{quote(item['name'])} AS {quote(item['physical_name'])}"
                    for item in schema
                )
                window_order = ", ".join(
                    (
                        f"q.{quote(item['expression'])}"
                        + (f" COLLATE {item['collation']}" if item["collation"] else "")
                        + f" {item['direction'].upper()} NULLS {item['nulls'].upper()}"
                    )
                    for item in order_descriptors
                )
                view_sql = (
                    f"CREATE VIEW {quote(relation_name)} AS "
                    f"SELECT ROW_NUMBER() OVER (ORDER BY {window_order}) "
                    f"AS {quote('__row_ordinal')}, {projected} "
                    f"FROM ({full_query}) q"
                )
                connection.exec_driver_sql(view_sql)
                row_count = int(connection.execute(
                    text(f"SELECT COUNT(*) FROM {quote(relation_name)}")
                ).scalar_one())
            phase = "publication_validation"
            content_hash = _relation_snapshot_hash(
                connection, relation_schema=None, relation_name=relation_name,
                variables=variables, ordinal_name="__row_ordinal",
                schema_hash=schema_hash,
            )
            content_hash_policy = "computed"
            variable_ids: dict[str, str] = {}
            phase = "publication"
            _assert_relation_absent(
                connection, _owned_staging_relation_name(staging_key)
            )
            changed = connection.execute(
                update(tables.transformation_run)
                .where(
                    tables.transformation_run.c.transformation_run_id == run_id,
                    tables.transformation_run.c.status == "started",
                )
                .values(status="succeeded", completed_at=_now())
            ).rowcount
            if changed != 1:
                raise TransformationError("publication_failed", "Run state transition failed.")
            connection.execute(insert(tables.derived_dataset).values(
                derived_dataset_id=derived_id, transformation_run_id=run_id,
                run_status="succeeded", physical_relation_schema=None,
                physical_relation_name=relation_name,
                physical_relation_key=_physical_relation_key(connection, None, relation_name), output_mode=output_mode,
                row_count=row_count, schema_hash=schema_hash, content_hash=content_hash,
                content_hash_policy=content_hash_policy,
                content_hash_kind="relation_snapshot",
                content_hash_algorithm="sha256",
                content_hash_version="openstatspec-relation-snapshot-v1", published_at=_now(),
            ))
            parent_by_name = {row["source_name"]: row for row in parent["variables"]}
            for item in schema:
                variable_id = str(uuid5(UUID(derived_id), item["name"]))
                variable_ids[item["name"]] = variable_id
                metadata_json = (
                    _canonical_json(item["metadata"])
                    if item.get("metadata") else None
                )
                connection.execute(insert(tables.derived_variable).values(
                    derived_variable_id=variable_id, derived_dataset_id=derived_id,
                    column_ordinal=item["ordinal"],
                    physical_name=item["physical_name"], logical_storage_kind=item["storage_kind"],
                    is_nullable=item["is_nullable"],
                    variable_label=item.get("label"), metadata_json=metadata_json,
                    metadata_hash=(_sha(metadata_json) if metadata_json else None),
                    lineage_kind=item["lineage_kind"],
                ))
                for source_ordinal, lineage in enumerate(item["lineage"], start=1):
                    source_name = lineage["parent_column"]
                    source = parent_by_name.get(source_name)
                    if source is None:
                        raise TransformationError(
                            "lineage_source_not_found", f"Lineage source {source_name!r} is not present in parent."
                        )
                    connection.execute(insert(tables.derived_variable_lineage).values(
                        derived_variable_id=variable_id, source_ordinal=source_ordinal,
                        transformation_run_id=run_id, input_ordinal=1,
                        core_variable_id=source["variable_id"] if parent_kind == "core" else None,
                        parent_derived_variable_id=source["variable_id"] if parent_kind == "derived" else None,
                        expression_role=lineage["expression_role"],
                    ))
            if weight_variable:
                connection.execute(insert(tables.derived_dataset_weight_variable).values(
                    derived_dataset_id=derived_id, derived_variable_id=variable_ids[weight_variable],
                    derivation_kind=declared_weight["derivation_kind"],
                    meaning=declared_weight["meaning"],
                ))
    except Exception as error:
        code = error.code if isinstance(error, TransformationError) else "execution_failed"
        try:
            _cleanup_execution_relations(engine, tables, run_id)
        except Exception as cleanup_error:
            _record_cleanup_failure(engine, tables, run_id)
            raise TransformationError(
                "cleanup_failed",
                "Profile-owned staging cleanup failed; the run is quarantined.",
            ) from cleanup_error
        _record_failure(engine, tables, run_id, code, phase)
        raise
    return {
        "derived_dataset_id": derived_id, "transformation_run_id": run_id,
        "physical_relation_name": relation_name, "row_count": row_count,
        "output_mode": version["output_mode"], "status": "succeeded",
    }


def derive_dataset(
    *, database_url: str, parent_dataset_id: str, query_sql: str,
    columns: Sequence[Mapping[str, Any]], parameters: Mapping[str, Any] | None = None,
    parent_kind: str = "core", output_mode: str = "materialized",
    transformation_name: str | None = None, dataset_name: str | None = None,
    weight_variable: str | None = None, row_semantics: str | None = None,
    metadata_policy: str = "declared",
    server_version_constraint: str = SQLITE_SERVER_VERSION_CONSTRAINT,
) -> dict[str, Any]:
    """Register and execute one immutable transformation."""
    registered = register_transformation(
        database_url=database_url, parent_dataset_id=parent_dataset_id,
        parent_kind=parent_kind, query_sql=query_sql, columns=columns,
        output_mode=output_mode, transformation_name=transformation_name,
        weight_variable=weight_variable, row_semantics=row_semantics,
        metadata_policy=metadata_policy,
        server_version_constraint=server_version_constraint,
    )
    return execute_transformation(
        database_url=database_url,
        transformation_version_id=registered["transformation_version_id"],
        parent_dataset_id=parent_dataset_id, parent_kind=parent_kind,
        parameters=parameters, dataset_name=dataset_name, weight_variable=weight_variable,
    )


def reconcile_started_runs(
    *, database_url: str, older_than_seconds: int = 0,
) -> dict[str, Any]:
    """Reconcile started runs using only their durable, profile-owned staging key."""
    if older_than_seconds < 0:
        raise TransformationError(
            "reconciliation_invalid", "older_than_seconds cannot be negative."
        )
    profile = _workflow_profile(database_url)
    engine = _workflow_engine(database_url, profile.name)
    tables = workflow_catalog(MetaData())
    cutoff = _now() - timedelta(seconds=older_than_seconds)
    reconciled: list[str] = []
    with engine.begin() as connection:
        create_workflow_catalog(connection, tables)
        runs = connection.execute(
            select(tables.transformation_run).where(
                tables.transformation_run.c.status == "started",
                tables.transformation_run.c.started_at <= cutoff,
            ).order_by(tables.transformation_run.c.transformation_run_id)
        ).mappings().all()
        run_ids = {str(row["transformation_run_id"]) for row in runs}
        published = connection.execute(
            select(tables.derived_dataset.c.transformation_run_id).where(
                tables.derived_dataset.c.transformation_run_id.in_(run_ids)
            )
        ).scalars().all() if run_ids else []
        if published:
            raise TransformationError(
                "profile_incompatible", "A started run already owns a derived dataset."
            )

        for row in runs:
            run_id = str(row["transformation_run_id"])
            relation_key = row["staging_relation_key"]
            staging_name = _owned_staging_relation_name(relation_key)
            _assert_staging_key_uniquely_owned(
                connection, tables, run_id, relation_key
            )
            _drop_relation_if_present(connection, staging_name)
            _assert_relation_absent(connection, staging_name)
            changed = connection.execute(
                update(tables.transformation_run).where(
                    tables.transformation_run.c.transformation_run_id == run_id,
                    tables.transformation_run.c.status == "started",
                ).values(status="failed", completed_at=_now())
            ).rowcount
            if changed != 1:
                raise TransformationError(
                    "publication_failed",
                    "Reconciliation run transition was not started -> failed.",
                )
            _append_run_event(
                connection, tables, run_id=run_id,
                code="interrupted_run", phase="reconciliation",
            )
            reconciled.append(run_id)
    return {
        "reconciled": len(reconciled),
        "transformation_run_ids": reconciled,
    }


def _next_disposition_ordinal(
    connection: Any, tables: WorkflowTables, derived_dataset_id: str,
) -> int:
    ordinals = connection.execute(
        select(tables.derived_dataset_disposition_event.c.event_ordinal)
        .where(
            tables.derived_dataset_disposition_event.c.derived_dataset_id
            == derived_dataset_id
        )
    ).scalars().all()
    return max((int(value) for value in ordinals), default=0) + 1


def _append_disposition(
    connection: Any, tables: WorkflowTables, *, derived_dataset_id: str,
    event_kind: str, actor_identity: str, reason: str,
    prior_content_hash: str,
) -> None:
    connection.execute(insert(tables.derived_dataset_disposition_event).values(
        disposition_event_id=str(uuid4()), derived_dataset_id=derived_dataset_id,
        event_ordinal=_next_disposition_ordinal(
            connection, tables, derived_dataset_id
        ),
        event_kind=event_kind, actor_identity=actor_identity, reason=reason,
        prior_content_hash=prior_content_hash, created_at=_now(),
    ))


def retire_derived_dataset(
    *, database_url: str, derived_dataset_id: str,
    actor_identity: str, reason: str,
) -> dict[str, Any]:
    """Append a retirement event without mutating or removing publication."""
    derived_id = _uuid(derived_dataset_id, "derived_dataset_id")
    if not actor_identity.strip() or not reason.strip():
        raise TransformationError(
            "disposition_invalid", "actor_identity and reason are required."
        )
    profile = validate_connection_url(database_url)
    engine = _workflow_engine(database_url, profile.name)
    tables = workflow_catalog(MetaData())
    with engine.begin() as connection:
        create_workflow_catalog(connection, tables)
        dataset = connection.execute(
            select(tables.derived_dataset).where(
                tables.derived_dataset.c.derived_dataset_id == derived_id
            )
        ).mappings().one_or_none()
        if dataset is None:
            raise TransformationError("derived_not_found", "Derived dataset was not found.")
        _append_disposition(
            connection, tables, derived_dataset_id=derived_id,
            event_kind="retired", actor_identity=actor_identity, reason=reason,
            prior_content_hash=dataset["content_hash"],
        )
    return {"derived_dataset_id": derived_id, "status": "retired"}


def remove_derived_relation(
    *, database_url: str, derived_dataset_id: str,
    actor_identity: str, reason: str,
) -> dict[str, Any]:
    """Audit request, remove only profile-owned output, then audit completion."""
    derived_id = _uuid(derived_dataset_id, "derived_dataset_id")
    if not actor_identity.strip() or not reason.strip():
        raise TransformationError(
            "disposition_invalid", "actor_identity and reason are required."
        )
    profile = validate_connection_url(database_url)
    engine = _workflow_engine(database_url, profile.name)
    tables = workflow_catalog(MetaData())
    with engine.begin() as connection:
        create_workflow_catalog(connection, tables)
        dataset = connection.execute(
            select(tables.derived_dataset).where(
                tables.derived_dataset.c.derived_dataset_id == derived_id
            )
        ).mappings().one_or_none()
        if dataset is None:
            raise TransformationError("derived_not_found", "Derived dataset was not found.")
        completed = connection.execute(
            select(tables.derived_dataset_disposition_event.c.event_kind).where(
                tables.derived_dataset_disposition_event.c.derived_dataset_id
                == derived_id,
                tables.derived_dataset_disposition_event.c.event_kind
                == "physical_removed",
            )
        ).first()
        if completed:
            return {"derived_dataset_id": derived_id, "status": "physical_removed"}
        dependent_runs = connection.execute(
            select(tables.transformation_run_input.c.transformation_run_id).where(
                tables.transformation_run_input.c.input_kind == "derived",
                tables.transformation_run_input.c.derived_dataset_id == derived_id,
            )
        ).scalars().all()
        if dependent_runs:
            raise TransformationError(
                "derived_has_dependents",
                "A derived relation used by another run cannot be physically removed.",
            )
        requested = connection.execute(
            select(tables.derived_dataset_disposition_event.c.event_kind).where(
                tables.derived_dataset_disposition_event.c.derived_dataset_id
                == derived_id,
                tables.derived_dataset_disposition_event.c.event_kind
                == "physical_removal_requested",
            )
        ).first()
        if not requested:
            _append_disposition(
                connection, tables, derived_dataset_id=derived_id,
                event_kind="physical_removal_requested",
                actor_identity=actor_identity, reason=reason,
                prior_content_hash=dataset["content_hash"],
            )
        relation_schema = dataset["physical_relation_schema"]
        relation_name = dataset["physical_relation_name"]
        prior_hash = dataset["content_hash"]
    try:
        with engine.begin() as connection:
            inspector = inspect(connection)
            quote = connection.dialect.identifier_preparer.quote
            qualified = _quote_relation(connection, relation_schema, relation_name)
            views = set(inspector.get_view_names(schema=relation_schema))
            tables_present = set(inspector.get_table_names(schema=relation_schema))
            if relation_name in views:
                connection.exec_driver_sql(f"DROP VIEW {qualified}")
            elif relation_name in tables_present:
                connection.exec_driver_sql(f"DROP TABLE {qualified}")
        with engine.begin() as connection:
            inspector = inspect(connection)
            if (
                relation_name in inspector.get_view_names(schema=relation_schema)
                or relation_name in inspector.get_table_names(schema=relation_schema)
            ):
                raise TransformationError(
                    "cleanup_failed", "Physical relation is still present after DROP."
                )
            _append_disposition(
                connection, tables, derived_dataset_id=derived_id,
                event_kind="physical_removed", actor_identity=actor_identity,
                reason=reason, prior_content_hash=prior_hash,
            )
    except Exception as error:
        if isinstance(error, TransformationError):
            raise
        raise TransformationError("cleanup_failed", str(error)) from error
    return {"derived_dataset_id": derived_id, "status": "physical_removed"}


def reconcile_physical_removals(
    *, database_url: str, actor_identity: str = "openstatspec-reconciler",
) -> dict[str, Any]:
    """Resume every requested removal that lacks a terminal completion event."""
    profile = validate_connection_url(database_url)
    engine = _workflow_engine(database_url, profile.name)
    tables = workflow_catalog(MetaData())
    pending: list[tuple[str, str]] = []
    with engine.begin() as connection:
        create_workflow_catalog(connection, tables)
        requests = connection.execute(
            select(tables.derived_dataset_disposition_event).where(
                tables.derived_dataset_disposition_event.c.event_kind
                == "physical_removal_requested"
            ).order_by(
                tables.derived_dataset_disposition_event.c.derived_dataset_id,
                tables.derived_dataset_disposition_event.c.event_ordinal,
            )
        ).mappings().all()
        for request in requests:
            completed = connection.execute(
                select(tables.derived_dataset_disposition_event.c.event_kind).where(
                    tables.derived_dataset_disposition_event.c.derived_dataset_id
                    == request["derived_dataset_id"],
                    tables.derived_dataset_disposition_event.c.event_kind
                    == "physical_removed",
                )
            ).first()
            if not completed:
                pending.append((
                    str(request["derived_dataset_id"]), str(request["reason"])
                ))
    results = [
        remove_derived_relation(
            database_url=database_url, derived_dataset_id=dataset_id,
            actor_identity=actor_identity, reason=reason,
        )
        for dataset_id, reason in pending
    ]
    return {"reconciled": len(results), "results": results}

def validate_derived_dataset(*, database_url: str, derived_dataset_id: str) -> dict[str, Any]:
    """Validate without creating or migrating catalog state."""
    from .catalog_api import _assert_workflow
    from .database_urls import require_existing_database_url

    require_existing_database_url(database_url)
    derived_id = _uuid(derived_dataset_id, "derived_dataset_id")
    profile = validate_connection_url(database_url)
    engine = _workflow_engine(database_url, profile.name)
    tables = workflow_catalog(MetaData())
    with engine.connect() as connection:
        _assert_core_identity(connection, core_catalog(MetaData()))
        _assert_workflow(connection, tables)
        _validate_workflow_schema(connection, tables)
        dataset = connection.execute(
            select(tables.derived_dataset)
            .where(tables.derived_dataset.c.derived_dataset_id == derived_id)
        ).mappings().one_or_none()
        if dataset is None:
            raise TransformationError("derived_not_found", "Derived dataset was not found.")
        variables, _actual_content_hash = _assert_derived_integrity(connection, tables, dataset)
        run = connection.execute(
            select(tables.transformation_run)
            .where(tables.transformation_run.c.transformation_run_id == dataset["transformation_run_id"])
        ).mappings().one()
        if run["status"] != "succeeded":
            raise TransformationError("run_not_succeeded", "Derived dataset points to an incomplete run.")
        version = connection.execute(
            select(tables.transformation_version).where(
                tables.transformation_version.c.transformation_version_id
                == run["transformation_version_id"]
            )
        ).mappings().one()
        if run["definition_hash"] != version["definition_hash"]:
            raise TransformationError(
                "definition_hash_mismatch", "Run is not bound to its published version hash."
            )
        run_inputs = connection.execute(
            select(tables.transformation_run_input).where(
                tables.transformation_run_input.c.transformation_run_id
                == run["transformation_run_id"]
            )
        ).mappings().all()
        if len(run_inputs) != 1:
            raise TransformationError("input_set_invalid", "Single-parent run needs exactly one input.")
        run_input = run_inputs[0]
        valid_input_xor = (
            run_input["input_kind"] == "core"
            and run_input["core_dataset_id"] is not None
            and run_input["derived_dataset_id"] is None
        ) or (
            run_input["input_kind"] == "derived"
            and run_input["core_dataset_id"] is None
            and run_input["derived_dataset_id"] is not None
        )
        if not valid_input_xor:
            raise TransformationError("input_set_invalid", "Run input kind/ID XOR is invalid.")
        expected_relation_key = _physical_relation_key(
            connection, dataset["physical_relation_schema"],
            dataset["physical_relation_name"],
        )
        if dataset["physical_relation_key"] != expected_relation_key:
            raise TransformationError(
                "physical_relation_key_invalid", "Physical relation key is not NULL-safe canonical."
            )
        relation = Table(
            dataset["physical_relation_name"], MetaData(),
            schema=dataset["physical_relation_schema"], autoload_with=connection,
        )
        expected = {"__row_ordinal", *(item["physical_name"] for item in variables)}
        if set(relation.c.keys()) != expected:
            raise TransformationError("physical_schema_mismatch", "Physical columns do not match derived variables.")
        quote = connection.dialect.identifier_preparer.quote
        qualified = _quote_relation(
            connection, dataset["physical_relation_schema"], dataset["physical_relation_name"]
        )
        row_count, minimum, maximum, distinct_count = connection.execute(text(
            f"SELECT COUNT(*), MIN({quote('__row_ordinal')}), "
            f"MAX({quote('__row_ordinal')}), COUNT(DISTINCT {quote('__row_ordinal')}) FROM {qualified}"
        )).one()
        if int(row_count) != int(dataset["row_count"]):
            raise TransformationError("row_count_mismatch", "Registered row count differs from the relation.")
        if row_count and (minimum != 1 or maximum != row_count or distinct_count != row_count):
            raise TransformationError("row_ordinal_invalid", "__row_ordinal must be contiguous and unique.")
    return {
        "derived_dataset_id": derived_id, "valid": True,
        "row_count": int(row_count), "variable_count": len(variables), "profile_id": PROFILE_ID,
    }
