"""Bind catalog-independent SPSS syntax against an explicit in-memory schema."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from ...transform.errors import SourceSpan, frontend_error
from ...transform.plan import (
    PlanOperation, RecodeMatch, RecodeOperation, RecodeResult, RecodeRule,
    ReplaceValueLabelsOperation, SetVariableLabelOperation, TransformationPlan,
    TypedValue, ValueLabel,
)
from ...transform.schema import (
    BoundTransformation, StorageKind, VariableDefinition, VariableSchema,
)
from ...transform.validation import bind_transformation_plan
from .syntax import (
    RecodeCommandSyntax, RecodeMatchSyntax, RecodeResultSyntax, SpssSyntaxProgram,
    SyntaxLiteral, ValueLabelsCommandSyntax, VariableLabelsCommandSyntax,
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
            if clause.match.kind == "else":
                else_result = result
                continue
            rules.append(RecodeRule(_match(clause.match, source), result))
        unmatched = else_result or RecodeResult(
            "system_missing" if target_mode == "create" else "copy"
        )
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
        if isinstance(command, RecodeCommandSyntax):
            recodes, recode_spans = _bind_recode(command, variables)
            operations.extend(recodes)
            spans.extend(recode_spans)
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
    plan = TransformationPlan(tuple(operations), input_alias=input_alias)
    return bind_transformation_plan(
        plan,
        schema,
    )
