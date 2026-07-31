"""Stable diagnostics and source locations for transformation frontends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourcePosition:
    """One zero-based byte-independent offset and one-based display position."""

    offset: int
    line: int
    column: int

    def as_dict(self) -> dict[str, int]:
        return {"offset": self.offset, "line": self.line, "column": self.column}


@dataclass(frozen=True)
class SourceSpan:
    """Half-open source range."""

    start: SourcePosition
    end: SourcePosition

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {"start": self.start.as_dict(), "end": self.end.as_dict()}


class TransformationFrontendError(ValueError):
    """A safe, machine-readable frontend or binding failure."""

    def __init__(
        self, code: str, detail: str, *, span: SourceSpan | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"Transformation frontend failed [{code}]: {detail}")
        self.code = code
        self.detail = detail
        self.span = span
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "detail": self.detail}
        if self.span is not None:
            result["span"] = self.span.as_dict()
        if self.details:
            result["details"] = dict(self.details)
        return result


def frontend_error(
    code: str, detail: str, *, span: SourceSpan | None = None, **details: Any,
) -> TransformationFrontendError:
    return TransformationFrontendError(
        code, detail, span=span, details=details or None,
    )
