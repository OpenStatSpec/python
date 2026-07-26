"""Dialect capability declarations for the strict wide-table contract.

A declaration is deliberately not a claim that a live target has been tested.
Importers use this information for preflight checks before creating a dataset.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from ..core import UnsupportedOperationError


@dataclass(frozen=True)
class SqlProfile:
    name: str
    url_schemes: tuple[str, ...]
    max_physical_variables: int
    identifier_limit: int
    binary64_numeric: bool
    lossless_text: bool
    max_text_value_bytes: int
    tested_reference: bool = False
    driver_packages: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "max_physical_variables": self.max_physical_variables,
            "identifier_limit": self.identifier_limit,
            "binary64_numeric": self.binary64_numeric,
            "lossless_text": self.lossless_text,
            "max_text_value_bytes": self.max_text_value_bytes,
            "tested_reference": self.tested_reference,
            "driver_packages": list(self.driver_packages),
        }


SQLITE = SqlProfile(
    "sqlite", ("sqlite",), 1_999, 255, True, True,
    1_000_000_000, True,
)
POSTGRESQL = SqlProfile(
    "postgresql", ("postgresql", "postgres"), 1_599, 63, True, True,
    1_073_741_824, True, ("psycopg",),
)
# SQLAlchemy's generic Text column compiles to MySQL TEXT, not MEDIUMTEXT.
# The contract therefore declares TEXT's 64 KiB payload limit rather than
# promising an unimplemented wider physical type.
MYSQL = SqlProfile(
    "mysql", ("mysql", "mariadb"), 1_016, 64, True, True,
    65_535, True, ("PyMySQL or mariadb",),
)
PROFILES = (SQLITE, POSTGRESQL, MYSQL)


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


def preflight(profile: SqlProfile, variables_or_count: int | Iterable[Mapping[str, Any]]) -> None:
    """Validate strict target capabilities before any source dataset is created."""
    variables = None if isinstance(variables_or_count, int) else list(variables_or_count)
    variable_count = variables_or_count if isinstance(variables_or_count, int) else len(variables)
    if variable_count > profile.max_physical_variables:
        raise _exceeded(
            "physical_variable_limit",
            f"{profile.name} supports at most {profile.max_physical_variables} "
            "source variables in one strict wide table.",
            variable_count=variable_count, maximum=profile.max_physical_variables,
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
        identifier_bytes = len(expected_name.encode("utf-8"))
        if identifier_bytes > profile.identifier_limit:
            raise _exceeded(
                "identifier_limit",
                f"identifier {expected_name!r} is {identifier_bytes} bytes; "
                f"{profile.name} permits {profile.identifier_limit}.",
                identifier=expected_name, identifier_bytes=identifier_bytes,
                maximum=profile.identifier_limit,
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
