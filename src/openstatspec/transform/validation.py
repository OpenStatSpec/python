"""Validate canonical transformation plans against an explicit live schema."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from .errors import frontend_error
from .plan import (
    AssignOperation,
    BooleanExpression,
    ComparisonExpression,
    CreateVariableOperation,
    ConditionalAssignOperation,
    DeleteVariableOperation,
    ExecuteOperation,
    Operand,
    RecodeMatch,
    RecodeOperation,
    RecodeResult,
    ReplaceValueLabelsOperation,
    SetFormatOperation,
    SetMeasurementLevelOperation,
    SetVariableLabelOperation,
    TransformationPlan,
)
from .schema import (
    BoundTransformation,
    StorageKind,
    VariableDefinition,
    VariableSchema,
)


ValueType = Literal["binary64", "string"]


def _expected_type(storage_kind: StorageKind) -> ValueType:
    return "binary64" if storage_kind == "numeric" else "string"


def _resolve(
    variables: list[VariableDefinition], name: str
) -> tuple[int, VariableDefinition]:
    matches = [
        (index, variable)
        for index, variable in enumerate(variables)
        if variable.name.casefold() == name.casefold()
    ]
    if len(matches) != 1:
        raise frontend_error(
            "unknown_variable",
            f"Variable {name!r} is not present in the current schema.",
            variable=name,
        )
    return matches[0]


def _validate_match(match: RecodeMatch, source: VariableDefinition) -> None:
    expected = _expected_type(source.storage_kind)
    if match.kind == "system_missing":
        if source.storage_kind == "string":
            raise frontend_error(
                "system_missing_for_string",
                "SYSMIS cannot match a string variable.",
                variable=source.name,
            )
        return
    if match.kind == "range":
        if expected != "binary64":
            raise frontend_error(
                "type_mismatch",
                "Ranges require a numeric source.",
                variable=source.name,
            )
        return
    if any(value.type != expected for value in match.values):
        raise frontend_error(
            "type_mismatch",
            "RECODE match values must match the source storage kind.",
            variable=source.name,
            expected_type=expected,
        )


def _result_type(
    result: RecodeResult, source: VariableDefinition
) -> ValueType:
    if result.kind == "copy":
        return _expected_type(source.storage_kind)
    if result.kind == "system_missing":
        if source.storage_kind == "string":
            raise frontend_error(
                "system_missing_for_string",
                "SYSMIS cannot be produced for a string variable.",
                variable=source.name,
            )
        return "binary64"
    assert result.value is not None
    return result.value.type


def _bind_recode(
    operation: RecodeOperation, variables: list[VariableDefinition]
) -> None:
    _, source = _resolve(variables, operation.source)
    if operation.target_mode == "create":
        if operation.target.startswith("__"):
            raise frontend_error(
                "reserved_target_name",
                f"Target name {operation.target!r} is reserved.",
                target=operation.target,
            )
        if any(
            variable.name.casefold() == operation.target.casefold()
            for variable in variables
        ):
            raise frontend_error(
                "target_already_exists",
                f"Target name {operation.target!r} already exists.",
                target=operation.target,
            )
    for rule in operation.rules:
        _validate_match(rule.match, source)
    result_types = {
        _result_type(result, source)
        for result in [
            *(rule.result for rule in operation.rules),
            operation.unmatched,
        ]
    }
    if len(result_types) != 1:
        raise frontend_error(
            "mixed_result_types",
            "All RECODE results, including unmatched behavior, must have one type.",
            source=source.name,
            result_types=sorted(result_types),
        )
    output_type = next(iter(result_types))
    if (
        operation.target_mode == "replace"
        and output_type != _expected_type(source.storage_kind)
    ):
        raise frontend_error(
            "type_mismatch",
            "In-place RECODE cannot change the variable storage kind.",
            variable=source.name,
        )
    if operation.target_mode == "create":
        variables.append(
            VariableDefinition(
                operation.target,
                "numeric" if output_type == "binary64" else "string",
            )
        )


def _operand_type(
    operand: Operand, variables: list[VariableDefinition]
) -> ValueType:
    if operand.kind == "literal":
        assert operand.value is not None
        return operand.value.type
    assert operand.variable is not None
    _, variable = _resolve(variables, operand.variable)
    return _expected_type(variable.storage_kind)


def _validate_predicate(
    predicate: ComparisonExpression | BooleanExpression,
    variables: list[VariableDefinition],
) -> None:
    if isinstance(predicate, BooleanExpression):
        for item in predicate.operands:
            _validate_predicate(item, variables)
        return
    left_type = _operand_type(predicate.left, variables)
    right_type = _operand_type(predicate.right, variables)
    if left_type != right_type:
        raise frontend_error(
            "type_mismatch",
            "Comparison operands must have the same storage type.",
            operator=predicate.operator,
            left_type=left_type,
            right_type=right_type,
        )
    if left_type == "string":
        raise frontend_error(
            "expression_type_unsupported",
            "String comparisons are not supported until exact, "
            "profile-independent collation semantics are available.",
            operator=predicate.operator,
        )
    if predicate.operator != "=" and left_type != "binary64":
        raise frontend_error(
            "type_mismatch",
            "Ordered comparisons require numeric operands.",
            operator=predicate.operator,
        )


def _bind_assign(
    operation: AssignOperation, variables: list[VariableDefinition]
) -> None:
    output_type = _operand_type(operation.value, variables)
    if output_type == "string":
        raise frontend_error(
            "expression_type_unsupported",
            "String assignment is not supported until explicit width and "
            "profile-independent semantics are available.",
            variable=operation.target,
        )
    if operation.target_mode == "create":
        if operation.target.startswith("__"):
            raise frontend_error(
                "reserved_target_name",
                f"Target name {operation.target!r} is reserved.",
                target=operation.target,
            )
        if any(
            variable.name.casefold() == operation.target.casefold()
            for variable in variables
        ):
            raise frontend_error(
                "target_already_exists",
                f"Target name {operation.target!r} already exists.",
                target=operation.target,
            )
        variables.append(VariableDefinition(
            operation.target,
            "numeric" if output_type == "binary64" else "string",
        ))
        return
    _, target = _resolve(variables, operation.target)
    if target.storage_kind == "string":
        raise frontend_error(
            "expression_type_unsupported",
            "String assignment targets are outside the bounded plan.",
            variable=target.name,
        )
    if output_type != _expected_type(target.storage_kind):
        raise frontend_error(
            "type_mismatch",
            "Assignment cannot change the target storage kind.",
            variable=target.name,
        )


def _bind_conditional_assign(
    operation: ConditionalAssignOperation,
    variables: list[VariableDefinition],
) -> None:
    _validate_predicate(operation.condition, variables)
    _, target = _resolve(variables, operation.target)
    if target.storage_kind == "string":
        raise frontend_error(
            "expression_type_unsupported",
            "String assignment targets are outside the bounded plan.",
            variable=target.name,
        )
    output_type = _operand_type(operation.value, variables)
    if output_type == "string":
        raise frontend_error(
            "expression_type_unsupported",
            "String assignment is not supported until explicit width and "
            "profile-independent semantics are available.",
            variable=operation.target,
        )
    if output_type != _expected_type(target.storage_kind):
        raise frontend_error(
            "type_mismatch",
            "Conditional assignment value must match the target storage kind.",
            variable=target.name,
        )

def bind_transformation_plan(
    plan: TransformationPlan, schema: VariableSchema
) -> BoundTransformation:
    """Validate sequential plan semantics and return the resulting schema."""
    if not isinstance(plan, TransformationPlan):
        raise TypeError("plan must be a TransformationPlan.")
    if not isinstance(schema, VariableSchema):
        raise TypeError("schema must be a VariableSchema.")
    variables = list(schema.variables)
    for operation in plan.operations:
        if isinstance(operation, CreateVariableOperation):
            _bind_create(operation, variables)
            continue
        if isinstance(operation, DeleteVariableOperation):
            _bind_delete(operation, variables)
            continue
        if isinstance(operation, RecodeOperation):
            _bind_recode(operation, variables)
            continue
        if isinstance(operation, AssignOperation):
            _bind_assign(operation, variables)
            continue
        if isinstance(operation, ConditionalAssignOperation):
            _bind_conditional_assign(operation, variables)
            continue
        if isinstance(operation, SetVariableLabelOperation):
            index, variable = _resolve(variables, operation.variable)
            variables[index] = replace(variable, variable_label=operation.label)
            continue
        if isinstance(operation, ReplaceValueLabelsOperation):
            index, variable = _resolve(variables, operation.variable)
            expected = _expected_type(variable.storage_kind)
            if any(label.value.type != expected for label in operation.labels):
                raise frontend_error(
                    "type_mismatch",
                    "Value-label codes must match the variable storage kind.",
                    variable=variable.name,
                    expected_type=expected,
                )
            if variable.storage_kind == "string" and variable.declared_string_width is not None:
                for label in operation.labels:
                    assert label.value.value is not None
                    if len(label.value.value.encode("utf-8")) > variable.declared_string_width:
                        raise frontend_error(
                            "string_width_exceeded",
                            "A value-label code exceeds the variable's declared string width.",
                            variable=variable.name,
                            declared_string_width=variable.declared_string_width,
                        )
            variables[index] = replace(variable, value_labels=operation.labels)
            continue
        if isinstance(operation, SetFormatOperation):
            index, variable = _resolve(variables, operation.variable)
            if variable.storage_kind != "numeric":
                raise frontend_error(
                    "expression_type_unsupported",
                    "Numeric F formats cannot target string variables.",
                    variable=variable.name,
                )
            variables[index] = replace(
                variable,
                format_family=operation.family,
                format_width=operation.width,
                format_decimals=operation.decimals,
            )
            continue
        if isinstance(operation, SetMeasurementLevelOperation):
            index, variable = _resolve(variables, operation.variable)
            variables[index] = replace(variable, measurement_level=operation.level)
            continue
        if isinstance(operation, ExecuteOperation):
            continue
        raise AssertionError(f"Unknown plan operation: {type(operation)!r}")
    return BoundTransformation(plan, VariableSchema(tuple(variables)))
def _bind_create(
    operation: CreateVariableOperation,
    variables: list[VariableDefinition],
) -> None:
    if any(variable.name.casefold() == operation.variable.casefold() for variable in variables):
        raise frontend_error(
            "target_already_exists",
            f"Target name {operation.variable!r} already exists.",
            target=operation.variable,
        )
    variables.append(VariableDefinition(
        operation.variable,
        operation.storage_kind,
        declared_string_width=operation.declared_string_width,
    ))


def _bind_delete(
    operation: DeleteVariableOperation,
    variables: list[VariableDefinition],
) -> None:
    index, variable = _resolve(variables, operation.variable)
    if len(variables) == 1:
        raise frontend_error(
            "cannot_delete_last_variable",
            "A dataset must retain at least one variable.",
            variable=variable.name,
        )
    del variables[index]
