from .results import Diagnostic, OperationResult
"""Pure OpenStatSpec concepts; no file or database adapter code."""

from dataclasses import dataclass, field
from typing import Any


class UnsupportedOperationError(NotImplementedError):
    """Raised when faithful support for a requested operation is unavailable."""


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
