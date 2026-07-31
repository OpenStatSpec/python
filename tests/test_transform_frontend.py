from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from openstatspec.frontends.spss import (
    bind_spss_syntax,
    compile_spss_syntax,
    normalize_spss_source,
    parse_spss_syntax,
    spss_source_hash,
)
from openstatspec.transform import (
    RecodeMatch,
    RecodeOperation,
    RecodeResult,
    RecodeRule,
    SetVariableLabelOperation,
    TransformationPlan,
    TransformationFrontendError,
    TypedValue,
    ValueLabel,
    VariableDefinition,
    VariableSchema,
    bind_transformation_plan,
    canonical_plan_hash,
    canonical_plan_json,
    transformation_plan_from_dict,
)


def _schema(*variables: VariableDefinition) -> VariableSchema:
    return VariableSchema(tuple(variables))


def _compile(source: str, schema: VariableSchema):
    return bind_spss_syntax(parse_spss_syntax(source), schema)


def _error(source: str, schema: VariableSchema) -> TransformationFrontendError:
    with pytest.raises(TransformationFrontendError) as caught:
        _compile(source, schema)
    return caught.value


def _frontend_conformance_manifest() -> Path:
    configured = os.environ.get("OPENSTATSPEC_SPECIFICATION_DIR")
    candidates = [
        (
            Path(configured) / "conformance/spss-syntax-frontend-0.1.json"
            if configured
            else None
        ),
        Path(__file__).resolve().parents[1]
        / "openstatspec-specification/conformance/spss-syntax-frontend-0.1.json",
        Path(__file__).resolve().parents[2]
        / "specification/conformance/spss-syntax-frontend-0.1.json",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise RuntimeError(
        "The SPSS frontend conformance fixture is required; "
        "set OPENSTATSPEC_SPECIFICATION_DIR."
    )


def test_official_spss_frontend_conformance_manifest() -> None:
    manifest_path = _frontend_conformance_manifest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan_manifest = json.loads(
        (manifest_path.parent / "transformation-plan-0.1.json").read_text(
            encoding="utf-8"
        )
    )
    plan_cases = {case["id"]: case for case in plan_manifest["cases"]}

    for case in manifest["cases"]:
        request = case["request"]
        schema = VariableSchema(tuple(
            VariableDefinition(
                variable["name"],
                variable["storage_kind"],
                variable_label=variable.get("variable_label"),
                value_labels=tuple(
                    ValueLabel(
                        TypedValue.from_dict(label["value"]),
                        label["label"],
                    )
                    for label in variable.get("value_labels", [])
                ),
            )
            for variable in request["input_schema"]["variables"]
        ))
        assert spss_source_hash(request["source_text"]) == case["expected_source_hash"]
        if case["expected_error"] is not None:
            with pytest.raises(TransformationFrontendError) as caught:
                compile_spss_syntax(
                    request["source_text"],
                    schema,
                    input_alias=request["input_alias"],
                )
            assert caught.value.code == case["expected_error"], case["id"]
            continue

        compilation = compile_spss_syntax(
            request["source_text"],
            schema,
            input_alias=request["input_alias"],
        )
        if "expected_plan_case" in case:
            expected_plan = plan_cases[case["expected_plan_case"]]["plan"]
            expected_hash = plan_cases[case["expected_plan_case"]][
                "expected_plan_hash"
            ]
        else:
            expected_plan = case["expected_plan"]
            expected_hash = case["expected_plan_hash"]
        assert compilation.plan.as_dict() == expected_plan, case["id"]
        assert compilation.plan_hash == expected_hash, case["id"]
        if "expected_output_metadata" in case:
            actual_metadata = {
                variable.name: {
                    "variable_label": variable.variable_label,
                    "value_labels": [
                        label.as_dict() for label in variable.value_labels
                    ],
                }
                for variable in compilation.bound.output_schema.variables
            }
            assert actual_metadata == case["expected_output_metadata"], case["id"]


def test_recode_and_labels_lower_to_exact_canonical_plan() -> None:
    source = (
        "RECODE q1 (1,2 = 0) (3 THRU 5 = 1) (ELSE = SYSMIS) "
        "INTO q1_binary.\n"
        "VARIABLE LABELS q1_binary 'Positive response'.\n"
        "VALUE LABELS q1_binary 0 'No' 1 'Yes'."
    )
    bound = _compile(source, _schema(VariableDefinition("q1", "numeric")))

    manifest_path = (
        Path(__file__).parents[2]
        / "worktree-spec-transform-plan"
        / "conformance"
        / "transformation-plan-0.1.json"
    )
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = next(
            case["plan"]
            for case in manifest["cases"]
            if case["id"] == "numeric-recode-and-declared-labels"
        )
        assert bound.plan.as_dict() == expected

    assert canonical_plan_json(bound.plan) == canonical_plan_json(
        bound.plan.as_dict()
    )
    assert canonical_plan_hash(bound.plan) == bound.plan.sha256()
    assert bound.output_schema.variables[-1].variable_label == "Positive response"
    assert [label.label for label in bound.output_schema.variables[-1].value_labels] == [
        "No",
        "Yes",
    ]


def test_recode_varlists_lower_positionally_from_precommand_state() -> None:
    bound = _compile(
        "RECODE first second (1 = 9) INTO first_new second_new.",
        _schema(
            VariableDefinition("first", "numeric"),
            VariableDefinition("second", "numeric"),
        ),
    )
    assert [
        (operation.source, operation.target)
        for operation in bound.plan.operations
    ] == [("first", "first_new"), ("second", "second_new")]
    assert [variable.name for variable in bound.output_schema.variables] == [
        "first",
        "second",
        "first_new",
        "second_new",
    ]


def test_recode_varlist_target_count_and_collisions_are_rejected() -> None:
    schema = _schema(
        VariableDefinition("first", "numeric"),
        VariableDefinition("second", "numeric"),
    )
    assert _error(
        "RECODE first second (1 = 9) INTO only_one.", schema
    ).code == "spss_syntax_error"
    assert _error(
        "RECODE first second (1 = 9) INTO new NEW.", schema
    ).code == "target_already_exists"
    assert _error(
        "RECODE first (1 = 9) INTO second.", schema
    ).code == "target_already_exists"


def test_value_label_varlists_and_slash_groups_preserve_source_order() -> None:
    bound = _compile(
        "VALUE LABELS a b 0 'No' 1 'Yes' / color 'R' 'Red'.",
        _schema(
            VariableDefinition("a", "numeric"),
            VariableDefinition("b", "numeric"),
            VariableDefinition("color", "string"),
        ),
    )
    assert [operation.variable for operation in bound.plan.operations] == [
        "a",
        "b",
        "color",
    ]
    assert [
        label.label
        for label in bound.output_schema.variables[0].value_labels
    ] == ["No", "Yes"]
    assert bound.output_schema.variables[2].value_labels[0].value == TypedValue.string(
        "R"
    )


def test_in_place_recode_preserves_all_existing_metadata() -> None:
    labels = (
        ValueLabel(TypedValue.binary64(1), "One"),
        ValueLabel(TypedValue.binary64(2), "Two"),
    )
    original = VariableDefinition(
        "score", "numeric", variable_label="Score", value_labels=labels
    )
    bound = _compile("RECODE score (1 = 2).", _schema(original))
    assert bound.output_schema.variables == (original,)

    changed = _compile(
        "RECODE score (1 = 2). "
        "VARIABLE LABELS score 'Changed'. "
        "VALUE LABELS score 2 'Two only'.",
        _schema(original),
    )
    output = changed.output_schema.variables[0]
    assert output.variable_label == "Changed"
    assert [label.label for label in output.value_labels] == ["Two only"]


def test_strings_quotes_case_and_exact_catalog_spelling() -> None:
    bound = _compile(
        "variable labels NAME 'O''Brien – nimi'. "
        "value labels Name 'x' 'Täpselt'.",
        _schema(VariableDefinition("Name", "string")),
    )
    assert [operation.variable for operation in bound.plan.operations] == [
        "Name",
        "Name",
    ]
    assert bound.output_schema.variables[0].variable_label == "O'Brien – nimi"


def test_source_normalization_hash_and_positions_are_stable() -> None:
    lf = "VARIABLE LABELS q1 'One'.\nVALUE LABELS q1 1 'Yes'."
    crlf = lf.replace("\n", "\r\n")
    assert normalize_spss_source(crlf) == lf
    assert spss_source_hash(crlf) == spss_source_hash(lf)
    program = parse_spss_syntax(crlf)
    assert program.commands[1].span.start.line == 2
    compilation = compile_spss_syntax(
        crlf, _schema(VariableDefinition("q1", "numeric"))
    )
    assert compilation.source_text_lf == lf
    assert compilation.source_hash == spss_source_hash(lf)
    assert compilation.plan_hash == compilation.plan.sha256()


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("FREQUENCIES q1.", "unsupported_spss_command"),
        ("COMMENT ignored.", "unsupported_spss_command"),
        ("* ignored.", "unsupported_spss_command"),
        ("RECODE q1 (1 = 0) /* ignored */.", "spss_syntax_error"),
        ("RECODE q1 (ELSE = 0) (1 = 1).", "else_not_last"),
        ("RECODE q1 (1 = 0) (ELSE = 1) (ELSE = 2).", "duplicate_else"),
        ("RECODE q1 (5 THRU 3 = 1).", "invalid_numeric_range"),
        ("VARIABLE LABELS missing 'No'.", "unknown_variable"),
    ],
)
def test_stable_failures(source: str, code: str) -> None:
    assert _error(
        source, _schema(VariableDefinition("q1", "numeric"))
    ).code == code


def test_string_create_requires_declaration_before_mixed_type_diagnostic() -> None:
    error = _error(
        "RECODE color ('R' = 'red') INTO normalized.",
        _schema(VariableDefinition("color", "string")),
    )
    assert error.code == "string_target_requires_declaration"


def test_negative_zero_canonicalizes_and_collides_with_positive_zero() -> None:
    error = _error(
        "VALUE LABELS q1 -0 'Minus' 0 'Plus'.",
        _schema(VariableDefinition("q1", "numeric")),
    )
    assert error.code == "duplicate_value_label"
    assert TypedValue.binary64(-0.0).bits == "0000000000000000"


def test_strict_plan_loader_rejects_runtime_type_confusion() -> None:
    raw = {
        "contract": "openstatspec-transformation-plan-v0.1",
        "input_alias": "parent",
        "operations": [
            {
                "op": "set_variable_label",
                "variable": 7,
                "label": "bad",
            }
        ],
    }
    with pytest.raises(TransformationFrontendError) as caught:
        transformation_plan_from_dict(raw)
    assert caught.value.code == "invalid_transformation_plan"


def test_custom_nonempty_input_alias_is_canonical() -> None:
    plan = bind_spss_syntax(
        parse_spss_syntax("VARIABLE LABELS q1 'One'."),
        _schema(VariableDefinition("q1", "numeric")),
        input_alias="survey",
    ).plan
    assert plan.input_alias == "survey"


def test_generic_plan_binding_validates_sequential_schema_state() -> None:
    plan = TransformationPlan((
        RecodeOperation(
            source="q1",
            target="q1_binary",
            target_mode="create",
            rules=(
                RecodeRule(
                    RecodeMatch("values", (TypedValue.binary64(1),)),
                    RecodeResult("literal", TypedValue.binary64(1)),
                ),
            ),
            unmatched=RecodeResult("literal", TypedValue.binary64(0)),
        ),
        SetVariableLabelOperation("q1_binary", "Binary response"),
    ))

    bound = bind_transformation_plan(
        plan, _schema(VariableDefinition("q1", "numeric"))
    )

    assert [variable.name for variable in bound.output_schema.variables] == [
        "q1",
        "q1_binary",
    ]
    assert bound.output_schema.variables[-1].variable_label == "Binary response"
