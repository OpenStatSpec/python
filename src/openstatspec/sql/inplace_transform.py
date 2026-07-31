"""In-place application without an OpenStatSpec undo or copy layer."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Column, DateTime, Float, Integer, MetaData, String, Table, Text, case,
    create_engine, delete, inspect, insert, literal, null, select, text, update,
)

from ..transform import (
    RecodeOperation, RecodeResult, ReplaceValueLabelsOperation,
    SetVariableLabelOperation, TypedValue, ValueLabel, VariableDefinition,
    VariableSchema, compile_spss_syntax,
)
from .capabilities import effective_profile
from .normative import catalog as core_catalog
from .wide import catalog as legacy_catalog
from .wide import normalized_metadata_tables, physical_name
from .workflow import TransformationError


APPLY_CONTRACT = "openstatspec-in-place-transformation-v0.1"


def in_place_transformation_capabilities() -> dict[str, Any]:
    return {
        "contract": APPLY_CONTRACT,
        "status": "experimental",
        "database_products": [
            "sqlite", "postgresql", "mysql", "mariadb", "dolt",
        ],
        "parent_kinds": ["core"],
        "mutation": "same_dataset_same_physical_wide_table",
        "commands": ["RECODE", "VARIABLE LABELS", "VALUE LABELS"],
        "new_target_column": {
            "sqlite": True,
            "postgresql": True,
            "mysql": False,
            "mariadb": False,
            "dolt": False,
            "reason": "non-transactional DDL must not make apply partially durable",
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
        Column("source_hash", String(64), nullable=False),
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
) -> tuple[dict[str, Any], list[dict[str, Any]], VariableSchema]:
    core = core_catalog(MetaData())
    dataset = connection.execute(
        select(core.dataset).where(core.dataset.c.dataset_id == dataset_id)
    ).mappings().one_or_none()
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
        )
        for row in variables
    ))
    return dict(dataset), variables, schema


def _catalog_identity_counts(
    connection: Any,
) -> tuple[int, tuple[tuple[str | None, str], ...]]:
    core = core_catalog(MetaData())
    dataset_rows = connection.execute(
        select(
            core.dataset.c.physical_table_schema,
            core.dataset.c.physical_table_name,
        )
    ).all()
    identities = tuple(sorted(
        (
            (
                str(schema) if schema is not None else None,
                str(name),
            )
            for schema, name in dataset_rows
        ),
        key=lambda item: (item[0] or "", item[1]),
    ))
    return len(dataset_rows), identities


def _legacy_identifiers(dataset: dict[str, Any]) -> tuple[str, str]:
    dataset_name = dataset.get("dataset_name")
    table_name = dataset.get("physical_table_name")
    if not isinstance(dataset_name, str) or not dataset_name:
        raise TransformationError(
            "dataset_invalid", "The target lacks its legacy catalog identity."
        )
    if not isinstance(table_name, str) or not table_name:
        raise TransformationError(
            "dataset_invalid", "The target lacks its physical wide-table name."
        )
    return dataset_name, table_name


def _replace_value_labels(
    connection: Any,
    *,
    core: Any,
    legacy_variable: Table,
    legacy_labels: Table,
    legacy_dataset_id: str,
    variable: dict[str, Any],
    labels: tuple[ValueLabel, ...],
) -> None:
    variable_id = str(variable["variable_id"])
    ordinal = int(variable["source_ordinal"])
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
    connection.execute(delete(legacy_labels).where(
        legacy_labels.c.dataset_id == legacy_dataset_id,
        legacy_labels.c.variable_ordinal == ordinal,
    ))
    for label_ordinal, item in enumerate(labels, start=1):
        connection.execute(insert(legacy_labels).values(
            dataset_id=legacy_dataset_id,
            variable_ordinal=ordinal,
            ordinal=label_ordinal,
            value_type="numeric" if item.value.type == "binary64" else "text",
            numeric_value=(item.value.number() if item.value.type == "binary64" else None),
            text_value=(str(item.value.value) if item.value.type == "string" else None),
            label=item.label,
        ))
    legacy_json = {
        str(_typed_value(item.value)): item.label for item in labels
    }
    connection.execute(update(legacy_variable).where(
        legacy_variable.c.dataset_id == legacy_dataset_id,
        legacy_variable.c.ordinal == ordinal,
    ).values(value_labels=json.dumps(legacy_json, ensure_ascii=False)))


def _apply_on_connection(
    connection: Any,
    *,
    dataset_id: str,
    source_text: str,
    actor: str,
    database_profile: str,
    allow_schema_change: bool,
    dolt_branch: str | None,
    dolt_head: str | None,
) -> dict[str, Any]:
    before_count, before_tables = _catalog_identity_counts(connection)
    dataset, variables, schema = _input_schema(connection, dataset_id)
    legacy_dataset_id, table_name = _legacy_identifiers(dataset)
    compilation = compile_spss_syntax(source_text, schema, input_alias="parent")
    audit = apply_audit_catalog(MetaData())
    if not inspect(connection).has_table("transformation_apply"):
        raise TransformationError(
            "in_place_audit_schema_missing",
            "The compact transformation_apply audit schema must be installed "
            "before apply.",
        )
    if not allow_schema_change and any(
        isinstance(operation, RecodeOperation)
        and operation.target_mode == "create"
        for operation in compilation.plan.operations
    ):
        raise TransformationError(
            "schema_change_not_atomic",
            "This database profile requires RECODE targets to exist before "
            "apply because its DDL is not transaction-atomic.",
        )
    core = core_catalog(MetaData())
    legacy_metadata = MetaData()
    _, legacy_variable, _, _ = legacy_catalog(legacy_metadata)
    _, legacy_labels, _, _ = normalized_metadata_tables(legacy_metadata)
    relation = Table(
        table_name,
        MetaData(),
        schema=dataset.get("physical_table_schema"),
        autoload_with=connection,
    )
    by_name = {str(row["source_name"]).casefold(): row for row in variables}
    used_physical = {str(row["physical_name"]).casefold() for row in variables}

    for operation in compilation.plan.operations:
        if isinstance(operation, RecodeOperation):
            source_variable = by_name[operation.source.casefold()]
            if operation.target_mode == "create":
                if str(source_variable["storage_kind"]) != "numeric":
                    raise TransformationError(
                        "in_place_target_type_unsupported",
                        "This profile creates only numeric RECODE targets.",
                    )
                target_physical = physical_name(operation.target, used_physical)
                quote = connection.dialect.identifier_preparer.quote
                numeric_type = (
                    "DOUBLE PRECISION"
                    if connection.dialect.name == "postgresql"
                    else "DOUBLE"
                )
                qualified_table = connection.dialect.identifier_preparer.format_table(
                    relation
                )
                connection.exec_driver_sql(
                    f"ALTER TABLE {qualified_table} ADD COLUMN "
                    f"{quote(target_physical)} {numeric_type} NULL"
                )
                new_ordinal = max(int(row["source_ordinal"]) for row in variables) + 1
                target_variable = {
                    "variable_id": str(uuid4()),
                    "dataset_id": dataset_id,
                    "source_ordinal": new_ordinal,
                    "source_name": operation.target,
                    "physical_name": target_physical,
                    "storage_kind": "numeric",
                    "variable_label": None,
                }
                connection.execute(insert(core.variable).values(**target_variable))
                connection.execute(insert(legacy_variable).values(
                    dataset_id=legacy_dataset_id,
                    ordinal=new_ordinal,
                    source_name=operation.target,
                    physical_name=target_physical,
                    storage_kind="numeric",
                    string_width=None,
                    label="",
                    attributes="{}",
                    value_labels="{}",
                    missing_ranges="[]",
                ))
                variables.append(target_variable)
                by_name[operation.target.casefold()] = target_variable
                relation = Table(
                    table_name,
                    MetaData(),
                    schema=dataset.get("physical_table_schema"),
                    autoload_with=connection,
                )
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
        elif isinstance(operation, SetVariableLabelOperation):
            variable = by_name[operation.variable.casefold()]
            connection.execute(update(core.variable).where(
                core.variable.c.variable_id == variable["variable_id"]
            ).values(variable_label=operation.label))
            connection.execute(update(legacy_variable).where(
                legacy_variable.c.dataset_id == legacy_dataset_id,
                legacy_variable.c.ordinal == variable["source_ordinal"],
            ).values(label=operation.label))
        elif isinstance(operation, ReplaceValueLabelsOperation):
            _replace_value_labels(
                connection,
                core=core,
                legacy_variable=legacy_variable,
                legacy_labels=legacy_labels,
                legacy_dataset_id=legacy_dataset_id,
                variable=by_name[operation.variable.casefold()],
                labels=operation.labels,
            )
        else:  # pragma: no cover - canonical plan type is closed
            raise TransformationError(
                "operation_not_supported", "Unsupported in-place plan operation."
            )

    after_count, after_tables = _catalog_identity_counts(connection)
    if (after_count, after_tables) != (before_count, before_tables):
        raise TransformationError(
            "dataset_identity_changed",
            "In-place apply changed dataset or physical data-table identity.",
        )
    apply_id = str(uuid4())
    started = _now()
    connection.execute(insert(audit).values(
        apply_id=apply_id,
        contract_id=APPLY_CONTRACT,
        dataset_id=dataset_id,
        database_profile=database_profile,
        physical_table_schema=dataset.get("physical_table_schema"),
        physical_table_name=table_name,
        source_hash=compilation.source_hash,
        plan_hash=compilation.plan_hash,
        canonical_plan_json=compilation.plan.canonical_json(),
        actor=actor,
        status="succeeded",
        dolt_branch=dolt_branch,
        dolt_head_before=dolt_head,
        dolt_head_after=dolt_head,
        operation_count=len(compilation.plan.operations),
        started_at=started,
        completed_at=_now(),
    ))
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
        "source_hash": compilation.source_hash,
        "plan_hash": compilation.plan_hash,
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


def install_in_place_transformation_schema(*, database_url: str) -> None:
    """Install the compact operation audit separately from any data apply."""
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            apply_audit_catalog(MetaData()).create(connection, checkfirst=True)
    finally:
        engine.dispose()


def apply_spss_in_place(
    *,
    database_url: str,
    dataset_id: str,
    source_text: str,
    actor: str,
    expected_branch: str | None = None,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Apply one plan to the same dataset/table; never create undo or copies."""
    if not actor:
        raise TransformationError(
            "actor_required", "A non-empty actor identity is mandatory.",
        )
    profile, _active = effective_profile(database_url)
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            branch: str | None = None
            head: str | None = None
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
            result = _apply_on_connection(
                connection,
                dataset_id=dataset_id,
                source_text=source_text,
                actor=actor,
                database_profile=profile.name,
                allow_schema_change=profile.name in {"sqlite", "postgresql"},
                dolt_branch=branch,
                dolt_head=head,
            )
            if profile.name == "dolt":
                after_branch, after_head, _dirty_after = _dolt_state(connection)
                if after_branch != branch or after_head != head:
                    raise TransformationError(
                        "dolt_context_changed",
                        "Apply must not switch branches or create a Dolt commit.",
                    )
            return result
    finally:
        engine.dispose()
