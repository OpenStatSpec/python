"""Generic in-memory variable schema and bound transformation models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .plan import TransformationPlan, ValueLabel


StorageKind = Literal["numeric", "string"]


@dataclass(frozen=True)
class VariableDefinition:
    name: str
    storage_kind: StorageKind
    variable_label: str | None = None
    value_labels: tuple[ValueLabel, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Variable name must be non-empty text.")
        if self.storage_kind not in {"numeric", "string"}:
            raise ValueError("storage_kind must be numeric or string.")
        expected_type = "binary64" if self.storage_kind == "numeric" else "string"
        if any(label.value.type != expected_type for label in self.value_labels):
            raise ValueError(
                "Value-label types must match their variable storage kind."
            )


@dataclass(frozen=True)
class VariableSchema:
    variables: tuple[VariableDefinition, ...]

    def __post_init__(self) -> None:
        names = [variable.name.casefold() for variable in self.variables]
        if len(names) != len(set(names)):
            raise ValueError("Variable names must be unique case-insensitively.")


@dataclass(frozen=True)
class BoundTransformation:
    plan: TransformationPlan
    output_schema: VariableSchema
