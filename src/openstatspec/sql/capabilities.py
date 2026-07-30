"""Machine-readable SQL and specification capability declarations."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import make_url

from .normative import catalog
from .profiles import DOLT, MYSQL, POSTGRESQL, SQLITE, SqlProfile
from .profiles import profile_for_url, validate_connection_url
from ..core import UnsupportedOperationError

SPECIFICATION_COMMIT = "34141dda023d9e0217c37c232e39f436edfb0746"
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
    "dolt": {
        "claimed": ["Dolt 2.2.2"],
        "ci": ["Dolt 2.2.2"],
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
        "dolt": _profile("dolt", DOLT, active),
        "postgresql": _profile("postgresql", POSTGRESQL, active),
    }


def _required_text_probe(connection: Any, statement: str, label: str) -> str:
    """Return one required identity value without normalizing absence into text."""
    try:
        value = connection.execute(text(statement)).scalar_one()
    except Exception as error:
        raise UnsupportedOperationError(
            f"Active SQL server identity probe {label} failed."
        ) from error
    if value is None or value is False:
        raise UnsupportedOperationError(
            f"Active SQL server identity probe {label} returned no value."
        )
    raw = str(value)
    if not raw.strip():
        raise UnsupportedOperationError(
            f"Active SQL server identity probe {label} returned no value."
        )
    return raw


def active_connection(database_url: str) -> dict[str, Any]:
    engine = create_engine(database_url)
    with engine.connect() as connection:
        dialect = connection.dialect.name
        raw_comment: str | None = None
        if dialect == "sqlite":
            profile_name = "sqlite"
            raw_wire_version = _required_text_probe(
                connection, "select sqlite_version()", "sqlite_version()",
            )
            raw_product_version = raw_wire_version
            identity_source = "SELECT sqlite_version()"
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
            raw_wire_version = _required_text_probe(
                connection, "show server_version", "server_version",
            )
            raw_product_version = raw_wire_version
            identity_source = "SHOW server_version"
            observed = {}
        elif dialect in {"mysql", "mariadb"}:
            raw_wire_version = _required_text_probe(
                connection, "select @@version", "@@version",
            )
            raw_comment = _required_text_probe(
                connection, "select @@version_comment", "@@version_comment",
            )
            identity_text = f"{raw_wire_version} {raw_comment}".casefold()
            if raw_comment.strip().casefold() == "dolt":
                profile_name = "dolt"
                raw_product_version = _required_text_probe(
                    connection, "select DOLT_VERSION()", "DOLT_VERSION()",
                )
                if raw_product_version.strip() != "2.2.2":
                    raise UnsupportedOperationError(
                        "The active Dolt product version must be exactly 2.2.2."
                    )
                identity_source = "SELECT @@version, @@version_comment, DOLT_VERSION()"
            elif "mariadb" in identity_text:
                profile_name = "mariadb"
                raw_product_version = raw_wire_version
                identity_source = "SELECT @@version, @@version_comment"
            elif "mysql" in raw_comment.casefold():
                profile_name = "mysql"
                raw_product_version = raw_wire_version
                identity_source = "SELECT @@version, @@version_comment"
            else:
                raise UnsupportedOperationError(
                    "The active MySQL-wire server product is unknown or unsupported."
                )
            packet_text = _required_text_probe(
                connection, "select @@max_allowed_packet", "@@max_allowed_packet",
            )
            try:
                packet = int(packet_text)
            except ValueError as error:
                raise UnsupportedOperationError(
                    "Active SQL server returned an invalid @@max_allowed_packet."
                ) from error
            if packet <= 0:
                raise UnsupportedOperationError(
                    "Active SQL server returned an invalid @@max_allowed_packet."
                )
            observed = {"max_allowed_packet": packet}
        else:  # pragma: no cover - validate_connection_url rejects this first
            raise ValueError(f"Unsupported active SQL dialect {dialect!r}.")
    return {
        "dialect": dialect,
        "profile": profile_name,
        "engine": profile_name,
        "product": profile_name,
        "transport": "mysql" if dialect in {"mysql", "mariadb"} else dialect,
        "driver": engine.dialect.driver,
        "server_version": _normalized_version(raw_product_version),
        "raw_server_version": raw_product_version,
        "raw_wire_version": raw_wire_version,
        "raw_product_version": raw_product_version,
        "raw_version_comment": raw_comment,
        "identity_source": identity_source,
        "claimed_supported": server_version_supported(profile_name, raw_product_version),
        "matched_claim": _matched_claim(profile_name, raw_product_version),
        "catalog_binding": catalog_binding(database_url),
        "observed": observed,
    }


def effective_profile(database_url: str) -> tuple[SqlProfile, dict[str, Any]]:
    """Resolve and enforce the profile used by import preflight."""
    configured = validate_connection_url(database_url)
    active = active_connection(database_url)
    if configured is not MYSQL and active["profile"] != configured.name:
        raise UnsupportedOperationError("The active SQL server does not match the configured profile.")
    if configured is MYSQL and active["profile"] not in {"mysql", "mariadb", "dolt"}:
        raise UnsupportedOperationError("The active SQL server is not MySQL, MariaDB, or Dolt.")
    if active["profile"] == "dolt" and make_url(database_url).drivername != "mysql+pymysql":
        raise UnsupportedOperationError("Dolt requires an explicit mysql+pymysql URL.")
    if not active["claimed_supported"]:
        raise UnsupportedOperationError(
            f"Active {active['profile']} server version {active['server_version']} is not claimed supported."
        )
    selected = DOLT if active["profile"] == "dolt" else configured
    declaration = _profile(active["profile"], selected, active)
    limits = declaration["effective_limits"]
    assert limits is not None
    return replace(
        selected,
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
            else "Dolt 2.2.2 observed 64-byte ASCII identifier limit" if name == "dolt"
            else "PostgreSQL NAMEDATALEN minus one native byte limit" if name == "postgresql"
            else "OpenStatSpec profile boundary; SQLite has no fixed native identifier limit"
        ),
        "repertoire": "generated ASCII [a-z0-9_] identifiers",
    }
    declared = {
        "maximum_physical_columns": profile.max_physical_variables + 1,
        "maximum_source_variables": profile.max_physical_variables,
        "identifier_limit": identifier,
        "maximum_value_bytes": profile.max_text_value_bytes,
        "maximum_row_bytes": profile.max_row_bytes,
    }
    theoretical = (
        {"maximum_value_bytes": 4_294_967_295}
        if name == "dolt" else declared
    )
    proposed = (
        {
            "maximum_physical_columns": declared["maximum_physical_columns"],
            "maximum_source_variables": declared["maximum_source_variables"],
            "maximum_value_bytes": declared["maximum_value_bytes"],
            "maximum_row_bytes": declared["maximum_row_bytes"],
        }
        if name == "dolt" else None
    )
    observed_limits = (
        {
            "minimum_observed_physical_columns": 307,
            "identifier_limit": identifier,
            "rejected_identifier_bytes": 65,
        }
        if name == "dolt" else None
    )
    effective = None
    status = "not_connected"
    if active and active["profile"] == name:
        effective = dict(declared)
        sources = (
            {
                "maximum_source_variables": "proposed Dolt adapter envelope",
                "maximum_physical_columns": "proposed Dolt adapter envelope",
                "identifier_limit": "observed on exact Dolt 2.2.2",
                "maximum_value_bytes": "observed Dolt adapter value envelope",
                "maximum_row_bytes": "proposed Dolt adapter envelope",
            }
            if name == "dolt"
            else {
                "maximum_source_variables": "profile theoretical engine ceiling",
                "maximum_physical_columns": "profile theoretical engine ceiling",
                "identifier_limit": identifier["source"],
                "maximum_value_bytes": "profile theoretical engine ceiling",
                "maximum_row_bytes": "profile theoretical engine ceiling",
            }
        )
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
        elif name in {"mysql", "mariadb", "dolt"}:
            packet = int(observed["max_allowed_packet"])
            payload = max(0, (packet - 131_072) // 2)
            effective["maximum_value_bytes"] = min(
                (
                    declared["maximum_value_bytes"]
                    if name == "dolt" else theoretical["maximum_value_bytes"]
                ),
                payload,
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
        "profile": name,
        "engine": name,
        "dialect": "mysql" if name == "dolt" else name,
        "transport": "mysql" if name == "dolt" else name,
        "specification_commit": SPECIFICATION_COMMIT,
        "specification_status": "release_candidate",
        "specification_release": SPECIFICATION_RELEASE,
        "driver": "psycopg" if name == "postgresql" else "PyMySQL" if name in {"mysql", "mariadb", "dolt"} else "sqlite3",
        "claimed_server_versions": policy["claimed"],
        "ci_tested_server_versions": policy["ci"],
        "theoretical_limits": theoretical,
        "proposed_adapter_limits": proposed,
        "observed_limits": observed_limits,
        "effective_limits": effective,
        "effective_limits_status": status,
        "numeric_type": "DOUBLE PRECISION" if name == "postgresql" else "DOUBLE" if name in {"mysql", "mariadb", "dolt"} else "REAL",
        "numeric_value_policy": {
            "finite_binary64": "supported",
            "nan": "rejected_before_ddl",
            "positive_infinity": "rejected_before_ddl",
            "negative_infinity": "rejected_before_ddl",
        },
        "text_type": "LONGTEXT" if name in {"mysql", "mariadb", "dolt"} else "TEXT",
        "ddl_atomic": name not in {"mysql", "mariadb", "dolt"},
        "failure_cleanup": "compensating_cleanup" if name in {"mysql", "mariadb", "dolt"} else "transaction_rollback",
        "limit_bases": {
            "maximum_physical_columns": (
                "proposed_adapter_envelope" if name == "dolt" else "theoretical_engine_limit"
            ),
            "maximum_source_variables": (
                "proposed_adapter_envelope" if name == "dolt" else "theoretical_engine_limit"
            ),
            "identifier_limit": "observed_exact_version" if name == "dolt" else "theoretical_engine_limit",
            "maximum_value_bytes": (
                "observed_exact_version" if name == "dolt" else "theoretical_engine_limit"
            ),
            "maximum_row_bytes": (
                "proposed_adapter_envelope" if name == "dolt" else "theoretical_engine_limit"
            ),
            "maximum_statement_bytes": "active_connection_observation",
        },
        "storage_evidence": (
            {
                "binary64": {
                    "type": "DOUBLE",
                    "classification": "observed_exact_version",
                    "source": "Dolt 2.2.2 interoperability verification",
                    "version": "2.2.2",
                    "maximum_finite_round_trip_exact": True,
                },
                "text": {
                    "type": "LONGTEXT NOT NULL",
                    "classification": "observed_exact_version",
                    "source": "Dolt 2.2.2 interoperability verification",
                    "version": "2.2.2",
                    "observed_value_bytes": 65_504,
                    "unit": "bytes",
                },
            }
            if name == "dolt" else None
        ),
        "transformation_workflow": "unsupported" if name == "dolt" else None,
        "physical_table_mapping": "dataset.physical_table_schema + dataset.physical_table_name",
        "identifier_policy": "deterministic ASCII mapping; source name remains authoritative",
    }


def server_version_supported(profile: str, raw_version: str) -> bool:
    if profile == "dolt":
        return raw_version.strip() == "2.2.2"
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
