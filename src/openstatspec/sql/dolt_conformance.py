"""Adapter-owned validation for language-neutral Dolt declarations."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import PackageNotFoundError, version as distribution_version
import re
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

_EXACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_CANONICAL_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_ADAPTER_VERSION = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:(?:a|b|rc)[0-9]+|\.post[0-9]+|\.dev[0-9]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)
_REQUIRED_FIELDS = {
    "active_product_version", "adapter_implementation_id", "adapter_version",
    "conformance_run_id", "conformance_status", "declaration_id",
    "declaration_schema_id", "evidence_records", "import_enabled",
    "identifier_limit", "limit_declarations", "specification_commit",
}


def _reject(condition: bool, message: str) -> None:
    if not condition:
        raise UnsupportedOperationError(
            "Invalid adapter-owned Dolt declaration: " + message
        )


def _canonical_string(value: object, field: str) -> str:
    _reject(
        isinstance(value, str) and value == value.strip() and bool(value),
        f"invalid {field}.",
    )
    assert isinstance(value, str)
    return value


def _resource(root: Path, relative_path: str) -> Path:
    _reject(
        isinstance(relative_path, str)
        and bool(relative_path)
        and "\\" not in relative_path
        and not relative_path.startswith("/"),
        "resource path must be canonical and relative.",
    )
    parts = tuple(relative_path.split("/"))
    _reject(
        all(part not in {"", ".", ".."} for part in parts),
        "resource path is not canonical.",
    )
    candidate = root
    for part in parts:
        candidate = candidate / part
        _reject(not candidate.is_symlink(), "resource path traverses a symlink.")
    try:
        candidate.resolve().relative_to(root)
    except ValueError as error:
        raise UnsupportedOperationError(
            "Invalid adapter-owned Dolt declaration: resource path escapes "
            "its source root."
        ) from error
    return candidate


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UnsupportedOperationError(
            "Invalid adapter-owned Dolt declaration: resource is not readable "
            "UTF-8 JSON."
        ) from error


def _validate_limits(declaration: Mapping[str, Any]) -> None:
    dimensions = declaration["limit_declarations"]
    _reject(isinstance(dimensions, dict), "limit declarations must be an object.")
    effective_rows: dict[str, Mapping[str, Any]] = {}
    for name in (
        "physical_columns", "source_variables", "identifier", "value",
        "structural_row", "emitted_statement",
    ):
        rows = dimensions.get(name) if isinstance(dimensions, dict) else None
        _reject(isinstance(rows, list) and bool(rows), f"{name} limits are missing.")
        matches = tuple(
            row
            for row in rows
            if isinstance(row, dict) and row.get("basis") == "effective"
        )
        _reject(
            len(matches) == 1,
            f"{name} has no unique effective limit layer.",
        )
        effective_rows[name] = matches[0]
    for name in (
        "physical_columns", "source_variables", "identifier", "value",
        "emitted_statement",
    ):
        value = effective_rows[name].get("value")
        _reject(
            isinstance(value, int) and not isinstance(value, bool) and value > 0,
            f"{name} effective limit is not a positive integer.",
        )
    structural = effective_rows["structural_row"].get("value")
    _reject(
        (
            isinstance(structural, int)
            and not isinstance(structural, bool)
            and structural > 0
        )
        or (
            isinstance(structural, dict)
            and structural.get("kind") == "not_applicable_proof"
        ),
        "structural row effective limit is invalid.",
    )
    _reject(
        effective_rows["physical_columns"]["value"]
        == effective_rows["source_variables"]["value"] + 1,
        "effective physical columns must equal source variables plus one.",
    )
    identifier = declaration["identifier_limit"]
    _reject(
        isinstance(identifier, dict)
        and isinstance(identifier.get("repertoire"), str)
        and bool(identifier["repertoire"]),
        "identifier limit repertoire is missing.",
    )


def _validate_declaration(
    declaration: object, *, root: Path,
) -> Mapping[str, Any]:
    _reject(isinstance(declaration, dict), "declaration must be an object.")
    assert isinstance(declaration, dict)
    _reject(
        _REQUIRED_FIELDS <= set(declaration), "required fields are incomplete."
    )
    _reject(
        declaration["declaration_schema_id"]
        == "openstatspec-dolt-adapter-declaration-v1",
        "unexpected declaration schema.",
    )
    for field in (
        "declaration_id", "adapter_implementation_id", "conformance_run_id",
    ):
        value = _canonical_string(declaration[field], field)
        _reject(
            _CANONICAL_ID.fullmatch(value) is not None,
            f"noncanonical {field}.",
        )
    adapter_version = _canonical_string(
        declaration["adapter_version"], "adapter_version"
    )
    _reject(
        _ADAPTER_VERSION.fullmatch(adapter_version) is not None,
        "adapter version is not exact.",
    )
    specification_commit = _canonical_string(
        declaration["specification_commit"], "specification_commit"
    )
    _reject(
        re.fullmatch(r"[0-9a-f]{40}", specification_commit) is not None,
        "specification commit is not exact.",
    )
    active_version = _canonical_string(
        declaration["active_product_version"], "active_product_version"
    )
    _reject(
        _EXACT_VERSION.fullmatch(active_version) is not None,
        "active Dolt version is not exact.",
    )
    _reject(
        declaration["conformance_status"] == "tested"
        and declaration["import_enabled"] is True,
        "declaration is not active and tested.",
    )
    evidence = declaration["evidence_records"]
    _reject(
        isinstance(evidence, list) and bool(evidence), "evidence is required."
    )
    covered = False
    seen: set[str] = set()
    for record in evidence:
        _reject(
            isinstance(record, dict)
            and set(record)
            == {
                "artifact_ref", "artifact_sha256", "evidence_id",
                "exact_versions",
            },
            "evidence fields are incomplete.",
        )
        evidence_id = _canonical_string(record["evidence_id"], "evidence_id")
        _reject(
            _CANONICAL_ID.fullmatch(evidence_id) is not None
            and evidence_id not in seen,
            "evidence ID is invalid or duplicated.",
        )
        seen.add(evidence_id)
        versions = record["exact_versions"]
        versions_are_exact = (
            isinstance(versions, list)
            and bool(versions)
            and all(
                isinstance(item, str) and _EXACT_VERSION.fullmatch(item)
                for item in versions
            )
        )
        _reject(
            versions_are_exact and len(versions) == len(set(versions)),
            "evidence versions are not unique exact versions.",
        )
        covered = covered or active_version in versions
        artifact_ref = _canonical_string(record["artifact_ref"], "artifact_ref")
        parts = tuple(artifact_ref.split("/"))
        _reject(
            parts[:3] == ("sql", "dolt-adapter-declarations", "evidence")
            and len(parts) > 3,
            "evidence artifact is outside its directory.",
        )
        artifact_sha256 = _canonical_string(
            record["artifact_sha256"], "artifact_sha256"
        )
        _reject(
            re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is not None,
            "evidence SHA-256 is invalid.",
        )
        artifact = _resource(root, artifact_ref)
        try:
            payload = artifact.read_bytes()
        except OSError as error:
            raise UnsupportedOperationError(
                "Invalid adapter-owned Dolt declaration: evidence artifact is "
                "missing or unreadable."
            ) from error
        _reject(
            hashlib.sha256(payload).hexdigest() == artifact_sha256,
            "evidence hash differs.",
        )
    _reject(covered, "evidence does not cover the active Dolt version.")
    _validate_limits(declaration)
    return declaration


@dataclass(frozen=True)
class DoltConformanceSource:
    """Explicit source for adapter-owned declaration and evidence files."""

    directory: Path | None = None

    @classmethod
    def packaged(cls) -> "DoltConformanceSource":
        """Return the empty built-in registry; no Dolt write claim is packaged."""
        return cls()

    @classmethod
    def from_directory(cls, root: str | Path) -> "DoltConformanceSource":
        return cls(directory=Path(root))

    def validated_declarations(self) -> tuple[Mapping[str, Any], ...]:
        if self.directory is None:
            return ()
        root = self.directory
        _reject(root.is_dir(), "declaration source directory is missing.")
        _reject(
            not root.is_symlink(), "declaration source root must not be a symlink."
        )
        root = root.resolve()
        declaration_directory = _resource(
            root, "sql/dolt-adapter-declarations"
        )
        _reject(
            declaration_directory.is_dir(), "declaration directory is missing."
        )
        try:
            resources = tuple(
                sorted(
                    (
                        item
                        for item in declaration_directory.iterdir()
                        if item.suffix == ".json"
                    ),
                    key=lambda item: item.name,
                )
            )
        except OSError as error:
            raise UnsupportedOperationError(
                "Invalid adapter-owned Dolt declaration: declaration directory "
                "is unreadable."
            ) from error
        declarations: list[Mapping[str, Any]] = []
        declaration_ids: set[object] = set()
        conformance_run_ids: set[object] = set()
        for item in resources:
            _reject(
                item.is_file() and not item.is_symlink(),
                "declaration JSON is not a regular file.",
            )
            declaration = _validate_declaration(_read_json(item), root=root)
            _reject(
                declaration["declaration_id"] not in declaration_ids
                and declaration["conformance_run_id"]
                not in conformance_run_ids,
                "duplicate declaration or conformance run ID.",
            )
            declaration_ids.add(declaration["declaration_id"])
            conformance_run_ids.add(declaration["conformance_run_id"])
            declarations.append(declaration)
        return tuple(declarations)

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
            or any(
                character not in "0123456789abcdef"
                for character in specification_commit
            )
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
        matches = tuple(
            item
            for item in declarations
            if item["active_product_version"] == active_product_version
            and item["adapter_implementation_id"] == ADAPTER_IMPLEMENTATION_ID
            and item["adapter_version"] == ADAPTER_VERSION
            and item["specification_commit"] == specification_commit
        )
        if len(matches) != 1:
            raise UnsupportedOperationError(
                "No unique exact Dolt conformance declaration matches the "
                "active product, adapter, and specification binding."
            )
        return matches[0]


def effective_limits(declaration: Mapping[str, Any]) -> dict[str, Any]:
    """Project a validated adapter declaration's effective layers into limits."""

    dimensions = declaration["limit_declarations"]

    def effective(name: str) -> Mapping[str, Any]:
        matches = tuple(
            row for row in dimensions[name] if row.get("basis") == "effective"
        )
        if len(matches) != 1:
            raise UnsupportedOperationError(
                "Validated Dolt declaration has no unique effective "
                + name
                + " layer."
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
