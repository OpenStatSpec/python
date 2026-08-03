from .results import Diagnostic, OperationResult
"""Pure OpenStatSpec concepts; no file or database adapter code."""

import hashlib
from dataclasses import dataclass, field
from typing import Any


class UnsupportedOperationError(NotImplementedError):
    """Raised when faithful support for a requested operation is unavailable."""


def safe_error_identity(error: Exception, *, phase: str) -> dict[str, Any]:
    """Return a path-free, stable identity for an exception."""
    code = getattr(error, "code", None)
    if code is not None and not isinstance(code, (str, int, float, bool)):
        code = type(code).__name__
    return {
        "type": type(error).__name__,
        "code": code,
        "phase": phase,
        "message_sha256": hashlib.sha256(str(error).encode("utf-8")).hexdigest(),
    }


@dataclass(frozen=True)
class LossReport:
    """Machine-readable fidelity outcome for a completed future operation."""

    events: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CapabilityDeclaration:
    specification: str | None = None
    formats: dict[str, Any] = field(default_factory=dict)
    database_profiles: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "CapabilityDeclaration":
        return cls()

    def as_dict(self) -> dict[str, Any]:
        return {
            "specification": self.specification, "formats": self.formats, "database_profiles": self.database_profiles,
            "operations": {"inspect": True, "import_sav": True, "export_sav": True, "validate": True},
        }
