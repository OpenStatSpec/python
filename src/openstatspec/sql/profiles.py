"""Dialect capability declarations for the strict wide-table contract.

A declaration is deliberately not a claim that a live target has been tested.
Importers use this information for preflight checks before creating a dataset.
"""

from dataclasses import dataclass
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
    tested_reference: bool = False
    driver_packages: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "max_physical_variables": self.max_physical_variables,
            "identifier_limit": self.identifier_limit,
            "binary64_numeric": self.binary64_numeric,
            "lossless_text": self.lossless_text,
            "tested_reference": self.tested_reference,
            "driver_packages": list(self.driver_packages),
        }


SQLITE = SqlProfile("sqlite", ("sqlite",), 1_999, 255, True, True, True)
POSTGRESQL = SqlProfile("postgresql", ("postgresql", "postgres"), 1_599, 63, True, True, False, ("psycopg",))
MYSQL = SqlProfile("mysql", ("mysql", "mariadb"), 1_016, 64, True, True, False, ("PyMySQL or mariadb",))
PROFILES = (SQLITE, POSTGRESQL, MYSQL)


def profile_for_url(database_url: str) -> SqlProfile:
    scheme = urlparse(database_url).scheme.split("+", 1)[0].lower()
    for profile in PROFILES:
        if scheme in profile.url_schemes:
            return profile
    raise UnsupportedOperationError(
        f"No OpenStatSpec SQL profile is declared for database URL scheme {scheme!r}."
    )


def preflight(profile: SqlProfile, variable_count: int) -> None:
    if variable_count > profile.max_physical_variables:
        raise UnsupportedOperationError(
            f"Target capability exceeded: {profile.name} supports at most "
            f"{profile.max_physical_variables} source variables in one strict wide table."
        )