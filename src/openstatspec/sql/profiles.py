"""Dialect capability declarations for the strict wide-table contract.

A declaration is deliberately not a claim that a live target has been tested.
Importers use this information for preflight checks before creating a dataset.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from numbers import Real
from typing import Any
from urllib.parse import urlparse

from ..core import UnsupportedOperationError


@dataclass(frozen=True)
class SqlProfile:
    name: str
    url_schemes: tuple[str, ...]
    max_source_variables: int
    identifier_limit: int
    binary64_numeric: bool
    lossless_text: bool
    max_text_value_bytes: int
    max_row_bytes: int | None
    tested_reference: bool = False
    driver_packages: tuple[str, ...] = ()
    max_statement_bytes: int | None = None

    @property
    def max_physical_variables(self) -> int:
        """Compatibility alias for the historical, source-count-named field."""
        return self.max_source_variables

    def as_dict(self) -> dict[str, object]:
        return {
            "max_source_variables": self.max_source_variables,
            "identifier_limit": self.identifier_limit,
            "binary64_numeric": self.binary64_numeric,
            "lossless_text": self.lossless_text,
            "max_text_value_bytes": self.max_text_value_bytes,
            "max_row_bytes": self.max_row_bytes,
            "max_statement_bytes": self.max_statement_bytes,
            "tested_reference": self.tested_reference,
            "driver_packages": list(self.driver_packages),
        }


SQLITE = SqlProfile(
    "sqlite", ("sqlite",), 1_999, 255, True, True,
    1_000_000_000, 1_000_000_000, True,
)
POSTGRESQL = SqlProfile(
    "postgresql", ("postgresql", "postgres"), 1_599, 63, True, True,
    1_073_741_823, 1_073_741_823, True, ("psycopg",),
)
MYSQL = SqlProfile(
    "mysql", ("mysql", "mariadb"), 1_016, 64, True, True,
    65_535, 65_535, True, ("PyMySQL",),
)
DOLT = SqlProfile(
    "dolt", (), 305, 64, True, True,
    4_294_967_295, None, False, ("PyMySQL",),
)
PROFILES = (SQLITE, POSTGRESQL, MYSQL)
MYSQL_WIRE_PROFILES = frozenset({"mysql", "mariadb", "dolt"})


def profile_for_url(database_url: str) -> SqlProfile:
    scheme = urlparse(database_url).scheme.split("+", 1)[0].lower()
    for profile in PROFILES:
        if scheme in profile.url_schemes:
            return profile
    raise UnsupportedOperationError(
        f"No OpenStatSpec SQL profile is declared for database URL scheme {scheme!r}."
    )



def validate_connection_url(database_url: str) -> SqlProfile:
    profile = profile_for_url(database_url)
    scheme = urlparse(database_url).scheme.lower()
    if profile is POSTGRESQL and scheme != "postgresql+psycopg":
        raise UnsupportedOperationError("PostgreSQL requires an explicit postgresql+psycopg URL.")
    if profile is MYSQL and scheme not in {"mysql+pymysql", "mariadb+mariadbconnector"}:
        raise UnsupportedOperationError("MySQL/MariaDB requires an explicit mysql+pymysql or mariadb+mariadbconnector URL.")
    return profile

class TargetCapabilityExceededError(UnsupportedOperationError):
    """A strict wide-table capability check failed before source state existed."""

    def __init__(self, message: str, *, details: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.details = dict(details)


def _exceeded(reason: str, message: str, **details: Any) -> TargetCapabilityExceededError:
    return TargetCapabilityExceededError(
        "Target capability exceeded: " + message,
        details={"reason": reason, **details},
    )


def preflight_identifier(
    profile: SqlProfile, identifier: str, *, role: str,
) -> None:
    """Validate one generated identifier against the effective profile."""
    identifier_bytes = len(identifier.encode("utf-8"))
    if identifier_bytes > profile.identifier_limit:
        raise _exceeded(
            "identifier_limit",
            f"{role} {identifier!r} is {identifier_bytes} bytes; "
            f"{profile.name} permits {profile.identifier_limit}.",
            identifier=identifier,
            identifier_bytes=identifier_bytes,
            maximum=profile.identifier_limit,
            role=role,
        )


def preflight(
    profile: SqlProfile,
    variables_or_count: int | Iterable[Mapping[str, Any]],
    *,
    rows: Iterable[Mapping[str, Any]] | None = None,
) -> None:
    """Validate strict target capabilities before any source dataset is created."""
    variables = None if isinstance(variables_or_count, int) else list(variables_or_count)
    variable_count = variables_or_count if isinstance(variables_or_count, int) else len(variables)
    if variable_count > profile.max_source_variables:
        raise _exceeded(
            "source_variable_limit",
            f"{profile.name} supports at most {profile.max_source_variables} "
            "source variables in one strict wide table.",
            source_count=variable_count,
            max_source=profile.max_source_variables,
            physical_count=variable_count + 1,
            max_physical=profile.max_source_variables + 1,
        )
    if variables is None:
        return

    used = {"__case_ordinal"}
    source_names: set[str] = set()
    for expected_ordinal, variable in enumerate(variables, start=1):
        source_name = variable.get("source_name")
        if not isinstance(source_name, str) or not source_name or source_name in source_names:
            raise _exceeded(
                "source_identifier_collision", "source variable names must be non-empty and unique.",
                source_name=source_name,
            )
        source_names.add(source_name)
        expected_name = _physical_name(source_name, used)
        actual_name = variable.get("physical_name")
        if variable.get("ordinal") != expected_ordinal or actual_name != expected_name:
            raise _exceeded(
                "physical_identifier_mapping_invalid",
                f"{source_name!r} must map deterministically to {expected_name!r} in source order.",
                source_name=source_name, expected_physical_name=expected_name,
                actual_physical_name=actual_name,
            )
        preflight_identifier(
            profile, expected_name, role="physical variable identifier",
        )
        if variable.get("storage_kind") == "string":
            declared_width = variable.get("string_width")
            if declared_width is not None and (
                isinstance(declared_width, bool)
                or not isinstance(declared_width, int)
                or declared_width < 0
            ):
                raise _exceeded(
                    "invalid_declared_string_width",
                    f"{source_name!r} has an invalid declared string width.",
                    source_name=source_name, string_width=declared_width,
                )
            if declared_width is not None and declared_width > profile.max_text_value_bytes:
                raise _exceeded(
                    "declared_string_width_limit",
                    f"{source_name!r} declares {declared_width} UTF-8 bytes; "
                    f"{profile.name} permits {profile.max_text_value_bytes}.",
                    source_name=source_name, string_width=declared_width,
                    maximum=profile.max_text_value_bytes,
                )
    declared_row_bytes = 8 + sum(_row_storage_bytes(profile, variable) for variable in variables)
    if profile.max_row_bytes is not None and declared_row_bytes > profile.max_row_bytes:
        raise _exceeded(
            "declared_row_size_limit",
            f"the declared SQL row requires {declared_row_bytes} bytes; "
            f"{profile.name} permits {profile.max_row_bytes}.",
            declared_row_bytes=declared_row_bytes, maximum=profile.max_row_bytes,
        )
    for row_ordinal, row in enumerate(rows or (), start=1):
        row_bytes = 8
        for variable in variables:
            if variable.get("storage_kind") == "numeric":
                physical_name = str(variable["physical_name"])
                if physical_name not in row:
                    raise _exceeded(
                        "numeric_value_missing",
                        f"row {row_ordinal} has no value for {variable['source_name']!r}.",
                        row_ordinal=row_ordinal,
                        source_name=variable["source_name"],
                    )
                value = row[physical_name]
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, Real)
                ):
                    raise _exceeded(
                        "numeric_value_type",
                        f"row {row_ordinal} value for {variable['source_name']!r} "
                        "is not a binary64 number or SQL NULL.",
                        row_ordinal=row_ordinal,
                        source_name=variable["source_name"],
                        value_type=type(value).__name__,
                    )
                if isinstance(value, int) and value is not None:
                    try:
                        converted = float(value)
                    except OverflowError as error:
                        raise _exceeded(
                            "numeric_value_not_binary64_exact",
                            f"row {row_ordinal} integer for {variable['source_name']!r} "
                            "cannot be represented exactly as binary64.",
                            row_ordinal=row_ordinal,
                            source_name=variable["source_name"],
                        ) from error
                    if not math.isfinite(converted) or int(converted) != value:
                        raise _exceeded(
                            "numeric_value_not_binary64_exact",
                            f"row {row_ordinal} integer for {variable['source_name']!r} "
                            "cannot be represented exactly as binary64.",
                            row_ordinal=row_ordinal,
                            source_name=variable["source_name"],
                        )
                if value is not None and not math.isfinite(float(value)):
                    raise _exceeded(
                        "nonfinite_numeric_value",
                        f"row {row_ordinal} value for {variable['source_name']!r} "
                        "is not finite; non-finite adapter inputs are rejected.",
                        row_ordinal=row_ordinal,
                        source_name=variable["source_name"],
                    )
                row_bytes += 8
                continue
            physical_name = str(variable["physical_name"])
            if physical_name not in row:
                raise _exceeded(
                    "string_value_missing",
                    f"row {row_ordinal} has no value for {variable['source_name']!r}.",
                    row_ordinal=row_ordinal,
                    source_name=variable["source_name"],
                )
            value = row[physical_name]
            if not isinstance(value, str):
                raise _exceeded(
                    "string_value_type",
                    f"row {row_ordinal} value for {variable['source_name']!r} "
                    "is not a string; SPSS string missing values must be empty strings.",
                    row_ordinal=row_ordinal,
                    source_name=variable["source_name"],
                    value_type=type(value).__name__,
                )
            encoded_bytes = len(value.encode("utf-8"))
            if encoded_bytes > profile.max_text_value_bytes:
                raise _exceeded(
                    "text_value_limit",
                    f"row {row_ordinal} value for {variable['source_name']!r} is "
                    f"{encoded_bytes} UTF-8 bytes; {profile.name} permits "
                    f"{profile.max_text_value_bytes}.",
                    row_ordinal=row_ordinal, source_name=variable["source_name"],
                    encoded_bytes=encoded_bytes, maximum=profile.max_text_value_bytes,
                )
            row_bytes += 20 if profile.name in MYSQL_WIRE_PROFILES else encoded_bytes
        if profile.max_row_bytes is not None and row_bytes > profile.max_row_bytes:
            raise _exceeded(
                "row_size_limit",
                f"row {row_ordinal} requires {row_bytes} bytes; "
                f"{profile.name} permits {profile.max_row_bytes}.",
                row_ordinal=row_ordinal, row_bytes=row_bytes,
                maximum=profile.max_row_bytes,
            )
        statement_bytes = statement_payload_bytes(row, variables)
        if profile.max_statement_bytes is not None and statement_bytes > profile.max_statement_bytes:
            raise _exceeded(
                "statement_payload_limit",
                f"row {row_ordinal} requires {statement_bytes} payload bytes; "
                f"{profile.name} permits {profile.max_statement_bytes} per bounded statement.",
                row_ordinal=row_ordinal, statement_bytes=statement_bytes,
                maximum=profile.max_statement_bytes,
            )


def _row_storage_bytes(profile: SqlProfile, variable: Mapping[str, Any]) -> int:
    if variable.get("storage_kind") == "numeric":
        return 8
    if profile.name in MYSQL_WIRE_PROFILES:
        return 20
    return int(variable.get("string_width") or 0)


def statement_payload_bytes(
    row: Mapping[str, Any], variables: Iterable[Mapping[str, Any]],
) -> int:
    """Count raw payload bytes for packet-safe bounded insert batches."""
    size = 32
    for variable in variables:
        if variable.get("storage_kind") == "numeric":
            # PyMySQL serializes binary64 parameters as decimal wire text;
            # the longest finite literal is 24 bytes (including its sign).
            size += 24
        else:
            value = row.get(str(variable["physical_name"]), "")
            size += len(str(value).encode("utf-8")) + 8
    return size



def _physical_name(source_name: str, used: set[str]) -> str:
    """The profile-independent deterministic OpenStatSpec SQL-name mapping."""
    import re

    stem = re.sub(r"[^a-zA-Z0-9_]+", "_", source_name).strip("_").lower() or "variable"
    stem = stem[:54]
    candidate, suffix = stem, 2
    while candidate.lower() in used or candidate.startswith("__"):
        candidate = f"{stem[:50]}_{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate
