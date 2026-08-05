"""Bind catalog-independent SPSS syntax against an explicit in-memory schema."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from ...transform.errors import SourceSpan, frontend_error
from ...transform.plan import (
    AssignOperation, BooleanExpression, ComparisonExpression,
    ConditionalAssignOperation, CreateVariableOperation, DeleteVariableOperation,
    ExecuteOperation, Operand, PlanOperation,
    PredicateExpression, RecodeMatch, RecodeOperation, RecodeResult, RecodeRule,
    ReplaceValueLabelsOperation, SetFormatOperation,
    SetMeasurementLevelOperation, SetVariableLabelOperation,
    TRANSFORMATION_PLAN_SCHEMA_CHANGE_CONTRACT,
    TRANSFORMATION_PLAN_V1_CONTRACT,
    TransformationPlan, TypedValue, ValueLabel,
)
from ...transform.schema import (
    BoundTransformation, StorageKind, VariableDefinition, VariableSchema,
)
from ...transform.validation import bind_transformation_plan
from .syntax import (
    BooleanSyntax, ComparisonSyntax, ComputeCommandSyntax, ExecuteCommandSyntax,
    DeleteVariablesCommandSyntax, FormatsCommandSyntax, IfCommandSyntax,
    OperandSyntax, PredicateSyntax,
    RecodeCommandSyntax, RecodeMatchSyntax, RecodeResultSyntax,
    SpssSyntaxProgram, SyntaxLiteral, ValueLabelsCommandSyntax,
    StringCommandSyntax, VariableLabelsCommandSyntax, VariableLevelCommandSyntax,
)


def _typed(literal: SyntaxLiteral) -> TypedValue:
    if literal.kind == "numeric":
        assert isinstance(literal.value, float)
        return TypedValue.binary64(literal.value)
    assert isinstance(literal.value, str)
    return TypedValue.string(literal.value)


def _expected_type(storage_kind: StorageKind) -> str:
    return "binary64" if storage_kind == "numeric" else "string"


def _resolve(
    variables: list[VariableDefinition], name: str, span: SourceSpan,
) -> tuple[int, VariableDefinition]:
    matches = [
        (index, variable) for index, variable in enumerate(variables)
        if variable.name.casefold() == name.casefold()
    ]
    if len(matches) != 1:
        raise frontend_error(
            "unknown_variable", f"Variable {name!r} is not present in the current schema.",
            span=span, variable=name,
        )
    return matches[0]


def _bind_operand(
    syntax: OperandSyntax, variables: list[VariableDefinition],
) -> tuple[Operand, Literal["binary64", "string"]]:
    if syntax.kind == "variable":
        assert syntax.variable is not None
        _, variable = _resolve(variables, syntax.variable.text, syntax.variable.span)
        return (
            Operand.variable_ref(variable.name),
            "binary64" if variable.storage_kind == "numeric" else "string",
        )
    assert syntax.literal is not None
    value = _typed(syntax.literal)
    return Operand.literal(value), value.type


def _bind_predicate(
    syntax: PredicateSyntax, variables: list[VariableDefinition],
) -> PredicateExpression:
    if isinstance(syntax, ComparisonSyntax):
        left, left_type = _bind_operand(syntax.left, variables)
        right, right_type = _bind_operand(syntax.right, variables)
        if left_type != right_type:
            raise frontend_error(
                "type_mismatch",
                "Comparison operands must have the same storage kind.",
                span=syntax.span, left_type=left_type, right_type=right_type,
            )
        if syntax.operator != "=" and left_type != "binary64":
            raise frontend_error(
                "type_mismatch",
                "Ordered comparisons require numeric operands.",
                span=syntax.span, operator=syntax.operator,
            )
        return ComparisonExpression(left, syntax.operator, right)
    assert isinstance(syntax, BooleanSyntax)
    operands: list[PredicateExpression] = []
    for operand in syntax.operands:
        bound = _bind_predicate(operand, variables)
        if (
            isinstance(bound, BooleanExpression)
            and bound.operator == syntax.operator
        ):
            operands.extend(bound.operands)
        else:
            operands.append(bound)
    return BooleanExpression(syntax.operator, tuple(operands))


def _assignment(
    target_name: str, target_span: SourceSpan, value_syntax: OperandSyntax,
    variables: list[VariableDefinition],
) -> AssignOperation:
    value, value_type = _bind_operand(value_syntax, variables)
    if value_type == "string":
        raise frontend_error(
            "expression_type_unsupported",
            "String assignment is not supported until explicit width and "
            "profile-independent semantics are available.",
            span=value_syntax.span, variable=target_name,
        )
    matches = [
        (index, variable) for index, variable in enumerate(variables)
        if variable.name.casefold() == target_name.casefold()
    ]
    if matches:
        _, target = matches[0]
        if target.storage_kind == "string":
            raise frontend_error(
                "expression_type_unsupported",
                "String assignment targets are outside the bounded frontend.",
                span=target_span,
                variable=target.name,
            )
        if value_type != _expected_type(target.storage_kind):
            raise frontend_error(
                "type_mismatch",
                "COMPUTE cannot change an existing variable's storage kind.",
                span=target_span, variable=target.name,
                expected_type=_expected_type(target.storage_kind),
            )
        return AssignOperation(target.name, "replace", value)
    if target_name.startswith("__"):
        raise frontend_error(
            "reserved_target_name", f"Target name {target_name!r} is reserved.",
            span=target_span, target=target_name,
        )
    variables.append(VariableDefinition(
        target_name, "numeric" if value_type == "binary64" else "string",
    ))
    return AssignOperation(target_name, "create", value)


def _match(
    syntax: RecodeMatchSyntax, source: VariableDefinition,
) -> RecodeMatch:
    expected = _expected_type(source.storage_kind)
    if syntax.kind == "system_missing":
        if source.storage_kind == "string":
            raise frontend_error(
                "system_missing_for_string",
                "SYSMIS cannot match a string variable.", span=syntax.span,
                variable=source.name,
            )
        return RecodeMatch("system_missing")
    if syntax.kind == "range":
        assert syntax.lower is not None and syntax.upper is not None
        lower, upper = _typed(syntax.lower), _typed(syntax.upper)
        if lower.type != "binary64" or upper.type != "binary64" or expected != "binary64":
            raise frontend_error(
                "type_mismatch", "THRU ranges require a numeric source and endpoints.",
                span=syntax.span, variable=source.name,
            )
        if lower.number() > upper.number():
            raise frontend_error(
                "invalid_numeric_range",
                "THRU lower endpoint exceeds its upper endpoint.",
                span=syntax.span, variable=source.name,
            )
        return RecodeMatch("range", lower=lower, upper=upper)
    if syntax.kind != "values":
        raise AssertionError("ELSE is lowered separately")
    values = tuple(_typed(value) for value in syntax.values)
    if any(value.type != expected for value in values):
        raise frontend_error(
            "type_mismatch", "RECODE match values must match the source storage kind.",
            span=syntax.span, variable=source.name, expected_type=expected,
        )
    return RecodeMatch("values", values)


def _result(
    syntax: RecodeResultSyntax, source: VariableDefinition,
) -> RecodeResult:
    if syntax.kind == "copy":
        return RecodeResult("copy")
    if syntax.kind == "system_missing":
        if source.storage_kind == "string":
            raise frontend_error(
                "system_missing_for_string",
                "SYSMIS cannot be produced for a string variable.", span=syntax.span,
                variable=source.name,
            )
        return RecodeResult("system_missing")
    assert syntax.value is not None
    return RecodeResult("literal", _typed(syntax.value))


def _result_type(
    result: RecodeResult, source: VariableDefinition,
) -> Literal["binary64", "string"]:
    if result.kind == "copy":
        return _expected_type(source.storage_kind)  # type: ignore[return-value]
    if result.kind == "system_missing":
        return "binary64"
    assert result.value is not None
    return result.value.type


def _validate_recode_string_width(
    result: RecodeResult,
    source: VariableDefinition,
    target: VariableDefinition,
    span: SourceSpan,
) -> None:
    if target.storage_kind != "string" or target.declared_string_width is None:
        return
    if result.kind == "literal":
        assert result.value is not None
        if result.value.type != "string":
            return
        assert isinstance(result.value.value, str)
        if len(result.value.value.encode("utf-8")) > target.declared_string_width:
            raise frontend_error(
                "string_width_exceeded",
                "A RECODE string literal exceeds the target's declared string width.",
                span=span,
                variable=target.name,
                declared_string_width=target.declared_string_width,
            )
        return
    if result.kind == "copy" and source.storage_kind == "string":
        source_width = source.declared_string_width
        if source_width is None or source_width > target.declared_string_width:
            raise frontend_error(
                "string_width_exceeded",
                "A RECODE COPY result can exceed the target's declared string width.",
                span=span,
                source=source.name,
                variable=target.name,
                declared_string_width=target.declared_string_width,
            )


def _bind_recode(
    command: RecodeCommandSyntax, variables: list[VariableDefinition],
) -> tuple[list[RecodeOperation], list[SourceSpan]]:
    sources = [_resolve(variables, token.text, token.span)[1] for token in command.sources]
    targets = command.targets
    target_mode: Literal["create", "replace"] = "create" if targets is not None else "replace"
    target_names = (
        [token.text for token in targets] if targets is not None
        else [source.name for source in sources]
    )
    operations: list[RecodeOperation] = []
    spans: list[SourceSpan] = []
    for ordinal, (source, target_name) in enumerate(zip(sources, target_names)):
        target_span = (
            targets[ordinal].span if targets is not None else command.sources[ordinal].span
        )
        if target_mode == "create":
            if target_name.startswith("__"):
                raise frontend_error(
                    "reserved_target_name",
                    f"Target name {target_name!r} is reserved.",
                    span=target_span, target=target_name,
                )
            if any(
                variable.name.casefold() == target_name.casefold()
                for variable in variables
            ):
                raise frontend_error(
                    "target_already_exists",
                    f"Target name {target_name!r} already exists.",
                    span=target_span, target=target_name,
                )
        rules: list[RecodeRule] = []
        else_result: RecodeResult | None = None
        for clause in command.clauses:
            result = _result(clause.result, source)
            if target_mode == "replace":
                _validate_recode_string_width(result, source, source, clause.result.span)
            if clause.match.kind == "else":
                else_result = result
                continue
            rules.append(RecodeRule(_match(clause.match, source), result))
        unmatched = else_result or RecodeResult(
            "system_missing" if target_mode == "create" else "copy"
        )
        if target_mode == "replace" and else_result is None:
            _validate_recode_string_width(unmatched, source, source, command.span)
        result_types = {
            _result_type(result, source)
            for result in [*(rule.result for rule in rules), unmatched]
        }
        if target_mode == "create" and "string" in result_types:
            raise frontend_error(
                "string_target_requires_declaration",
                "New string targets require a STRING declaration, which is outside the MVP.",
                span=target_span, target=target_name,
            )
        if len(result_types) != 1:
            raise frontend_error(
                "mixed_result_types",
                "All RECODE results, including unmatched behavior, must have one type.",
                span=command.span, source=source.name, result_types=sorted(result_types),
            )
        output_type = next(iter(result_types))
        if target_mode == "replace" and output_type != _expected_type(source.storage_kind):
            raise frontend_error(
                "type_mismatch", "In-place RECODE cannot change the variable storage kind.",
                span=command.span, variable=source.name,
            )
        operation = RecodeOperation(
            source=source.name,
            target=source.name if target_mode == "replace" else target_name,
            target_mode=target_mode, rules=tuple(rules), unmatched=unmatched,
        )
        operations.append(operation)
        spans.append(command.span)
        if target_mode == "create":
            variables.append(VariableDefinition(target_name, "numeric"))
        # replace intentionally preserves the existing variable metadata. A later
        # VALUE LABELS command replaces value labels explicitly.
    return operations, spans
def bind_spss_syntax(
    program: SpssSyntaxProgram, schema: VariableSchema, *, input_alias: str = "parent",
) -> BoundTransformation:
    """Resolve names and sequential semantics into a canonical generic plan."""
    if not program.commands:
        raise frontend_error(
            "spss_syntax_error", "At least one supported SPSS command is required.",
            span=program.span,
        )
    variables = list(schema.variables)
    operations: list[PlanOperation] = []
    spans: list[SourceSpan] = []
    for command in program.commands:
        if isinstance(command, StringCommandSyntax):
            for variable_token in command.variables:
                if variable_token.text.startswith("__"):
                    raise frontend_error(
                        "reserved_target_name",
                        f"Target name {variable_token.text!r} is reserved.",
                        span=variable_token.span,
                        target=variable_token.text,
                    )
                if any(
                    variable.name.casefold() == variable_token.text.casefold()
                    for variable in variables
                ):
                    raise frontend_error(
                        "target_already_exists",
                        f"Target name {variable_token.text!r} already exists.",
                        span=variable_token.span,
                        target=variable_token.text,
                    )
                operations.append(CreateVariableOperation(
                    variable_token.text, "string", command.width,
                ))
                spans.append(command.span)
                variables.append(VariableDefinition(
                    variable_token.text, "string",
                    declared_string_width=command.width,
                ))
            continue
        if isinstance(command, DeleteVariablesCommandSyntax):
            for variable_token in command.variables:
                index, variable = _resolve(
                    variables, variable_token.text, variable_token.span,
                )
                operations.append(DeleteVariableOperation(variable.name))
                spans.append(command.span)
                del variables[index]
            continue
        if isinstance(command, RecodeCommandSyntax):
            recodes, recode_spans = _bind_recode(command, variables)
            operations.extend(recodes)
            spans.extend(recode_spans)
            continue
        if isinstance(command, ComputeCommandSyntax):
            operations.append(_assignment(
                command.target.text, command.target.span, command.value, variables,
            ))
            spans.append(command.span)
            continue
        if isinstance(command, IfCommandSyntax):
            condition = _bind_predicate(command.condition, variables)
            target_matches = [
                (index, variable) for index, variable in enumerate(variables)
                if variable.name.casefold() == command.target.text.casefold()
            ]
            if len(target_matches) != 1:
                raise frontend_error(
                    "conditional_target_missing",
                    "IF assignment target must already exist.",
                    span=command.target.span, variable=command.target.text,
                )
            _, target = target_matches[0]
            if target.storage_kind == "string":
                raise frontend_error(
                    "expression_type_unsupported",
                    "String assignment targets are outside the bounded frontend.",
                    span=command.target.span,
                    variable=target.name,
                )
            value, value_type = _bind_operand(command.value, variables)
            expected = _expected_type(target.storage_kind)
            if value_type == "string":
                raise frontend_error(
                    "expression_type_unsupported",
                    "String assignment is not supported until explicit width "
                    "and profile-independent semantics are available.",
                    span=command.value.span, variable=target.name,
                )
            if value_type != expected:
                raise frontend_error(
                    "type_mismatch",
                    "IF assignment value must match the target storage kind.",
                    span=command.value.span, variable=target.name,
                    expected_type=expected,
                )
            operations.append(ConditionalAssignOperation(
                condition, target.name, value,
            ))
            spans.append(command.span)
            continue
        if isinstance(command, FormatsCommandSyntax):
            for assignment in command.assignments:
                index, variable = _resolve(
                    variables, assignment.variable.text, assignment.variable.span,
                )
                if variable.storage_kind != "numeric":
                    raise frontend_error(
                        "expression_type_unsupported",
                        "Numeric F formats cannot target string variables.",
                        span=assignment.span,
                        variable=variable.name,
                    )
                operations.append(SetFormatOperation(
                    variable.name, assignment.family,
                    assignment.width, assignment.decimals,
                ))
                spans.append(assignment.span)
                variables[index] = replace(
                    variable, format_family=assignment.family,
                    format_width=assignment.width,
                    format_decimals=assignment.decimals,
                )
            continue
        if isinstance(command, VariableLevelCommandSyntax):
            for assignment in command.assignments:
                index, variable = _resolve(
                    variables, assignment.variable.text, assignment.variable.span,
                )
                operations.append(SetMeasurementLevelOperation(
                    variable.name, assignment.level,
                ))
                spans.append(assignment.span)
                variables[index] = replace(
                    variable, measurement_level=assignment.level,
                )
            continue
        if isinstance(command, ExecuteCommandSyntax):
            operations.append(ExecuteOperation())
            spans.append(command.span)
            continue
        if isinstance(command, VariableLabelsCommandSyntax):
            for assignment in command.assignments:
                index, variable = _resolve(
                    variables, assignment.variable.text, assignment.variable.span,
                )
                assert isinstance(assignment.label.value, str)
                operations.append(SetVariableLabelOperation(
                    variable.name, assignment.label.value,
                ))
                spans.append(assignment.span)
                variables[index] = replace(
                    variable, variable_label=assignment.label.value,
                )
            continue
        if isinstance(command, ValueLabelsCommandSyntax):
            for group in command.groups:
                for variable_token in group.variables:
                    index, variable = _resolve(
                        variables, variable_token.text, variable_token.span,
                    )
                    expected = _expected_type(variable.storage_kind)
                    labels = tuple(ValueLabel(
                        _typed(label.value), str(label.label.value),
                    ) for label in group.labels)
                    mismatched = next(
                        (label for label in labels if label.value.type != expected), None
                    )
                    if mismatched is not None:
                        raise frontend_error(
                            "type_mismatch",
                            "VALUE LABELS codes must match the variable storage kind.",
                            span=group.span, variable=variable.name,
                            expected_type=expected,
                        )
                    keys = [label.value.canonical_key() for label in labels]
                    if len(keys) != len(set(keys)):
                        raise frontend_error(
                            "duplicate_value_label",
                            "VALUE LABELS contains duplicate canonical codes.",
                            span=group.span, variable=variable.name,
                        )
                    operation = ReplaceValueLabelsOperation(variable.name, labels)
                    operations.append(operation)
                    spans.append(group.span)
                    variables[index] = replace(variable, value_labels=labels)
            continue
        raise AssertionError(f"Unknown syntax command: {type(command)!r}")
    v01_types = (
        RecodeOperation, SetVariableLabelOperation, ReplaceValueLabelsOperation,
    )
    schema_change_types = (CreateVariableOperation, DeleteVariableOperation)
    contract = (
        TRANSFORMATION_PLAN_SCHEMA_CHANGE_CONTRACT
        if any(isinstance(operation, schema_change_types) for operation in operations)
        else (
            TRANSFORMATION_PLAN_V1_CONTRACT
            if all(isinstance(operation, v01_types) for operation in operations)
            else "openstatspec-transformation-plan-v0.2"
        )
    )
    plan = TransformationPlan(
        tuple(operations), contract=contract, input_alias=input_alias,
    )
    return bind_transformation_plan(
        plan,
        schema,
    )
