"""High-level, deterministic SPSS-like frontend compilation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...transform.errors import TransformationFrontendError, frontend_error
from ...transform.plan import (
    TRANSFORMATION_PLAN_SCHEMA_CHANGE_CONTRACT,
    TransformationPlan, TypedValue, ValueLabel,
)
from ...transform.schema import BoundTransformation, VariableDefinition, VariableSchema
from .binding import bind_spss_syntax
from .syntax import (
    normalize_spss_source,
    parse_spss_syntax,
    spss_source_hash,
)


SPSS_FRONTEND_CONTRACT = "openstatspec-spss-syntax-frontend-v0.2"
SPSS_FRONTEND_V03_CONTRACT = "openstatspec-spss-syntax-frontend-v0.3"
# Python-owned schema extension, not the official syntax-only Frontend 0.3.
SPSS_FRONTEND_SCHEMA_CHANGE_CONTRACT = "openstatspec-python-schema-change-spss-v0.1"


@dataclass(frozen=True)
class SpssFrontendCompilation:
    """One source artifact and its fully bound canonical plan."""

    source_text_lf: str
    source_hash: str
    bound: BoundTransformation
    selected_contract: str | None = None

    @property
    def plan(self) -> TransformationPlan:
        return self.bound.plan

    @property
    def plan_hash(self) -> str:
        return self.plan.sha256()

    @property
    def frontend_contract(self) -> str:
        if self.selected_contract is not None:
            return self.selected_contract
        if self.plan.contract == TRANSFORMATION_PLAN_SCHEMA_CHANGE_CONTRACT:
            return SPSS_FRONTEND_SCHEMA_CHANGE_CONTRACT
        return SPSS_FRONTEND_CONTRACT


def compile_spss_syntax(
    source: str,
    schema: VariableSchema,
    *,
    input_alias: str = "parent",
    frontend_contract: str | None = None,
) -> SpssFrontendCompilation:
    """Parse and bind source without SQL generation or database mutation."""
    if frontend_contract not in (None, SPSS_FRONTEND_V03_CONTRACT):
        raise frontend_error("invalid_spss_request", "Select the exact official Frontend 0.3 contract or omit the selector for legacy behavior.")
    normalized = normalize_spss_source(source)
    program = parse_spss_syntax(normalized, official_v03=frontend_contract is not None)
    try:
        bound = bind_spss_syntax(program, schema, input_alias=input_alias)
    except TransformationFrontendError as error:
        if frontend_contract is not None and error.span is None:
            error.span = program.span
        raise
    return SpssFrontendCompilation(
        source_text_lf=normalized,
        source_hash=spss_source_hash(normalized),
        bound=bound,
        selected_contract=frontend_contract,
    )


def compile_spss_request(request: Mapping[str, Any]) -> SpssFrontendCompilation:
    """Validate the exact official Frontend 0.3 request, then use the shared frontend."""
    def invalid():
        raise frontend_error("invalid_spss_request", "Request must conform to the official Frontend 0.3 schema.")

    if not isinstance(request, Mapping) or set(request) != {"contract", "input_alias", "input_schema", "source_text"}:
        invalid()
    if request["contract"] != SPSS_FRONTEND_V03_CONTRACT:
        invalid()
    if any(not isinstance(request[key], str) or not request[key] for key in ("input_alias", "source_text")):
        invalid()
    raw_schema = request["input_schema"]
    if not isinstance(raw_schema, Mapping) or set(raw_schema) != {"variables"}:
        invalid()
    raw_variables = raw_schema["variables"]
    if not isinstance(raw_variables, list) or not raw_variables:
        invalid()
    variables = []
    allowed = {"name", "storage_kind", "variable_label", "value_labels", "format_family", "width", "decimals", "measurement_level"}
    try:
        for raw in raw_variables:
            if not isinstance(raw, Mapping) or not {"name", "storage_kind"} <= set(raw) <= allowed:
                invalid()
            if not isinstance(raw["name"], str) or not raw["name"] or raw["storage_kind"] not in ("numeric", "string"):
                invalid()
            for key in ("variable_label", "format_family"):
                if raw.get(key) is not None and not isinstance(raw[key], str):
                    invalid()
            for key, minimum in (("width", 1), ("decimals", 0)):
                value = raw.get(key)
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum or value != int(value)):
                    invalid()
            if raw.get("measurement_level") not in (None, "nominal", "ordinal", "scale"):
                invalid()
            raw_labels = raw.get("value_labels", [])
            if not isinstance(raw_labels, list):
                invalid()
            labels = []
            for label in raw_labels:
                if not isinstance(label, Mapping) or set(label) != {"value", "label"} or not isinstance(label["label"], str):
                    invalid()
                labels.append(ValueLabel(TypedValue.from_dict(label["value"]), label["label"]))
            keys = [label.value.canonical_key() for label in labels]
            if len(keys) != len(set(keys)):
                invalid()
            variables.append(VariableDefinition(
                raw["name"], raw["storage_kind"],
                variable_label=raw.get("variable_label"), value_labels=tuple(labels),
                format_family=raw.get("format_family"),
                format_width=int(raw["width"]) if raw.get("width") is not None else None,
                format_decimals=int(raw["decimals"]) if raw.get("decimals") is not None else None,
                measurement_level=raw.get("measurement_level"), _validate_format=False,
            ))
        schema = VariableSchema(tuple(variables))
    except (ValueError, TypeError, OverflowError) as error:
        raise frontend_error("invalid_spss_request", "Invalid Frontend 0.3 input schema.") from error
    return compile_spss_syntax(request["source_text"], schema, input_alias=request["input_alias"], frontend_contract=SPSS_FRONTEND_V03_CONTRACT)
