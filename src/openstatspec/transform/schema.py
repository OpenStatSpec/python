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
    format_family: str | None = None
    format_width: int | None = None
    format_decimals: int | None = None
    measurement_level: Literal["nominal", "ordinal", "scale"] | None = None

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

        format_parts = (self.format_family, self.format_width, self.format_decimals)
        if any(part is not None for part in format_parts):
            if any(part is None for part in format_parts):
                raise ValueError("Format family, width, and decimals must be set together.")
            if self.storage_kind != "numeric" or self.format_family != "F":
                raise ValueError("The bounded schema supports F formats on numeric variables only.")
            if (
                not isinstance(self.format_width, int)
                or isinstance(self.format_width, bool)
                or not 1 <= self.format_width <= 40
            ):
                raise ValueError("F format width must be an integer from 1 through 40.")
            if (
                not isinstance(self.format_decimals, int)
                or isinstance(self.format_decimals, bool)
                or not 0 <= self.format_decimals <= 16
                or (
                    self.format_decimals != 0
                    and self.format_width < self.format_decimals + 2
                )
            ):
                raise ValueError(
                    "F format decimals must be zero through 16 and fit the width."
                )
        if self.measurement_level not in {None, "nominal", "ordinal", "scale"}:
            raise ValueError("measurement_level must be nominal, ordinal, scale, or None.")

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
