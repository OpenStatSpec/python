"""Machine-readable SQL and specification capability declarations."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import make_url

from .normative import catalog
from .profiles import MYSQL, POSTGRESQL, SQLITE, SqlProfile
from .profiles import profile_for_url
from ..core import UnsupportedOperationError

SPECIFICATION_COMMIT = "6b9d1fc38f2f083c0ac5cf1c64874a6d07b95045"
SPECIFICATION_RELEASE: str | None = None

SERVER_POLICIES = {
    "sqlite": {
        "claimed": ["SQLite >=3.24.0 <4.0.0"],
        "ci": ["active GitHub runner SQLite version"],
    },
    "mysql": {
        "claimed": ["MySQL 8.4.x", "MySQL 9.7.x"],
        "ci": ["MySQL 8.4.x", "MySQL 9.7.x"],
    },
    "mariadb": {
        "claimed": ["MariaDB 11.4.x", "MariaDB 11.8.x", "MariaDB 12.3.x"],
        "ci": ["MariaDB 11.4.x", "MariaDB 11.8.x", "MariaDB 12.3.x"],
    },
    "postgresql": {
        "claimed": ["PostgreSQL 17.x", "PostgreSQL 18.x"],
        "ci": ["PostgreSQL 17.x", "PostgreSQL 18.x"],
    },
}


def profile_declarations(database_url: str | None = None) -> dict[str, dict[str, Any]]:
    """Declare every profile and, optionally, one active connection."""
    active = active_connection(database_url) if database_url else None
    return {
        "sqlite": _profile("sqlite", SQLITE, active),
        "mysql": _profile("mysql", MYSQL, active),
        "mariadb": _profile("mariadb", MYSQL, active),
        "postgresql": _profile("postgresql", POSTGRESQL, active),
    }


def active_connection(database_url: str) -> dict[str, Any]:
    engine = create_engine(database_url)
    configured = make_url(database_url)
    with engine.connect() as connection:
        dialect = connection.dialect.name
        if dialect == "sqlite":
            profile_name = "sqlite"
            raw_version = str(connection.execute(text("select sqlite_version()")).scalar_one())
            identity_source = "select sqlite_version()"
            compile_options = {
                name: int(value)
                for option in connection.exec_driver_sql("pragma compile_options").scalars()
                if "=" in str(option)
                for name, value in [str(option).split("=", 1)]
                if value.isdigit()
            }
            observed = {"compile_options": compile_options}
        elif dialect == "postgresql":
            profile_name = "postgresql"
            raw_version = str(connection.execute(text("show server_version")).scalar_one())
            identity_source = "SHOW server_version"
            observed = {}
        elif dialect in {"mysql", "mariadb"}:
            raw_version = str(connection.execute(text("select @@version")).scalar_one())
            comment = str(connection.execute(text("select @@version_comment")).scalar_one())
            profile_name = "mariadb" if "mariadb" in (raw_version + " " + comment).lower() else "mysql"
            identity_source = "SELECT @@version, @@version_comment"
            observed = {
                "max_allowed_packet": int(
                    connection.execute(text("select @@max_allowed_packet")).scalar_one()
                )
            }
        else:  # pragma: no cover - validate_connection_url rejects this first
            raise ValueError(f"Unsupported active SQL dialect {dialect!r}.")
    return {
        "profile": profile_name,
        "server_version": _normalized_version(raw_version),
        "raw_server_version": raw_version,
        "identity_source": identity_source,
        "claimed_supported": server_version_supported(profile_name, raw_version),
        "matched_claim": _matched_claim(profile_name, raw_version),
        "catalog_binding": catalog_binding(database_url),
        "observed": observed,
    }


def effective_profile(database_url: str) -> tuple[SqlProfile, dict[str, Any]]:
    """Resolve and enforce the profile used by import preflight."""
    configured = profile_for_url(database_url)
    active = active_connection(database_url)
    if configured is not MYSQL and active["profile"] != configured.name:
        raise UnsupportedOperationError("The active SQL server does not match the configured profile.")
    if configured is MYSQL and active["profile"] not in {"mysql", "mariadb"}:
        raise UnsupportedOperationError("The active SQL server is not MySQL or MariaDB.")
    if not active["claimed_supported"]:
        raise UnsupportedOperationError(
            f"Active {active['profile']} server version {active['server_version']} is not claimed supported."
        )
    declaration = _profile(active["profile"], configured, active)
    limits = declaration["effective_limits"]
    assert limits is not None
    return replace(
        configured,
        name=active["profile"],
        max_physical_variables=int(limits["maximum_source_variables"]),
        max_text_value_bytes=int(limits["maximum_value_bytes"]),
        max_row_bytes=int(limits["maximum_row_bytes"]),
    ), active


def catalog_binding(database_url: str) -> dict[str, Any]:
    url = make_url(database_url)
    database = url.database or ""
    if url.get_backend_name() == "sqlite":
        namespace = str(Path(database).resolve()) if database and database != ":memory:" else database
        mode = "dedicated_database_file"
    else:
        namespace = database
        mode = "dedicated_database"
    logical = catalog(MetaData())
    return {
        "mode": mode,
        "namespace": namespace,
        "identity_marker": "catalog_identity",
        "contract_id": "openstatspec-strict-wide-table-v1",
        "schema_version": 1,
        "required_isolation": "exclusive OpenStatSpec catalog connection",
        "logical_to_physical_relations": {
            table.name: table.name for table in logical.all()
        },
    }


def _profile(
    name: str, profile: SqlProfile, active: dict[str, Any] | None,
) -> dict[str, Any]:
    identifier = {
        "value": profile.identifier_limit,
        "unit": "characters" if name in {"mysql", "mariadb"} else "bytes",
        "source": (
            "MySQL/MariaDB native identifier limit" if name in {"mysql", "mariadb"}
            else "PostgreSQL NAMEDATALEN minus one native byte limit" if name == "postgresql"
            else "OpenStatSpec profile boundary; SQLite has no fixed native identifier limit"
        ),
        "repertoire": "generated ASCII [a-z0-9_] identifiers",
    }
    theoretical = {
        "maximum_physical_columns": profile.max_physical_variables + 1,
        "maximum_source_variables": profile.max_physical_variables,
        "identifier_limit": identifier,
        "maximum_value_bytes": profile.max_text_value_bytes,
        "maximum_row_bytes": profile.max_row_bytes,
    }
    effective = None
    status = "not_connected"
    if active and active["profile"] == name:
        effective = dict(theoretical)
        sources = {
            "maximum_source_variables": "profile theoretical engine ceiling",
            "maximum_physical_columns": "profile theoretical engine ceiling",
            "identifier_limit": identifier["source"],
            "maximum_value_bytes": "profile theoretical engine ceiling",
            "maximum_row_bytes": "profile theoretical engine ceiling",
        }
        observed = active["observed"]
        if name == "sqlite":
            options = observed["compile_options"]
            if "MAX_COLUMN" in options:
                effective["maximum_physical_columns"] = min(
                    theoretical["maximum_physical_columns"], options["MAX_COLUMN"],
                )
                effective["maximum_source_variables"] = max(
                    0, effective["maximum_physical_columns"] - 1,
                )
                sources["maximum_physical_columns"] = "active PRAGMA compile_options MAX_COLUMN"
                sources["maximum_source_variables"] = "active MAX_COLUMN minus technical ordinal"
            if "MAX_LENGTH" in options:
                effective["maximum_value_bytes"] = min(
                    theoretical["maximum_value_bytes"], options["MAX_LENGTH"],
                )
                effective["maximum_row_bytes"] = min(
                    theoretical["maximum_row_bytes"], options["MAX_LENGTH"],
                )
                sources["maximum_value_bytes"] = "active PRAGMA compile_options MAX_LENGTH"
                sources["maximum_row_bytes"] = "active PRAGMA compile_options MAX_LENGTH"
            status = "active_connection_mixed"
        elif name in {"mysql", "mariadb"}:
            packet = int(observed["max_allowed_packet"])
            payload = max(0, (packet - 131_072) // 2)
            effective["maximum_value_bytes"] = min(
                theoretical["maximum_value_bytes"], payload,
            )
            effective["maximum_statement_bytes"] = payload
            sources["maximum_value_bytes"] = "active @@max_allowed_packet worst-case payload"
            sources["maximum_statement_bytes"] = "active @@max_allowed_packet worst-case payload"
            status = "active_connection_mixed"
        else:
            status = "profile_theoretical_fallback"
        effective["sources"] = sources
    policy = SERVER_POLICIES[name]
    return {
        "driver": "psycopg" if name == "postgresql" else "PyMySQL" if name in {"mysql", "mariadb"} else "sqlite3",
        "claimed_server_versions": policy["claimed"],
        "ci_tested_server_versions": policy["ci"],
        "theoretical_limits": theoretical,
        "effective_limits": effective,
        "effective_limits_status": status,
        "numeric_type": "DOUBLE PRECISION" if name == "postgresql" else "DOUBLE" if name in {"mysql", "mariadb"} else "REAL",
        "text_type": "LONGTEXT" if name in {"mysql", "mariadb"} else "TEXT",
        "ddl_atomic": name not in {"mysql", "mariadb"},
        "failure_cleanup": "compensating_cleanup" if name in {"mysql", "mariadb"} else "transaction_rollback",
        "physical_table_mapping": "dataset.physical_table_schema + dataset.physical_table_name",
        "identifier_policy": "deterministic ASCII mapping; source name remains authoritative",
    }


def server_version_supported(profile: str, raw_version: str) -> bool:
    version = _version_tuple(raw_version)
    if profile == "sqlite":
        return (3, 24) <= version[:2] < (4, 0)
    allowed = {
        "postgresql": {(17,), (18,)},
        "mysql": {(8, 4), (9, 7)},
        "mariadb": {(11, 4), (11, 8), (12, 3)},
    }[profile]
    width = len(next(iter(allowed)))
    return version[:width] in allowed


def _matched_claim(profile: str, raw_version: str) -> str | None:
    if not server_version_supported(profile, raw_version):
        return None
    version = _version_tuple(raw_version)
    for claim in SERVER_POLICIES[profile]["claimed"]:
        numbers = _version_tuple(claim)
        if profile == "sqlite" or version[:2] == numbers[:2] or version[:1] == numbers[:1]:
            return claim
    return SERVER_POLICIES[profile]["claimed"][0]


def _version_tuple(raw_version: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", raw_version)
    if not match:
        return ()
    return tuple(int(part) for part in match.groups(default="0"))


def _normalized_version(raw_version: str) -> str:
    return ".".join(str(part) for part in _version_tuple(raw_version))
