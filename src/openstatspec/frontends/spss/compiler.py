"""High-level, deterministic SPSS-like frontend compilation."""

from __future__ import annotations

from dataclasses import dataclass

from ...transform.plan import TransformationPlan
from ...transform.schema import BoundTransformation, VariableSchema
from .binding import bind_spss_syntax
from .syntax import (
    normalize_spss_source,
    parse_spss_syntax,
    spss_source_hash,
)


@dataclass(frozen=True)
class SpssFrontendCompilation:
    """One source artifact and its fully bound canonical plan."""

    source_text_lf: str
    source_hash: str
    bound: BoundTransformation

    @property
    def plan(self) -> TransformationPlan:
        return self.bound.plan

    @property
    def plan_hash(self) -> str:
        return self.plan.sha256()


def compile_spss_syntax(
    source: str,
    schema: VariableSchema,
    *,
    input_alias: str = "parent",
) -> SpssFrontendCompilation:
    """Parse and bind source without SQL generation or database mutation."""
    normalized = normalize_spss_source(source)
    bound = bind_spss_syntax(
        parse_spss_syntax(normalized),
        schema,
        input_alias=input_alias,
    )
    return SpssFrontendCompilation(
        source_text_lf=normalized,
        source_hash=spss_source_hash(normalized),
        bound=bound,
    )
