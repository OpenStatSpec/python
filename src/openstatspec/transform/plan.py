"""Canonical generic OpenStatSpec transformation-plan models."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import struct
from typing import Any, Literal, Mapping, Sequence

import rfc8785

from .errors import frontend_error


TRANSFORMATION_PLAN_CONTRACT = "openstatspec-transformation-plan-v0.1"
_BINARY64 = re.compile(r"[0-9a-f]{16}")


def _invalid(detail: str, **details: Any):
    raise frontend_error("invalid_transformation_plan", detail, **details)


@dataclass(frozen=True)
class TypedValue:
    type: Literal["binary64", "string"]
    bits: str | None = None
    value: str | None = None

    def __post_init__(self) -> None:
        if self.type == "binary64":
            if self.value is not None or not isinstance(self.bits, str):
                _invalid("A binary64 value requires bits and forbids string value.")
            if _BINARY64.fullmatch(self.bits) is None:
                _invalid("binary64 bits must be exactly 16 lowercase hexadecimal digits.")
            number = struct.unpack(">d", bytes.fromhex(self.bits))[0]
            if not math.isfinite(number):
                _invalid("binary64 plan values must be finite.")
            if self.bits == "8000000000000000":
                _invalid("Negative zero is not canonical; use positive-zero bits.")
        elif self.type == "string":
            if self.bits is not None or not isinstance(self.value, str):
                _invalid("A string value requires exact text and forbids bits.")
        else:  # pragma: no cover - guarded by the public type and from_dict
            _invalid("Typed value type must be binary64 or string.")

    @classmethod
    def binary64(cls, value: float) -> "TypedValue":
        number = float(value)
        if not math.isfinite(number):
            _invalid("binary64 plan values must be finite.")
        if number == 0.0:
            number = 0.0
        return cls("binary64", bits=struct.pack(">d", number).hex())

    @classmethod
    def string(cls, value: str) -> "TypedValue":
        return cls("string", value=value)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TypedValue":
        if not isinstance(raw, Mapping):
            _invalid("Typed value must be an object.")
        if raw.get("type") == "binary64" and set(raw) == {"type", "bits"}:
            return cls("binary64", bits=raw.get("bits"))
        if raw.get("type") == "string" and set(raw) == {"type", "value"}:
            return cls("string", value=raw.get("value"))
        _invalid("Typed value has an unknown type or unexpected fields.")

    def as_dict(self) -> dict[str, str]:
        if self.type == "binary64":
            assert self.bits is not None
            return {"type": "binary64", "bits": self.bits}
        assert self.value is not None
        return {"type": "string", "value": self.value}

    def canonical_key(self) -> tuple[str, str]:
        return (
            self.type,
            self.bits if self.type == "binary64" else str(self.value),
        )

    def number(self) -> float:
        if self.type != "binary64" or self.bits is None:
            raise TypeError("Only binary64 values have a numeric value.")
        return struct.unpack(">d", bytes.fromhex(self.bits))[0]


@dataclass(frozen=True)
class RecodeMatch:
    kind: Literal["values", "range", "system_missing"]
    values: tuple[TypedValue, ...] = ()
    lower: TypedValue | None = None
    upper: TypedValue | None = None

    def __post_init__(self) -> None:
        if self.kind == "values":
            if not self.values or self.lower is not None or self.upper is not None:
                _invalid("A values match requires a non-empty values array only.")
            if not all(isinstance(value, TypedValue) for value in self.values):
                _invalid("Every values-match entry must be a typed value.")
            keys = [value.canonical_key() for value in self.values]
            if len(keys) != len(set(keys)):
                _invalid("A values match cannot contain duplicate typed values.")
        elif self.kind == "range":
            if self.values or self.lower is None or self.upper is None:
                _invalid("A range match requires lower and upper only.")
            if self.lower.type != "binary64" or self.upper.type != "binary64":
                _invalid("Range endpoints must be binary64 values.")
            if self.lower.number() > self.upper.number():
                _invalid("Range lower endpoint cannot exceed its upper endpoint.")
        elif self.kind == "system_missing":
            if self.values or self.lower is not None or self.upper is not None:
                _invalid("A system_missing match has no value fields.")
        else:  # pragma: no cover
            _invalid("Unknown recode match kind.")

    def as_dict(self) -> dict[str, Any]:
        if self.kind == "values":
            return {"kind": "values", "values": [value.as_dict() for value in self.values]}
        if self.kind == "range":
            assert self.lower is not None and self.upper is not None
            return {
                "kind": "range", "lower": self.lower.as_dict(),
                "upper": self.upper.as_dict(),
            }
        return {"kind": "system_missing"}


@dataclass(frozen=True)
class RecodeResult:
    kind: Literal["literal", "system_missing", "copy"]
    value: TypedValue | None = None

    def __post_init__(self) -> None:
        if self.kind == "literal":
            if not isinstance(self.value, TypedValue):
                _invalid("A literal result requires a typed value.")
        elif self.kind in {"system_missing", "copy"}:
            if self.value is not None:
                _invalid(f"A {self.kind} result cannot contain a value.")
        else:  # pragma: no cover
            _invalid("Unknown recode result kind.")

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind}
        if self.value is not None:
            result["value"] = self.value.as_dict()
        return result


@dataclass(frozen=True)
class RecodeRule:
    match: RecodeMatch
    result: RecodeResult

    def __post_init__(self) -> None:
        if not isinstance(self.match, RecodeMatch):
            _invalid("A recode rule requires a typed match.")
        if not isinstance(self.result, RecodeResult):
            _invalid("A recode rule requires a typed result.")

    def as_dict(self) -> dict[str, Any]:
        return {"match": self.match.as_dict(), "result": self.result.as_dict()}


@dataclass(frozen=True)
class RecodeOperation:
    source: str
    target: str
    target_mode: Literal["create", "replace"]
    rules: tuple[RecodeRule, ...]
    unmatched: RecodeResult
    op: Literal["recode"] = "recode"

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source:
            _invalid("Recode source name must be non-empty text.")
        if not isinstance(self.target, str) or not self.target:
            _invalid("Recode source and target names must be non-empty.")
        if self.op != "recode":
            _invalid("Recode operation discriminator is invalid.")
        if self.target_mode not in {"create", "replace"}:
            _invalid("Recode target_mode must be create or replace.")
        if not isinstance(self.rules, tuple) or not self.rules:
            _invalid("Recode requires at least one non-ELSE rule.")
        if not all(isinstance(rule, RecodeRule) for rule in self.rules):
            _invalid("Recode rules must be typed rule objects.")
        if not isinstance(self.unmatched, RecodeResult):
            _invalid("Recode unmatched behavior must be a typed result.")
        if self.target_mode == "replace" and self.source != self.target:
            _invalid("A replace recode must target its source variable.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "op": self.op, "source": self.source, "target": self.target,
            "target_mode": self.target_mode,
            "rules": [rule.as_dict() for rule in self.rules],
            "unmatched": self.unmatched.as_dict(),
        }


@dataclass(frozen=True)
class SetVariableLabelOperation:
    variable: str
    label: str
    op: Literal["set_variable_label"] = "set_variable_label"

    def __post_init__(self) -> None:
        if self.op != "set_variable_label":
            _invalid("Variable-label operation discriminator is invalid.")
        if not isinstance(self.variable, str) or not self.variable or not isinstance(self.label, str):
            _invalid("Variable-label operation requires a variable and text label.")

    def as_dict(self) -> dict[str, Any]:
        return {"op": self.op, "variable": self.variable, "label": self.label}


@dataclass(frozen=True)
class ValueLabel:
    value: TypedValue
    label: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, TypedValue) or not isinstance(self.label, str):
            _invalid("A value label must be text.")

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value.as_dict(), "label": self.label}


@dataclass(frozen=True)
class ReplaceValueLabelsOperation:
    variable: str
    labels: tuple[ValueLabel, ...]
    op: Literal["replace_value_labels"] = "replace_value_labels"

    def __post_init__(self) -> None:
        if self.op != "replace_value_labels":
            _invalid("Value-label operation discriminator is invalid.")
        if not isinstance(self.variable, str) or not self.variable or not self.labels:
            _invalid("replace_value_labels requires a variable and non-empty labels.")
        if not isinstance(self.labels, tuple) or not all(
            isinstance(label, ValueLabel) for label in self.labels
        ):
            _invalid("replace_value_labels labels must be typed value-label objects.")
        keys = [label.value.canonical_key() for label in self.labels]
        if len(keys) != len(set(keys)):
            _invalid("replace_value_labels cannot contain duplicate typed values.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "op": self.op, "variable": self.variable,
            "labels": [label.as_dict() for label in self.labels],
        }


PlanOperation = RecodeOperation | SetVariableLabelOperation | ReplaceValueLabelsOperation


@dataclass(frozen=True)
class TransformationPlan:
    operations: tuple[PlanOperation, ...]
    contract: str = TRANSFORMATION_PLAN_CONTRACT
    input_alias: str = "parent"

    def __post_init__(self) -> None:
        if self.contract != TRANSFORMATION_PLAN_CONTRACT:
            _invalid(f"Plan contract must be {TRANSFORMATION_PLAN_CONTRACT!r}.")
        if not isinstance(self.input_alias, str) or not self.input_alias:
            _invalid("Plan input_alias must be non-empty text.")
        if not isinstance(self.operations, tuple) or not self.operations:
            _invalid("A transformation plan requires at least one operation.")
        if not all(
            isinstance(
                operation,
                (RecodeOperation, SetVariableLabelOperation, ReplaceValueLabelsOperation),
            )
            for operation in self.operations
        ):
            _invalid("Plan operations must be typed operation objects.")

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract, "input_alias": self.input_alias,
            "operations": [operation.as_dict() for operation in self.operations],
        }

    def canonical_bytes(self) -> bytes:
        return rfc8785.dumps(self.as_dict())

    def canonical_json(self) -> str:
        return self.canonical_bytes().decode("utf-8")

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _exact(raw: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(raw) != fields:
        _invalid(f"{label} fields must be exactly {sorted(fields)!r}.")


def _typed(raw: Any) -> TypedValue:
    if not isinstance(raw, Mapping):
        _invalid("Typed value must be an object.")
    return TypedValue.from_dict(raw)


def _result(raw: Any) -> RecodeResult:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("kind"), str):
        _invalid("Recode result must be an object with a kind.")
    kind = raw["kind"]
    if kind == "literal":
        _exact(raw, {"kind", "value"}, "Literal result")
        return RecodeResult("literal", _typed(raw["value"]))
    if kind in {"system_missing", "copy"}:
        _exact(raw, {"kind"}, f"{kind} result")
        return RecodeResult(kind)
    _invalid("Unknown recode result kind.")


def _match(raw: Any) -> RecodeMatch:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("kind"), str):
        _invalid("Recode match must be an object with a kind.")
    kind = raw["kind"]
    if kind == "values":
        _exact(raw, {"kind", "values"}, "Values match")
        values = raw["values"]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            _invalid("Values match values must be an array.")
        return RecodeMatch("values", tuple(_typed(value) for value in values))
    if kind == "range":
        _exact(raw, {"kind", "lower", "upper"}, "Range match")
        return RecodeMatch("range", lower=_typed(raw["lower"]), upper=_typed(raw["upper"]))
    if kind == "system_missing":
        _exact(raw, {"kind"}, "system_missing match")
        return RecodeMatch("system_missing")
    _invalid("Unknown recode match kind.")


def transformation_plan_from_dict(raw: Mapping[str, Any]) -> TransformationPlan:
    """Strictly validate and construct the canonical v0.1 plan document."""
    if not isinstance(raw, Mapping):
        _invalid("Transformation plan must be an object.")
    _exact(raw, {"contract", "input_alias", "operations"}, "Transformation plan")
    operations_raw = raw["operations"]
    if not isinstance(operations_raw, Sequence) or isinstance(operations_raw, (str, bytes)):
        _invalid("Plan operations must be an array.")
    operations: list[PlanOperation] = []
    for raw_operation in operations_raw:
        if not isinstance(raw_operation, Mapping):
            _invalid("Every plan operation must be an object.")
        operation = raw_operation.get("op")
        if operation == "recode":
            _exact(
                raw_operation,
                {"op", "source", "target", "target_mode", "rules", "unmatched"},
                "Recode operation",
            )
            rules_raw = raw_operation["rules"]
            if not isinstance(rules_raw, Sequence) or isinstance(rules_raw, (str, bytes)):
                _invalid("Recode rules must be an array.")
            rules = []
            for raw_rule in rules_raw:
                if not isinstance(raw_rule, Mapping):
                    _invalid("Every recode rule must be an object.")
                _exact(raw_rule, {"match", "result"}, "Recode rule")
                rules.append(RecodeRule(_match(raw_rule["match"]), _result(raw_rule["result"])))
            operations.append(RecodeOperation(
                source=raw_operation["source"], target=raw_operation["target"],
                target_mode=raw_operation["target_mode"], rules=tuple(rules),
                unmatched=_result(raw_operation["unmatched"]),
            ))
        elif operation == "set_variable_label":
            _exact(raw_operation, {"op", "variable", "label"}, "Variable-label operation")
            operations.append(SetVariableLabelOperation(
                raw_operation["variable"], raw_operation["label"],
            ))
        elif operation == "replace_value_labels":
            _exact(raw_operation, {"op", "variable", "labels"}, "Value-label operation")
            labels_raw = raw_operation["labels"]
            if not isinstance(labels_raw, Sequence) or isinstance(labels_raw, (str, bytes)):
                _invalid("Value labels must be an array.")
            labels = []
            for raw_label in labels_raw:
                if not isinstance(raw_label, Mapping):
                    _invalid("Every value label must be an object.")
                _exact(raw_label, {"value", "label"}, "Value label")
                labels.append(ValueLabel(_typed(raw_label["value"]), raw_label["label"]))
            operations.append(ReplaceValueLabelsOperation(
                raw_operation["variable"], tuple(labels),
            ))
        else:
            _invalid(f"Unknown plan operation {operation!r}.")
    return TransformationPlan(
        tuple(operations), contract=raw["contract"], input_alias=raw["input_alias"],
    )


def canonical_plan_json(plan: TransformationPlan | Mapping[str, Any]) -> str:
    normalized = (
        plan if isinstance(plan, TransformationPlan)
        else transformation_plan_from_dict(plan)
    )
    return normalized.canonical_json()


def canonical_plan_hash(plan: TransformationPlan | Mapping[str, Any]) -> str:
    normalized = (
        plan if isinstance(plan, TransformationPlan)
        else transformation_plan_from_dict(plan)
    )
    return normalized.sha256()
