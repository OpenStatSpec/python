"""Typed public results while retaining mapping compatibility."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Diagnostic:
    code: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class OperationResult(Mapping[str, Any]):
    values: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result = dict(self.values)
        result["loss_report"] = [item.as_dict() for item in self.diagnostics]
        return result

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def result(values: Mapping[str, Any]) -> OperationResult:
    payload = dict(values)
    diagnostics = tuple(Diagnostic(**item) for item in payload.pop("loss_report", ()))
    return OperationResult(payload, diagnostics)