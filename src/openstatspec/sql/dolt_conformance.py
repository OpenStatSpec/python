"""Thin adapter glue for the shared OpenStatSpec Dolt declaration validator."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as distribution_version
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..core import UnsupportedOperationError


def _adapter_version() -> str:
    source_project = Path(__file__).resolve().parents[3] / "pyproject.toml"
    if source_project.is_file():
        project = tomllib.loads(source_project.read_text(encoding="utf-8"))
        value = project.get("project", {}).get("version")
        if isinstance(value, str) and value:
            return value
    try:
        return distribution_version("openstatspec")
    except PackageNotFoundError as error:
        raise RuntimeError(
            "The openstatspec adapter version is unavailable from both the "
            "source project and installed distribution metadata."
        ) from error


ADAPTER_IMPLEMENTATION_ID = "openstatspec-python"
ADAPTER_VERSION = _adapter_version()


def _shared_api() -> tuple[Any, Any, Any, Any]:
    try:
        from openstatspec_specification.dolt import (
            DoltDeclarationError,
            DoltDeclarationSource,
            load_validated_dolt_declarations,
            select_dolt_declaration,
        )
    except (ImportError, ModuleNotFoundError) as error:
        raise UnsupportedOperationError(
            "The openstatspec-specification companion distribution is not "
            "installed; Dolt writes remain disabled."
        ) from error
    return (
        DoltDeclarationError,
        DoltDeclarationSource,
        load_validated_dolt_declarations,
        select_dolt_declaration,
    )


@dataclass(frozen=True)
class DoltConformanceSource:
    """Explicit source for validated packaged or directory declarations."""

    directory: Path | None = None

    @classmethod
    def packaged(cls) -> "DoltConformanceSource":
        return cls()

    @classmethod
    def from_directory(cls, root: str | Path) -> "DoltConformanceSource":
        return cls(directory=Path(root))

    def validated_declarations(self) -> tuple[Mapping[str, Any], ...]:
        (
            shared_error,
            shared_source_type,
            shared_loader,
            _shared_selector,
        ) = _shared_api()
        try:
            shared_source = (
                shared_source_type.packaged()
                if self.directory is None
                else shared_source_type.from_directory(self.directory)
            )
            return tuple(shared_loader(shared_source))
        except shared_error as error:
            raise UnsupportedOperationError(
                "The Dolt declaration source failed shared semantic or "
                "resource-integrity validation: " + str(error)
            ) from error

    def status(self) -> Mapping[str, Any]:
        try:
            declarations = self.validated_declarations()
        except UnsupportedOperationError as error:
            return {
                "write_enabled": False,
                "declarations_available": False,
                "declaration_count": 0,
                "status": "blocked_invalid_or_unavailable_declaration_source",
                "reason": str(error),
            }
        return {
            "write_enabled": False,
            "declarations_available": bool(declarations),
            "declaration_count": len(declarations),
            "status": (
                "validated_concrete_declarations_available"
                if declarations
                else "blocked_no_concrete_declarations"
            ),
        }

    def require_exact_match(
        self,
        *,
        active_product_version: str,
        specification_commit: str,
    ) -> Mapping[str, Any]:
        if (
            not isinstance(specification_commit, str)
            or len(specification_commit) != 40
            or any(character not in "0123456789abcdef" for character in specification_commit)
        ):
            raise UnsupportedOperationError(
                "Dolt conformance selection requires an exact lowercase 40-hex "
                "specification commit."
            )
        declarations = self.validated_declarations()
        if not declarations:
            raise UnsupportedOperationError(
                "The validated Dolt declaration source contains no concrete "
                "declarations; write rejected before mutation."
            )
        shared_error, _source_type, _loader, shared_selector = _shared_api()
        try:
            return shared_selector(
                declarations,
                active_product_version=active_product_version,
                adapter_implementation_id=ADAPTER_IMPLEMENTATION_ID,
                adapter_version=ADAPTER_VERSION,
                specification_commit=specification_commit,
            )
        except shared_error as error:
            raise UnsupportedOperationError(
                "No unique exact Dolt conformance declaration matches the "
                "active product, adapter, and specification binding: "
                + str(error)
            ) from error


def effective_limits(declaration: Mapping[str, Any]) -> dict[str, Any]:
    """Project the shared declaration's effective layers into adapter limits."""

    dimensions = declaration["limit_declarations"]

    def effective(name: str) -> Mapping[str, Any]:
        matches = tuple(
            row for row in dimensions[name] if row.get("basis") == "effective"
        )
        if len(matches) != 1:
            raise UnsupportedOperationError(
                "Validated Dolt declaration has no unique effective " + name + " layer."
            )
        return matches[0]

    physical = effective("physical_columns")
    source = effective("source_variables")
    identifier = effective("identifier")
    value = effective("value")
    structural = effective("structural_row")
    statement = effective("emitted_statement")
    return {
        "maximum_physical_columns": physical["value"],
        "maximum_source_variables": source["value"],
        "maximum_statement_bytes": statement["value"],
        "identifier_limit": {
            "value": identifier["value"],
            "unit": identifier["unit"],
            "source": "validated concrete Dolt declaration",
            "repertoire": declaration["identifier_limit"]["repertoire"],
        },
        "maximum_value_bytes": value["value"],
        "maximum_row_bytes": (
            structural["value"]
            if isinstance(structural["value"], int)
            and not isinstance(structural["value"], bool)
            else None
        ),
        "limit_basis": "validated_concrete_dolt_declaration",
        "sources": {
            name: "declaration:" + str(declaration["declaration_id"])
            for name in (
                "maximum_physical_columns",
                "maximum_source_variables",
                "maximum_statement_bytes",
                "identifier_limit",
                "maximum_value_bytes",
                "maximum_row_bytes",
            )
        },
    }
