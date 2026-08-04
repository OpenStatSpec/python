"""In-place application without an OpenStatSpec undo or copy layer."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Column, DateTime, Float, Integer, MetaData, String, Table, Text, and_, case,
    create_engine, delete, inspect, insert, literal, null, select, text, update,
    or_,
)

from ..transform import (
    AssignOperation, BooleanExpression, ComparisonExpression,
    CreateVariableOperation, DeleteVariableOperation,
    ConditionalAssignOperation, ExecuteOperation, Operand,
    RecodeOperation, RecodeResult, ReplaceValueLabelsOperation,
    SetFormatOperation, SetMeasurementLevelOperation,
    SetVariableLabelOperation, TransformationPlan, TypedValue, ValueLabel,
    VariableDefinition, VariableSchema, bind_transformation_plan,
    transformation_plan_from_dict,
)
from .capabilities import effective_profile
from .dolt_conformance import DoltConformanceSource
from .normative import catalog as core_catalog
from .profiles import SqlProfile, preflight
from .wide import physical_name, require_verified_catalog
from .workflow import TransformationError


APPLY_CONTRACT = "openstatspec-in-place-transformation-v0.2"


@dataclass(frozen=True)
class InPlacePlanSubmission:
    """A canonical plan plus compact provenance for one atomic apply."""

    plan: TransformationPlan
    source_kind: str
    source_hash: str
    frontend_contract: str | None = None

    def __post_init__(self) -> None:
        if not self.source_kind or not self.source_hash:
            raise ValueError("source_kind and source_hash must be non-empty")


def in_place_transformation_capabilities() -> dict[str, Any]:
    return {
        "contract": APPLY_CONTRACT,
        "status": "experimental",
        "database_products": [
            "sqlite", "postgresql", "mysql", "mariadb", "dolt",
        ],
        "parent_kinds": ["core"],
        "mutation": "same_dataset_same_physical_wide_table",
        "commands": ["RECODE", "COMPUTE", "IF", "VARIABLE LABELS",
                     "VALUE LABELS", "FORMATS", "VARIABLE LEVEL",
                     "STRING", "DELETE VARIABLES", "EXECUTE"],
        "new_target_column": {
            "sqlite": True,
            "postgresql": True,
            "mysql": False,
            "mariadb": False,
            "dolt": False,
            "reason": "atomic create only on SQLite/PostgreSQL; other profiles require a pre-existing cataloged target",
        },
        "creates_derived_dataset": False,
        "creates_persistent_data_copy": False,
        "openstatspec_rollback_or_version_history": False,
        "dolt_requires_clean_working_set": True,
        "dolt_requires_expected_branch_and_head": True,
        "performs_dolt_commit": False,
        "execution_evidence": {
            "sqlite": "local_conformance",
            "postgresql": "service_conformance_required",
            "mysql": "service_conformance_required",
            "mariadb": "service_conformance_required",
            "dolt": "service_conformance_required",
        },
    }


def apply_audit_catalog(metadata: MetaData) -> Table:
    return Table(
        "transformation_apply",
        metadata,
        Column("apply_id", String(36), primary_key=True),
        Column("contract_id", String(128), nullable=False),
        Column("dataset_id", String(36), nullable=False),
        Column("database_profile", String(32), nullable=False),
        Column("physical_table_schema", String(255)),
        Column("physical_table_name", String(255), nullable=False),
        Column("source_kind", String(64)),
        Column("source_hash", String(64), nullable=False),
        Column("frontend_contract", String(128)),
        Column("plan_hash", String(64), nullable=False),
        Column("canonical_plan_json", Text, nullable=False),
        Column("actor", String(255), nullable=False),
        Column("status", String(16), nullable=False),
        Column("dolt_branch", String(255)),
        Column("dolt_head_before", String(128)),
        Column("dolt_head_after", String(128)),
        Column("operation_count", Integer, nullable=False),
        Column("started_at", DateTime, nullable=False),
        Column("completed_at", DateTime, nullable=False),
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _typed_value(value: TypedValue) -> float | str:
    return value.number() if value.type == "binary64" else str(value.value)


def _result_expression(result: RecodeResult, source: Any) -> Any:
    if result.kind == "copy":
        return source
    if result.kind == "system_missing":
        return null()
    assert result.value is not None
    return literal(_typed_value(result.value))


def _match_expression(match: Any, source: Any) -> Any:
    if match.kind == "system_missing":
        return source.is_(None)
    if match.kind == "range":
        return source.between(
            _typed_value(match.lower), _typed_value(match.upper)
        )
    return source.in_([_typed_value(value) for value in match.values])


def _input_schema(
    connection: Any,
    dataset_id: str,
    *,
    lock_dataset: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], VariableSchema]:
    core = core_catalog(MetaData())
    dataset_query = select(core.dataset).where(
        core.dataset.c.dataset_id == dataset_id
    )
    if lock_dataset:
        dataset_query = dataset_query.with_for_update()
    dataset = connection.execute(dataset_query).mappings().one_or_none()
    if dataset is None:
        raise TransformationError(
            "dataset_not_found", "The in-place target dataset does not exist."
        )
    variables = [dict(row) for row in connection.execute(
        select(core.variable)
        .where(core.variable.c.dataset_id == dataset_id)
        .order_by(core.variable.c.source_ordinal)
    ).mappings()]
    if not variables:
        raise TransformationError(
            "dataset_invalid", "The in-place target has no variables."
        )
    labels = connection.execute(
        select(
            core.variable_value_label_set.c.variable_id,
            core.value_label.c.ordinal,
            core.value_label.c.code_kind,
            core.value_label.c.numeric_code,
            core.value_label.c.string_code,
            core.value_label.c.label,
        ).select_from(
            core.variable_value_label_set.join(
                core.value_label,
                core.variable_value_label_set.c.value_label_set_id
                == core.value_label.c.value_label_set_id,
            )
        ).order_by(
            core.variable_value_label_set.c.variable_id,
            core.value_label.c.ordinal,
        )
    ).mappings()
    labels_by_variable: dict[str, list[ValueLabel]] = {}
    for row in labels:
        typed = (
            TypedValue.binary64(float(row["numeric_code"]))
            if row["code_kind"] == "numeric"
            else TypedValue.string(str(row["string_code"]))
        )
        labels_by_variable.setdefault(str(row["variable_id"]), []).append(
            ValueLabel(typed, str(row["label"]))
        )
    schema = VariableSchema(tuple(
        VariableDefinition(
            str(row["source_name"]),
            str(row["storage_kind"]),
            variable_label=row["variable_label"],
            value_labels=tuple(labels_by_variable.get(str(row["variable_id"]), [])),
            format_family=(
                "F" if str(row["print_format_family"]).upper() in {"F", "5"}
                and str(row["storage_kind"]) == "numeric" else None
            ),
            format_width=(
                row["print_format_width"]
                if str(row["print_format_family"]).upper() in {"F", "5"}
                and str(row["storage_kind"]) == "numeric" else None
            ),
            format_decimals=(
                row["print_format_decimals"]
                if str(row["print_format_family"]).upper() in {"F", "5"}
                and str(row["storage_kind"]) == "numeric" else None
            ),
            measurement_level=row["measurement_level"],
            declared_string_width=(
                int(row["declared_string_width"])
                if row["declared_string_width"] is not None else None
            ),
        )
        for row in variables
    ))
    return dict(dataset), variables, schema


def _target_identity_state(
    connection: Any,
    dataset_id: str,
    *,
    lock_dataset: bool = False,
) -> tuple[str, str | None, str, int]:
    """Return one locked catalog identity plus its actual relation count."""
    core = core_catalog(MetaData())
    query = (
        select(
            core.dataset.c.dataset_id,
            core.dataset.c.physical_table_schema,
            core.dataset.c.physical_table_name,
        )
        .where(core.dataset.c.dataset_id == dataset_id)
    )
    if lock_dataset:
        query = query.with_for_update()
    row = connection.execute(query).one_or_none()
    if row is None:
        raise TransformationError(
            "dataset_not_found", "The in-place target dataset does not exist."
        )
    schema = str(row.physical_table_schema) if row.physical_table_schema else None
    table_name = str(row.physical_table_name)
    relation_count = int(
        inspect(connection).has_table(table_name, schema=schema)
    )
    return str(row.dataset_id), schema, table_name, relation_count


def _physical_table_name(dataset: dict[str, Any]) -> str:
    table_name = dataset.get("physical_table_name")
    if not isinstance(table_name, str) or not table_name:
        raise TransformationError(
            "dataset_invalid", "The target lacks its physical wide-table name."
        )
    return table_name


def _replace_value_labels(
    connection: Any,
    *,
    core: Any,
    variable: dict[str, Any],
    labels: tuple[ValueLabel, ...],
) -> None:
    variable_id = str(variable["variable_id"])
    old_set = connection.execute(
        select(core.variable_value_label_set.c.value_label_set_id).where(
            core.variable_value_label_set.c.variable_id == variable_id
        )
    ).scalar_one_or_none()
    if old_set is not None:
        connection.execute(delete(core.value_label).where(
            core.value_label.c.value_label_set_id == old_set
        ))
        connection.execute(delete(core.variable_value_label_set).where(
            core.variable_value_label_set.c.variable_id == variable_id
        ))
        connection.execute(delete(core.value_label_set).where(
            core.value_label_set.c.value_label_set_id == old_set
        ))
    label_set_id = str(uuid4())
    connection.execute(insert(core.value_label_set).values(
        value_label_set_id=label_set_id,
        dataset_id=variable["dataset_id"],
        name=None,
    ))
    connection.execute(insert(core.variable_value_label_set).values(
        variable_id=variable_id,
        value_label_set_id=label_set_id,
    ))
    for label_ordinal, item in enumerate(labels, start=1):
        connection.execute(insert(core.value_label).values(
            value_label_id=str(uuid4()),
            value_label_set_id=label_set_id,
            ordinal=label_ordinal,
            code_kind="numeric" if item.value.type == "binary64" else "string",
            numeric_code=(item.value.number() if item.value.type == "binary64" else None),
            string_code=(str(item.value.value) if item.value.type == "string" else None),
            label=item.label,
        ))

def _delete_variable_metadata(
    connection: Any,
    *,
    core: Any,
    variable: dict[str, Any],
) -> None:
    """Delete one variable and every normative catalog row owned by it."""
    variable_id = str(variable["variable_id"])
    response_set_ids = list(connection.execute(
        select(core.multiple_response_member.c.multiple_response_set_id).where(
            core.multiple_response_member.c.variable_id == variable_id
        )
    ).scalars())
    label_set_id = connection.execute(
        select(core.variable_value_label_set.c.value_label_set_id).where(
            core.variable_value_label_set.c.variable_id == variable_id
        )
    ).scalar_one_or_none()
    connection.execute(delete(core.dataset_weight_variable).where(
        core.dataset_weight_variable.c.variable_id == variable_id
    ))
    connection.execute(delete(core.variable_set_member).where(
        core.variable_set_member.c.variable_id == variable_id
    ))
    connection.execute(delete(core.multiple_response_member).where(
        core.multiple_response_member.c.variable_id == variable_id
    ))
    for response_set_id in response_set_ids:
        remaining_member = connection.execute(
            select(core.multiple_response_member.c.variable_id).where(
                core.multiple_response_member.c.multiple_response_set_id
                == response_set_id
            )
        ).first()
        if remaining_member is None:
            connection.execute(delete(core.multiple_response_set).where(
                core.multiple_response_set.c.multiple_response_set_id
                == response_set_id
            ))
    connection.execute(delete(core.variable_attribute).where(
        core.variable_attribute.c.variable_id == variable_id
    ))
    connection.execute(delete(core.missing_rule).where(
        core.missing_rule.c.variable_id == variable_id
    ))
    connection.execute(delete(core.variable_value_label_set).where(
        core.variable_value_label_set.c.variable_id == variable_id
    ))
    if label_set_id is not None:
        still_used = connection.execute(
            select(core.variable_value_label_set.c.variable_id).where(
                core.variable_value_label_set.c.value_label_set_id == label_set_id
            )
        ).first()
        if still_used is None:
            connection.execute(delete(core.value_label).where(
                core.value_label.c.value_label_set_id == label_set_id
            ))
            connection.execute(delete(core.value_label_set).where(
                core.value_label_set.c.value_label_set_id == label_set_id
            ))
    connection.execute(delete(core.variable).where(
        core.variable.c.variable_id == variable_id
    ))


def _compact_variable_ordinals(
    connection: Any,
    *,
    core: Any,
    variables: list[dict[str, Any]],
) -> None:
    """Keep normative variable source order contiguous after deletion."""
    for source_ordinal, variable in enumerate(variables, start=1):
        if int(variable["source_ordinal"]) == source_ordinal:
            continue
        connection.execute(
            update(core.variable)
            .where(core.variable.c.variable_id == variable["variable_id"])
            .values(source_ordinal=source_ordinal)
        )
        variable["source_ordinal"] = source_ordinal


def _failure_boundary(_name: str) -> None:
    """Synthetic-test hook for schema/data/catalog/audit failure boundaries."""


def _operand_expression(
    operand: Operand, relation: Table, by_name: Mapping[str, dict[str, Any]],
) -> Any:
    if operand.kind == "literal":
        assert operand.value is not None
        return literal(_typed_value(operand.value))
    assert operand.variable is not None
    variable = by_name[operand.variable.casefold()]
    return relation.c[str(variable["physical_name"])]


def _predicate_expression(
    expression: ComparisonExpression | BooleanExpression,
    relation: Table,
    by_name: Mapping[str, dict[str, Any]],
) -> Any:
    if isinstance(expression, BooleanExpression):
        parts = [
            _predicate_expression(item, relation, by_name)
            for item in expression.operands
        ]
        return and_(*parts) if expression.operator == "and" else or_(*parts)
    left = _operand_expression(expression.left, relation, by_name)
    right = _operand_expression(expression.right, relation, by_name)
    return {
        "=": lambda: left == right,
        "<": lambda: left < right,
        "<=": lambda: left <= right,
        ">": lambda: left > right,
        ">=": lambda: left >= right,
    }[expression.operator]()


def _apply_plan_on_connection(
    connection: Any,
    *,
    dataset_id: str,
    submission: InPlacePlanSubmission,
    actor: str,
    database_profile: str,
    target_profile: SqlProfile,
    allow_schema_change: bool,
    allow_delete_variable: bool,
    dolt_branch: str | None,
    dolt_head: str | None,
    mutation_journal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before_identity = _target_identity_state(connection, dataset_id, lock_dataset=True)
    if before_identity[3] != 1:
        raise TransformationError(
            "physical_table_missing",
            "The target dataset's physical wide table does not exist.",
        )
    dataset, variables, schema = _input_schema(connection, dataset_id)
    table_name = _physical_table_name(dataset)
    plan = submission.plan
    bound = bind_transformation_plan(plan, schema)
    output_used_physical = {"__case_ordinal"}
    output_variables = [
        {
            "ordinal": source_ordinal,
            "source_name": variable.name,
            "physical_name": physical_name(
                variable.name, output_used_physical,
            ),
            "storage_kind": variable.storage_kind,
            "string_width": variable.declared_string_width,
        }
        for source_ordinal, variable in enumerate(
            bound.output_schema.variables, start=1,
        )
    ]
    preflight(target_profile, output_variables)
    output_by_name = {
        variable.name.casefold(): variable
        for variable in bound.output_schema.variables
    }
    audit = apply_audit_catalog(MetaData())
    if not inspect(connection).has_table("transformation_apply"):
        raise TransformationError(
            "in_place_audit_schema_missing",
            "The compact transformation_apply audit schema must be installed before apply.",
        )
    audit_columns = {
        str(column["name"])
        for column in inspect(connection).get_columns("transformation_apply")
    }
    if not {"source_kind", "frontend_contract"}.issubset(audit_columns):
        raise TransformationError(
            "in_place_audit_schema_outdated",
            "Re-run install_in_place_transformation_schema before apply.",
        )
    create_operations = [
        operation for operation in plan.operations
        if isinstance(operation, CreateVariableOperation)
        or (
            isinstance(operation, (RecodeOperation, AssignOperation))
            and operation.target_mode == "create"
        )
    ]
    schema_operations = create_operations + [
        operation for operation in plan.operations
        if isinstance(operation, DeleteVariableOperation)
    ]
    if schema_operations and not allow_schema_change:
        raise TransformationError(
            "schema_change_not_atomic",
            "This database profile has no coherent new-target strategy.",
        )
    if any(
        isinstance(operation, DeleteVariableOperation)
        for operation in plan.operations
    ) and not allow_delete_variable:
        raise TransformationError(
            "delete_variable_not_supported",
            "This SQLite runtime does not support ALTER TABLE DROP COLUMN.",
        )
    unsupported_targets = [
        operation.target for operation in create_operations
        if not isinstance(operation, CreateVariableOperation)
        and output_by_name[operation.target.casefold()].storage_kind != "numeric"
    ]
    if unsupported_targets:
        raise TransformationError(
            "in_place_target_type_unsupported",
            "New string targets require an explicit storage-width operation.",
        )

    core = core_catalog(MetaData())
    relation = Table(
        table_name, MetaData(), schema=dataset.get("physical_table_schema"),
        autoload_with=connection,
    )
    by_name = {str(row["source_name"]).casefold(): row for row in variables}
    used_physical = {str(row["physical_name"]).casefold() for row in variables}
    quote = connection.dialect.identifier_preparer.quote
    qualified_table = connection.dialect.identifier_preparer.format_table(relation)
    numeric_type = (
        "DOUBLE PRECISION" if connection.dialect.name == "postgresql" else "DOUBLE"
    )
    if mutation_journal is not None:
        mutation_journal.update({
            "table_schema": dataset.get("physical_table_schema"),
            "table_name": table_name,
            "added_columns": [],
            "target_rows": [],
        })

    if database_profile == "dolt":
        if dolt_branch is None or dolt_head is None:
            raise TransformationError(
                "dolt_context_required",
                "Dolt mutation requires the preflight branch and HEAD.",
            )
        locked_branch, locked_head, locked_dirty = _dolt_state(connection)
        if locked_branch != dolt_branch:
            raise TransformationError(
                "dolt_branch_mismatch",
                "The active Dolt branch changed after locking the dataset.",
            )
        if locked_head != dolt_head:
            raise TransformationError(
                "dolt_head_mismatch",
                "Dolt HEAD changed after locking the dataset.",
            )
        if locked_dirty != 0:
            raise TransformationError(
                "dolt_working_set_dirty",
                "The Dolt working set changed after locking the dataset.",
            )

    # Schema changes are allowed only on profiles whose DDL participates in
    # this apply transaction, so execute them in canonical operation order.
    # That preserves deterministic physical naming across delete/recreate flows.
    for operation in plan.operations:
        creates_target = (
            isinstance(operation, CreateVariableOperation)
            or (
                isinstance(operation, (RecodeOperation, AssignOperation))
                and operation.target_mode == "create"
            )
        )
        if creates_target:
            target_name = (
                operation.variable
                if isinstance(operation, CreateVariableOperation)
                else operation.target
            )
            target_physical = physical_name(target_name, used_physical)
            storage_kind = (
                operation.storage_kind
                if isinstance(operation, CreateVariableOperation)
                else "numeric"
            )
            string_width = (
                operation.declared_string_width
                if isinstance(operation, CreateVariableOperation)
                else None
            )
            column_type = numeric_type if storage_kind == "numeric" else "TEXT"
            null_clause = (
                "NULL" if storage_kind == "numeric"
                else "NOT NULL DEFAULT ''"
            )
            connection.exec_driver_sql(
                f"ALTER TABLE {qualified_table} ADD COLUMN "
                f"{quote(target_physical)} {column_type} {null_clause}"
            )
            created_target = {
                "variable_id": str(uuid4()),
                "dataset_id": dataset_id,
                "source_ordinal": len(variables) + 1,
                "source_name": target_name,
                "physical_name": target_physical,
                "storage_kind": storage_kind,
                "declared_string_width": string_width,
                "variable_label": None,
            }
            if mutation_journal is not None:
                mutation_journal["added_columns"].append(target_physical)
                mutation_journal["target_rows"].append(dict(created_target))
            _failure_boundary("schema")
            relation = Table(
                table_name, MetaData(), schema=dataset.get("physical_table_schema"),
                autoload_with=connection,
            )
            connection.execute(insert(core.variable).values(**created_target))
            variables.append(created_target)
            by_name[str(created_target["source_name"]).casefold()] = created_target
            _failure_boundary("catalog")
        if isinstance(operation, CreateVariableOperation):
            continue
        if isinstance(operation, RecodeOperation):
            source_variable = by_name[operation.source.casefold()]
            target_variable = by_name[operation.target.casefold()]
            source_column = relation.c[str(source_variable["physical_name"])]
            target_column = relation.c[str(target_variable["physical_name"])]
            expression = case(
                *[
                    (
                        _match_expression(rule.match, source_column),
                        _result_expression(rule.result, source_column),
                    )
                    for rule in operation.rules
                ],
                else_=_result_expression(operation.unmatched, source_column),
            )
            connection.execute(update(relation).values({target_column: expression}))
            _failure_boundary("data")
        elif isinstance(operation, AssignOperation):
            target = by_name[operation.target.casefold()]
            target_column = relation.c[str(target["physical_name"])]
            value = _operand_expression(operation.value, relation, by_name)
            connection.execute(update(relation).values({target_column: value}))
            _failure_boundary("data")
        elif isinstance(operation, ConditionalAssignOperation):
            target = by_name[operation.target.casefold()]
            target_column = relation.c[str(target["physical_name"])]
            value = _operand_expression(operation.value, relation, by_name)
            condition = _predicate_expression(operation.condition, relation, by_name)
            connection.execute(
                update(relation).where(condition).values({target_column: value})
            )
            _failure_boundary("data")
        elif isinstance(operation, SetVariableLabelOperation):
            variable = by_name[operation.variable.casefold()]
            connection.execute(update(core.variable).where(
                core.variable.c.variable_id == variable["variable_id"]
            ).values(variable_label=operation.label))
            _failure_boundary("catalog")
        elif isinstance(operation, ReplaceValueLabelsOperation):
            _replace_value_labels(
                connection,
                core=core,
                variable=by_name[operation.variable.casefold()],
                labels=operation.labels,
            )
            _failure_boundary("catalog")
        elif isinstance(operation, SetFormatOperation):
            variable = by_name[operation.variable.casefold()]
            connection.execute(update(core.variable).where(
                core.variable.c.variable_id == variable["variable_id"]
            ).values(
                print_format_family=operation.family,
                print_format_width=operation.width,
                print_format_decimals=operation.decimals,
                write_format_family=operation.family,
                write_format_width=operation.width,
                write_format_decimals=operation.decimals,
            ))
            _failure_boundary("catalog")
        elif isinstance(operation, SetMeasurementLevelOperation):
            variable = by_name[operation.variable.casefold()]
            connection.execute(update(core.variable).where(
                core.variable.c.variable_id == variable["variable_id"]
            ).values(measurement_level=operation.level))
            _failure_boundary("catalog")
        elif isinstance(operation, DeleteVariableOperation):
            variable = by_name[operation.variable.casefold()]
            connection.exec_driver_sql(
                f"ALTER TABLE {qualified_table} DROP COLUMN "
                f"{quote(variable['physical_name'])}"
            )
            _delete_variable_metadata(connection, core=core, variable=variable)
            by_name.pop(operation.variable.casefold(), None)
            used_physical.discard(str(variable["physical_name"]).casefold())
            variables = [
                row for row in variables
                if row["variable_id"] != variable["variable_id"]
            ]
            _compact_variable_ordinals(
                connection, core=core, variables=variables,
            )
            relation = Table(
                table_name, MetaData(), schema=dataset.get("physical_table_schema"),
                autoload_with=connection,
            )
            _failure_boundary("schema")
        elif isinstance(operation, ExecuteOperation):
            continue
        else:  # pragma: no cover
            raise TransformationError(
                "operation_not_supported", "Unsupported in-place plan operation."
            )

    after_identity = _target_identity_state(connection, dataset_id)
    if after_identity != before_identity:
        raise TransformationError(
            "dataset_identity_changed",
            "In-place apply changed the target dataset or its physical data-table identity.",
        )
    apply_id = str(uuid4())
    started = _now()
    if mutation_journal is not None:
        mutation_journal["apply_id"] = apply_id
    connection.execute(insert(audit).values(
        apply_id=apply_id,
        contract_id=APPLY_CONTRACT,
        dataset_id=dataset_id,
        database_profile=database_profile,
        physical_table_schema=dataset.get("physical_table_schema"),
        physical_table_name=table_name,
        source_kind=submission.source_kind,
        source_hash=submission.source_hash,
        frontend_contract=submission.frontend_contract,
        plan_hash=plan.sha256(),
        canonical_plan_json=plan.canonical_json(),
        actor=actor,
        status="succeeded",
        dolt_branch=dolt_branch,
        dolt_head_before=dolt_head,
        dolt_head_after=dolt_head,
        operation_count=len(plan.operations),
        started_at=started,
        completed_at=_now(),
    ))
    _failure_boundary("audit")
    forbidden = {
        name for name in inspect(connection).get_table_names()
        if name.startswith("derived_plan_")
        or name.startswith("__openstatspec_plan_staging_")
        or name.startswith("openstatspec_rollback_")
        or name.startswith("openstatspec_snapshot_")
    }
    if forbidden:
        raise TransformationError(
            "forbidden_copy_artifact",
            "In-place apply created a forbidden copy/history artifact.",
        )
    return {
        "apply_id": apply_id,
        "status": "succeeded",
        "dataset_id": dataset_id,
        "database_profile": database_profile,

        "physical_table_schema": dataset.get("physical_table_schema"),
        "physical_table_name": table_name,
        "source_kind": submission.source_kind,
        "source_hash": submission.source_hash,
        "frontend_contract": submission.frontend_contract,
        "plan_hash": plan.sha256(),
        "dolt_branch": dolt_branch,
        "dolt_head_before": dolt_head,
        "dolt_head_after": dolt_head,
        "dolt_commit_performed": False,
    }


def _dolt_state(connection: Any) -> tuple[str, str, int]:
    branch = str(connection.execute(text("SELECT active_branch()" )).scalar_one())
    head = str(connection.execute(text("SELECT DOLT_HASHOF('HEAD')")).scalar_one())
    dirty = int(connection.execute(text(
        "SELECT COUNT(*) FROM dolt_status"
    )).scalar_one())
    return branch, head, dirty


def install_in_place_transformation_schema(
    *,
    database_url: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> None:
    """Install the compact operation audit separately from any data apply."""
    # Resolve the effective profile first so Dolt conformance and explicit
    # driver checks fail closed before an engine transaction can execute DDL.
    effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            require_verified_catalog(
                connection,
                allowed_migrations={
                    "transformation_apply": {
                        "source_kind", "frontend_contract",
                    },
                },
            )
            apply_audit_catalog(MetaData()).create(connection, checkfirst=True)
            columns = {
                str(column["name"])
                for column in inspect(connection).get_columns("transformation_apply")
            }
            additions = {
                "source_kind": "VARCHAR(64) NULL",
                "frontend_contract": "VARCHAR(128) NULL",
            }
            quote = connection.dialect.identifier_preparer.quote
            for name, sql_type in additions.items():
                if name not in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {quote('transformation_apply')} "
                        f"ADD COLUMN {quote(name)} {sql_type}"
                    )
            require_verified_catalog(connection)
    finally:
        engine.dispose()


def load_transformation_schema(connection: Any, dataset_id: str) -> VariableSchema:
    """Read the live canonical variable schema within the caller's transaction."""
    return _input_schema(connection, dataset_id)[2]


def _compensate_failed_apply(
    engine: Any,
    *,
    journal: Mapping[str, Any],
) -> None:
    columns = list(journal.get("added_columns") or ())
    if not columns:
        return
    with engine.begin() as connection:

        core = core_catalog(MetaData())
        target_rows = list(journal.get("target_rows") or ())
        variable_ids = [str(row["variable_id"]) for row in target_rows]
        if variable_ids:
            label_set_ids = list(connection.execute(
                select(core.variable_value_label_set.c.value_label_set_id).where(
                    core.variable_value_label_set.c.variable_id.in_(variable_ids)
                )
            ).scalars())
            if label_set_ids:
                connection.execute(delete(core.value_label).where(
                    core.value_label.c.value_label_set_id.in_(label_set_ids)
                ))
                connection.execute(delete(core.variable_value_label_set).where(
                    core.variable_value_label_set.c.variable_id.in_(variable_ids)
                ))
                connection.execute(delete(core.value_label_set).where(
                    core.value_label_set.c.value_label_set_id.in_(label_set_ids)
                ))
            connection.execute(delete(core.variable).where(
                core.variable.c.variable_id.in_(variable_ids)
            ))
        apply_id = journal.get("apply_id")
        if apply_id:
            audit = apply_audit_catalog(MetaData())
            connection.execute(delete(audit).where(
                audit.c.apply_id == apply_id
            ))
        existing = {
            str(item["name"]).casefold()
            for item in inspect(connection).get_columns(
                str(journal["table_name"]), schema=journal.get("table_schema")
            )
        }
        columns = [column for column in columns if str(column).casefold() in existing]
        if not columns:
            return
        relation = Table(
            str(journal["table_name"]), MetaData(),
            schema=journal.get("table_schema"), autoload_with=connection,
        )
        quote = connection.dialect.identifier_preparer.quote
        qualified = connection.dialect.identifier_preparer.format_table(relation)
        for column in reversed(columns):
            connection.exec_driver_sql(
                f"ALTER TABLE {qualified} DROP COLUMN {quote(str(column))}"
            )
        remaining = {
            str(item["name"]).casefold()
            for item in inspect(connection).get_columns(
                str(journal["table_name"]), schema=journal.get("table_schema")
            )
        }
        if any(str(column).casefold() in remaining for column in columns):
            raise TransformationError(
                "schema_compensation_incomplete",
                "New target columns remain after compensating cleanup.",
            )


def _run_in_place_submission(
    *,
    database_url: str,
    dataset_id: str,
    actor: str,
    prepare: Callable[[Any, str], InPlacePlanSubmission],
    expected_branch: str | None = None,
    expected_head: str | None = None,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    """Prepare and apply one canonical plan in one controlled operation."""
    if not actor:
        raise TransformationError(
            "actor_required", "A non-empty actor identity is mandatory.",
        )
    profile, active = effective_profile(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )
    allow_delete_variable = True
    if profile.name == "sqlite":
        sqlite_version_parts = tuple(
            int(part) for part in str(active["server_version"]).split(".")
        )
        sqlite_version = (*sqlite_version_parts, 0, 0)[:3]
        allow_delete_variable = sqlite_version >= (3, 35, 0)
    engine = create_engine(database_url)
    journal: dict[str, Any] = {}
    branch: str | None = None
    head: str | None = None
    try:
        try:
            with engine.begin() as connection:
                if profile.name == "sqlite":
                    # Python's sqlite3 legacy transaction mode does not begin a
                    # transaction for DDL. Start one explicitly so schema and
                    # catalog mutations roll back together before the write lock
                    # is released.
                    connection.exec_driver_sql("BEGIN")
                require_verified_catalog(connection)
                if profile.name == "dolt":
                    if not expected_branch or not expected_head:
                        raise TransformationError(
                            "dolt_context_required",
                            "Dolt apply requires expected_branch and expected_head.",
                        )
                    branch, head, dirty = _dolt_state(connection)
                    if branch != expected_branch:
                        raise TransformationError(
                            "dolt_branch_mismatch",
                            "The active Dolt branch differs from the caller's expectation.",
                        )
                    if head != expected_head:
                        raise TransformationError(
                            "dolt_head_mismatch",
                            "The active Dolt HEAD differs from the caller's expectation.",
                        )
                    if dirty != 0:
                        raise TransformationError(
                            "dolt_working_set_dirty",
                            "The Dolt working set must be clean before in-place apply.",
                        )
                submission = prepare(connection, dataset_id)
                if not isinstance(submission, InPlacePlanSubmission):
                    raise TypeError("prepare must return InPlacePlanSubmission")
                result = _apply_plan_on_connection(
                    connection,
                    dataset_id=dataset_id,
                    submission=submission,
                    actor=actor,
                    database_profile=profile.name,
                    target_profile=profile,
                    allow_schema_change=profile.name in {"sqlite", "postgresql"},
                    allow_delete_variable=allow_delete_variable,
                    dolt_branch=branch,
                    dolt_head=head,
                    mutation_journal=journal,
                )
                if profile.name == "dolt":
                    after_branch, after_head, dirty_after = _dolt_state(connection)
                    if after_branch != branch or after_head != head:
                        raise TransformationError(
                            "dolt_context_changed",
                            "Apply must not switch branches or create a Dolt commit.",
                        )
                    if dirty_after <= 0:
                        raise TransformationError(
                            "dolt_expected_working_set_diff_missing",
                            "A successful Dolt apply must leave an inspectable working-set diff.",
                        )
                return result
        except Exception:
            # Transactional-DDL profiles roll back schema and catalog writes
            # atomically. Once that rollback releases the dataset lock, a
            # separate compensation transaction could delete artifacts created
            # by a waiting apply.
            if (
                journal.get("added_columns")
                and profile.name not in {"sqlite", "postgresql"}
            ):
                try:
                    _compensate_failed_apply(
                        engine,
                        journal=journal,
                    )
                except Exception as cleanup_error:
                    raise TransformationError(
                        "in_place_compensation_failed",
                        "Apply failed and compensating cleanup did not complete.",
                    ) from cleanup_error
            raise
    finally:
        engine.dispose()


def apply_transformation_plan_in_place(
    *,
    database_url: str,
    dataset_id: str,
    plan: TransformationPlan | Mapping[str, Any],
    actor: str,
    expected_branch: str | None = None,
    expected_head: str | None = None,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    """Apply a canonical plan without knowing which frontend produced it."""
    normalized = (
        plan
        if isinstance(plan, TransformationPlan)
        else transformation_plan_from_dict(plan)
    )
    plan_hash = normalized.sha256()
    submission = InPlacePlanSubmission(
        plan=normalized,
        source_kind="canonical_plan",
        source_hash=plan_hash,
    )
    return _run_in_place_submission(
        database_url=database_url,
        dataset_id=dataset_id,
        actor=actor,
        prepare=lambda _connection, _dataset_id: submission,
        expected_branch=expected_branch,
        expected_head=expected_head,
        dolt_conformance_source=dolt_conformance_source,
    )
