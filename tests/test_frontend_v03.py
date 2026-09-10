"""Official Frontend 0.3 evidence; fixtures remain specification-owned."""
from copy import deepcopy
import json

import pytest

import openstatspec
from openstatspec.frontends.spss import compiler
from openstatspec.frontends.spss import spss_source_hash
from test_transform_frontend import _frontend_conformance_manifest

CONTRACT = "openstatspec-spss-syntax-frontend-v0.3"


def _cases():
    root = _frontend_conformance_manifest().parent
    manifest = json.loads((root / "spss-syntax-frontend-0.3.json").read_text())
    cases = [("0.3", case) for case in manifest["cases"]]
    assert len(cases) == 35
    for inherited in manifest["inherited_manifests"]:
        parent = json.loads((root / inherited["manifest"]).read_text())
        for case in parent["cases"]:
            if case["id"] not in inherited["superseded_cases"]:
                case = deepcopy(case)
                case["request"]["contract"] = inherited["request_contract_override"]
                cases.append((parent["manifest_version"], case))
    assert len(cases) == 90
    return cases


@pytest.mark.parametrize("version,case", _cases(), ids=lambda x: x["id"] if isinstance(x, dict) else x)
def test_effective_official_frontend_v03(version, case):
    request = case["request"]
    assert request["contract"] == CONTRACT
    assert spss_source_hash(request["source_text"]) == case["expected_source_hash"]
    if case["expected_error"]:
        with pytest.raises(openstatspec.TransformationFrontendError) as caught:
            compiler.compile_spss_request(request)
        assert caught.value.code == case["expected_error"]
        assert caught.value.span is not None
        return
    compilation = compiler.compile_spss_request(request)
    expected = case.get("expected_plan", case.get("expected_plan_0_1"))
    expected_hash = case.get("expected_plan_hash")
    reference = case.get("expected_plan_case", case.get("expected_plan_case_0_1"))
    if reference:
        plan_version = "0.1" if "expected_plan_case_0_1" in case else version
        manifest = json.loads((_frontend_conformance_manifest().parent / f"transformation-plan-{plan_version}.json").read_text())
        plan_case = next((c for c in manifest["cases"] if c["id"] == reference), None)
        if plan_case is None:
            frontend = json.loads((_frontend_conformance_manifest().parent / f"spss-syntax-frontend-{plan_version}.json").read_text())
            plan_case = next(c for c in frontend["cases"] if c["id"] == reference)
        expected = plan_case.get("plan", plan_case.get("expected_plan"))
        assert expected_hash is None or expected_hash == plan_case["expected_plan_hash"]
        expected_hash = plan_case["expected_plan_hash"]
    assert expected is not None
    assert compilation.plan.as_dict() == expected
    assert compilation.plan_hash == expected_hash
    assert compilation.frontend_contract == CONTRACT
    assert compilation.source_hash == case["expected_source_hash"]
    if "expected_output_metadata" in case:
        assert {v.name: {"variable_label": v.variable_label, "value_labels": [x.as_dict() for x in v.value_labels]}
                for v in compilation.bound.output_schema.variables} == case["expected_output_metadata"]


def _request(source="EXECUTE."):
    return {"contract": CONTRACT, "input_alias": "parent", "source_text": source,
            "input_schema": {"variables": [{"name": n, "storage_kind": "numeric"} for n in ("a", "b", "c", "target")]}}


@pytest.mark.parametrize("source,explicit", [
    ("NOT a = 1 AND b = 1 OR c = 1", "((NOT (a = 1)) AND b = 1) OR c = 1"),
    ("NOT NOT a = 1", "a = 1"),
    ("NOT (a NE 1 OR b <= 2)", "(a >= 1 AND a <= 1) AND b > 2"),
])
def test_resolved_not_precedence_and_flattening(source, explicit):
    def compile_predicate(predicate):
        return compiler.compile_spss_request(_request(f"IF ({predicate}) target = 1.")).plan
    assert compile_predicate(source) == compile_predicate(explicit)
    if source.startswith("NOT a"):
        assert compile_predicate(source) != compile_predicate("NOT (a = 1 AND b = 1 OR c = 1)")


@pytest.mark.parametrize("source", ["STRING note (A4).", "DELETE VARIABLES a."])
def test_official_rejects_python_extensions(source):
    with pytest.raises(openstatspec.TransformationFrontendError) as caught:
        compiler.compile_spss_request(_request(source))
    assert caught.value.code == "unsupported_spss_command"


@pytest.mark.parametrize("path,value", [
    (("contract",), "openstatspec-spss-syntax-frontend-v0.2"),
    (("contract",), []), (("extra",), True), (("input_alias",), 1),
    (("source_text",), ""), (("input_schema", "extra"), 1),
    (("input_schema", "variables"), []),
    (("input_schema", "variables"), ()),
    (("input_schema", "variables", 0, "physical_name"), "a"),
    (("input_schema", "variables", 0, "declared_string_width"), 4),
    (("input_schema", "variables", 0, "width"), True),
    (("input_schema", "variables", 0, "decimals"), -1),
    (("input_schema", "variables", 0, "variable_label"), 1),
    (("input_schema", "variables", 0, "format_family"), []),
    (("input_schema", "variables", 0, "measurement_level"), {}),
    (("input_schema", "variables", 0, "storage_kind"), []),
    (("input_schema", "variables", 0, "value_labels"), [{"value": {"type": "binary64", "bits": "8000000000000000"}, "label": "zero"}]),
    (("input_schema", "variables", 0, "value_labels"), [{"value": {"type": "binary64", "bits": "7ff0000000000000"}, "label": "infinity"}]),
    (("input_schema", "variables", 0, "value_labels"), [{"value": {"type": "string", "value": "x"}, "label": 2}]),
])
def test_request_boundary_rejects_wrong_fields_and_types(path, value):
    request = _request()
    target = request
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(openstatspec.TransformationFrontendError) as caught:
        compiler.compile_spss_request(request)
    assert caught.value.code == "invalid_spss_request"


def test_grouped_commands_and_comments_preserve_order_and_metadata():
    source = ("/* before */ COMMENT command. * star.\r\n"
              "RECODE a TO b (LOWEST THRU 1 = 0) / c (1 THRU HIGHEST = 2).\r\n"
              "VARIABLE LABELS a TO b '/* literal */ O''Brien' / c 'Third'. "
              "FORMATS a b (F8.2) / c (F4.0). "
              "VARIABLE LEVEL a TO b (ORDINAL) / c (SCALE). "
              "VALUE LABELS a TO b -0 'Zero' 1 'One'. "
              "ADD VALUE LABELS a TO b 2 'Two' 0 'Updated' / c 3 'Three'.")
    compilation = openstatspec.compile_spss_request(_request(source))
    operations = compilation.plan.operations
    assert [op.op for op in operations] == (
        ["recode"] * 3 + ["set_variable_label"] * 3 + ["set_format"] * 3
        + ["set_measurement_level"] * 3 + ["replace_value_labels"] * 5
    )
    a, b, c, _ = compilation.bound.output_schema.variables
    for variable in (a, b):
        assert variable.variable_label == "/* literal */ O'Brien"
        assert (variable.format_family, variable.format_width, variable.format_decimals, variable.measurement_level) == ("F", 8, 2, "ordinal")
        assert [(x.value.number(), x.label) for x in variable.value_labels] == [(0, "Updated"), (1, "One"), (2, "Two")]
    assert c.variable_label == "Third"
    assert (c.format_width, c.format_decimals, c.measurement_level) == (4, 0, "scale")
    assert compilation.source_text_lf == source.replace("\r\n", "\n")
    assert compilation.source_hash == spss_source_hash(source)


@pytest.mark.parametrize("source", [
    "COMMENT text. EXECUTE.", "* text. EXECUTE.", "/* text */ EXECUTE.",
    "FORMATS a TO c (F8.0).", "FORMATS a b (F8.0).",
    "VARIABLE LABELS a b 'Both'.", "RECODE a (1 = 0) / b (1 = 0).",
    "IF (a NE 1) target = 1.", "IF (a <> 1) target = 1.",
    "IF (a ~= 1) target = 1.", "IF (NOT a = 1) target = 1.",
    "RECODE a (LOWEST THRU HIGHEST = 0).", "ADD VALUE LABELS a 1 'One'.",
])
def test_official_expansion_does_not_change_default_rejections(source):
    request = _request(source)
    schema = openstatspec.VariableSchema(tuple(openstatspec.VariableDefinition(v["name"], v["storage_kind"]) for v in request["input_schema"]["variables"]))
    with pytest.raises(openstatspec.TransformationFrontendError):
        openstatspec.compile_spss_syntax(source, schema)
    assert openstatspec.compile_spss_request(request).frontend_contract == CONTRACT


@pytest.mark.parametrize("source,code", [
    ("/* outer /* inner */ */ EXECUTE.", "spss_syntax_error"),
    ("COMPUTE a = 1 * not a comment.", "spss_syntax_error"),
    ("COMMENT unterminated", "spss_syntax_error"),
    ("EXECUTE. VALUE LABELS.", "spss_syntax_error"),
    ("EXECUTE. ADD VALUE LABELS.", "spss_syntax_error"),
    ("RECODE a (LOWEST = 0).", "spss_syntax_error"),
    ("RECODE a (2 THRU 1 = 0).", "invalid_variable_range"),
    ("RECODE a TO c (1 = 0) INTO only_one.", "spss_syntax_error"),
    ("RECODE a (1 = 0) INTO a.", "target_already_exists"),
    ("FORMATS c TO missing (F8.0).", "unknown_variable"),
    ("FORMATS a (F8.0). VARIABLE LABELS missing 'No'.", "unknown_variable"),
    ("ADD VALUE LABELS a -0 'Minus' 0 'Plus'.", "duplicate_value_label"),
    ("EXECUTE. FORMATſ a (F8.0).", "unsupported_spss_command"),
])
def test_official_source_boundary(source, code):
    with pytest.raises(openstatspec.TransformationFrontendError) as caught:
        openstatspec.compile_spss_request(_request(source))
    assert caught.value.code == code
    assert caught.value.span is not None


def test_request_metadata_uses_schema_fields_without_coercion_or_loss():
    request = _request("VARIABLE LABELS a 'Changed'. EXECUTE.")
    request["input_schema"]["variables"] = [
        {"name": "a", "storage_kind": "string", "format_family": "A", "width": 12,
         "decimals": 0, "measurement_level": "nominal", "value_labels": [
             {"value": {"type": "string", "value": "x "}, "label": "Exact"}]},
        {"name": "b", "storage_kind": "numeric", "width": 8.0},
    ]
    before = deepcopy(request)
    a, b = openstatspec.compile_spss_request(request).bound.output_schema.variables
    assert request == before
    assert (a.format_family, a.format_width, a.format_decimals, a.measurement_level) == ("A", 12, 0, "nominal")
    assert a.value_labels[0].value.value == "x "
    assert (b.format_family, b.format_width, b.format_decimals) == (None, 8, None)


@pytest.mark.parametrize("missing", ["contract", "input_alias", "input_schema", "source_text"])
def test_request_requires_every_contract_field(missing):
    request = _request()
    del request[missing]
    with pytest.raises(openstatspec.TransformationFrontendError, match="invalid_spss_request"):
        openstatspec.compile_spss_request(request)
