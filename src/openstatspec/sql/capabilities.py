"""Machine-readable SQL and specification capability declarations."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import make_url

from .dolt_conformance import DoltConformanceSource, effective_limits as dolt_effective_limits
from .normative import catalog
from .profiles import DOLT, MYSQL, POSTGRESQL, SQLITE, MYSQL_WIRE_PROFILES, SqlProfile
from .profiles import profile_for_url
from ..core import UnsupportedOperationError

# Release/build automation must bind this to the exact commit used to build
# openstatspec-specification. An uncommitted source tree has no truthful pin.
SPECIFICATION_COMMIT: str | None = None
SPECIFICATION_RELEASE: str | None = None

DOLT_WRITE_CONFORMANCE = {
    "declaration_schema_id": "openstatspec-dolt-adapter-declaration-v1",
    "write_enabled": False,
    "status": "packaged_concrete_declarations_required",
}


def _conformance_source(
    source: DoltConformanceSource | None,
) -> DoltConformanceSource:
    return source or DoltConformanceSource.packaged()


def _validated_dolt_declarations(
    source: DoltConformanceSource | None,
) -> tuple[dict[str, Any], ...]:
    try:
        return tuple(
            dict(item)
            for item in _conformance_source(source).validated_declarations()
        )
    except UnsupportedOperationError:
        return ()


def _bound_specification_commit() -> str:
    if (
        not isinstance(SPECIFICATION_COMMIT, str)
        or re.fullmatch(r"[0-9a-f]{40}", SPECIFICATION_COMMIT) is None
    ):
        raise UnsupportedOperationError(
            "The Python adapter is not bound to an exact "
            "openstatspec-specification commit; Dolt write rejected before "
            "mutation."
        )
    return SPECIFICATION_COMMIT


def _dolt_write_enabled(
    source: DoltConformanceSource | None = None,
    *,
    active_product_version: str | None = None,
) -> bool:
    conformance = _conformance_source(source)
    if active_product_version is None:
        return bool(conformance.status()["write_enabled"])
    conformance.require_exact_match(
        active_product_version=active_product_version,
        specification_commit=_bound_specification_commit(),
    )
    return True


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
    "dolt": {
        "claimed": [],
        "ci": [],
    },
    "postgresql": {
        "claimed": ["PostgreSQL 17.x", "PostgreSQL 18.x"],
        "ci": ["PostgreSQL 17.x", "PostgreSQL 18.x"],
    },
}


def profile_declarations(
    database_url: str | None = None,
    *,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, dict[str, Any]]:
    """Declare every profile and, optionally, one active connection."""
    source = _conformance_source(dolt_conformance_source)
    active = (
        active_connection(database_url, dolt_conformance_source=source)
        if database_url else None
    )
    dolt_declaration = None
    if active and active["profile"] == "dolt":
        dolt_declaration = source.require_exact_match(
            active_product_version=active["raw_product_version"],
            specification_commit=_bound_specification_commit(),
        )
    return {
        "sqlite": _profile("sqlite", SQLITE, active, source),
        "mysql": _profile("mysql", MYSQL, active, source),
        "mariadb": _profile("mariadb", MYSQL, active, source),
        "dolt": _profile("dolt", DOLT, active, source, dolt_declaration),
        "postgresql": _profile("postgresql", POSTGRESQL, active, source),
    }


def active_connection(
    database_url: str,
    *,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    engine = create_engine(database_url)
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
            wire_version = _required_identity_text(
                connection.execute(text("select @@version")).scalar_one(), "@@version",
            )
            comment = _required_identity_text(
                connection.execute(text("select @@version_comment")).scalar_one(),
                "@@version_comment",
            )
            normalized_comment = comment.strip().casefold()
            product_version = wire_version
            if normalized_comment == "dolt":
                if "mariadb" in wire_version.casefold():
                    raise UnsupportedOperationError(
                        "Conflicting Dolt and MariaDB active-server identity."
                    )
                product_version = _required_identity_text(
                    connection.execute(text("select DOLT_VERSION()")).scalar_one(),
                    "DOLT_VERSION()",
                )
                profile_name, product = "dolt", "Dolt"
                active_branch = _required_identity_text(
                    connection.execute(text("select ACTIVE_BRANCH()")).scalar_one(),
                    "ACTIVE_BRANCH()",
                )
                identity_source = (
                    "SELECT @@version, @@version_comment, DOLT_VERSION(), ACTIVE_BRANCH()"
                )
            elif "mariadb" in (wire_version + " " + comment).casefold():
                profile_name, product = "mariadb", "MariaDB"
                identity_source = "SELECT @@version, @@version_comment"
            else:
                profile_name, product = "mysql", "MySQL"
                identity_source = "SELECT @@version, @@version_comment"
            raw_version = product_version
            packet = int(connection.execute(text("select @@max_allowed_packet")).scalar_one())
            observed = {
                "max_allowed_packet": packet,
                **(
                    {"active_branch": active_branch}
                    if profile_name == "dolt" else {}
                ),
            }
        else:  # pragma: no cover - validate_connection_url rejects this first
            raise ValueError(f"Unsupported active SQL dialect {dialect!r}.")
    return {
        "dialect": dialect,
        "profile": profile_name,
        "product": product if dialect in {"mysql", "mariadb"} else profile_name,
        "server_version": _normalized_version(raw_version),
        "raw_server_version": raw_version,
        "raw_wire_version": wire_version if dialect in {"mysql", "mariadb"} else raw_version,
        "raw_product_version": raw_version,
        "raw_version_comment": comment if dialect in {"mysql", "mariadb"} else None,
        "identity_source": identity_source,
        "claimed_supported": server_version_supported(
            profile_name,
            raw_version,
            dolt_conformance_source=dolt_conformance_source,
        ),
        "matched_claim": _matched_claim(
            profile_name,
            raw_version,
            dolt_conformance_source=dolt_conformance_source,
        ),
        "catalog_binding": catalog_binding(database_url),
        "working_set_binding": (
            {
                "database": catalog_binding(database_url)["namespace"],
                "active_branch": active_branch,
            }
            if profile_name == "dolt" else None
        ),
        "observed": observed,
    }


def _required_identity_text(value: Any, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UnsupportedOperationError(
            f"Active-server identity probe {source} returned no non-empty text."
        )
    return value.strip()


def effective_profile(
    database_url: str,
    *,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> tuple[SqlProfile, dict[str, Any]]:
    """Resolve and enforce the profile used by import preflight."""
    source = _conformance_source(dolt_conformance_source)
    configured = profile_for_url(database_url)
    active = active_connection(database_url, dolt_conformance_source=source)
    if configured is not MYSQL and active["profile"] != configured.name:
        raise UnsupportedOperationError("The active SQL server does not match the configured profile.")
    if configured is MYSQL and active["profile"] not in MYSQL_WIRE_PROFILES:
        raise UnsupportedOperationError("The active SQL server is not a claimed MySQL-wire product.")
    dolt_declaration = None
    if active["profile"] == "dolt":
        dolt_declaration = source.require_exact_match(
            active_product_version=active["raw_product_version"],
            specification_commit=_bound_specification_commit(),
        )
    if not active["claimed_supported"]:
        raise UnsupportedOperationError(
            f"Active {active['profile']} server version {active['server_version']} is not claimed supported."
        )
    if (
        active["profile"] in MYSQL_WIRE_PROFILES
        and int(active["observed"]["max_allowed_packet"]) <= 131_072
    ):
        raise UnsupportedOperationError(
            "Active @@max_allowed_packet is too small for the SQL adapter safety reserve."
        )
    configured = DOLT if active["profile"] == "dolt" else configured
    declaration = _profile(
        active["profile"], configured, active, source, dolt_declaration,
    )
    limits = declaration["effective_limits"]
    assert limits is not None
    return replace(
        configured,
        name=active["profile"],
        max_source_variables=int(limits["maximum_source_variables"]),
        max_text_value_bytes=int(limits["maximum_value_bytes"]),
        max_row_bytes=(
            int(limits["maximum_row_bytes"]) if limits["maximum_row_bytes"] is not None else None
        ),
        max_statement_bytes=limits.get("maximum_statement_bytes"),
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
    name: str,
    profile: SqlProfile,
    active: dict[str, Any] | None,
    dolt_conformance_source: DoltConformanceSource | None = None,
    dolt_declaration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    dolt_envelope = name == "dolt"
    identifier = {
        "value": profile.identifier_limit,
        "unit": "characters" if name in {"mysql", "mariadb"} else "bytes",
        "source": (
            "MySQL/MariaDB native identifier limit" if name in {"mysql", "mariadb"}
            else "PostgreSQL NAMEDATALEN minus one native byte limit" if name == "postgresql"
            else "OpenStatSpec Dolt adapter envelope pending pinned live boundary evidence"
            if dolt_envelope
            else "OpenStatSpec profile boundary; SQLite has no fixed native identifier limit"
        ),
        "repertoire": "generated ASCII [a-z0-9_] identifiers",
    }
    profile_limits = {
        "maximum_physical_columns": profile.max_source_variables + 1,
        "maximum_source_variables": profile.max_source_variables,
        "maximum_statement_bytes": profile.max_statement_bytes,
        "identifier_limit": identifier,
        "maximum_value_bytes": profile.max_text_value_bytes,
        "maximum_row_bytes": profile.max_row_bytes,
    }
    adapter_envelope = None
    if dolt_envelope:
        adapter_envelope = {
            **profile_limits,
            "limit_basis": "proposed_adapter_envelope",
            "evidence_status": "pending_pinned_live_conformance",
        }
        theoretical = {
            key: None for key in (
                "maximum_physical_columns", "maximum_source_variables",
                "maximum_statement_bytes", "identifier_limit",
                "maximum_value_bytes", "maximum_row_bytes",
            )
        }
        theoretical["limit_basis"] = "server_limits_not_claimed"
    else:
        theoretical = {
            **profile_limits,
            "limit_basis": "profile_theoretical_engine_ceiling",
        }
    effective = None
    status = "not_connected"
    if active and active["profile"] == name and (
        not dolt_envelope or dolt_declaration is not None
    ):
        effective = (
            dolt_effective_limits(dolt_declaration)
            if dolt_envelope and dolt_declaration is not None
            else dict(theoretical)
        )
        default_source = (
            "proposed adapter envelope pending pinned live conformance"
            if dolt_envelope else "profile theoretical engine ceiling"
        )
        sources = {
            "maximum_source_variables": default_source,
            "maximum_statement_bytes": default_source,
            "maximum_physical_columns": default_source,
            "identifier_limit": identifier["source"],
            "maximum_value_bytes": default_source,
            "maximum_row_bytes": default_source,
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
        elif name in MYSQL_WIRE_PROFILES:
            packet = int(observed["max_allowed_packet"])
            payload = max(0, (packet - 131_072) // 2)
            effective["maximum_value_bytes"] = min(
                effective["maximum_value_bytes"], payload,
            )
            effective["maximum_statement_bytes"] = payload
            sources["maximum_value_bytes"] = "active @@max_allowed_packet worst-case payload"
            sources["maximum_statement_bytes"] = "active @@max_allowed_packet worst-case payload"
            status = "active_connection_mixed"
        else:
            status = "profile_theoretical_fallback"
        effective["sources"] = sources
    elif active and active["profile"] == name and dolt_envelope:
        status = "blocked_pending_pinned_live_conformance"
    policy = SERVER_POLICIES[name]
    dolt_declarations = (
        _validated_dolt_declarations(dolt_conformance_source)
        if dolt_envelope else ()
    )
    dolt_claimed_versions = sorted({
        version
        for declaration in dolt_declarations
        for version in declaration["claimed_product_versions"]
    })
    dolt_tested_versions = sorted({
        version
        for declaration in dolt_declarations
        for version in declaration["tested_product_versions"]
    })
    dolt_status = (
        _conformance_source(dolt_conformance_source).status()
        if dolt_envelope else None
    )
    return {
        "driver": "psycopg" if name == "postgresql" else "PyMySQL" if name in MYSQL_WIRE_PROFILES else "sqlite3",
        "claimed_server_versions": (
            dolt_claimed_versions if dolt_envelope else policy["claimed"]
        ),
        "ci_tested_server_versions": (
            dolt_tested_versions if dolt_envelope else policy["ci"]
        ),
        "write_conformance": (
            {
                "declaration_schema_id": DOLT_WRITE_CONFORMANCE[
                    "declaration_schema_id"
                ],
                **dict(dolt_status or {}),
                "active_declaration_id": (
                    dolt_declaration["declaration_id"]
                    if dolt_declaration is not None else None
                ),
            } if dolt_envelope else {
                "write_enabled": True,
                "tested_server_versions": list(policy["ci"]),
                "status": "profile_claimed",
            }
        ),
        "operational_write_enabled": (
            dolt_declaration is not None
            if dolt_envelope and active and active["profile"] == name
            else bool((dolt_status or {}).get("write_enabled"))
            if dolt_envelope else True
        ),
        "theoretical_limits": theoretical,
        "adapter_envelope": adapter_envelope,
        "effective_limits": effective,
        "effective_limits_status": status,
        "numeric_type": "DOUBLE PRECISION" if name == "postgresql" else "DOUBLE" if name in MYSQL_WIRE_PROFILES else "REAL",
        "numeric_value_policy": {
            "sql_null": "canonical_system_missing",
            "spss_nan": "canonicalize_to_sql_null_during_spss_decode",
            "adapter_input": "finite_binary64_or_null",
            "positive_infinity": "reject_before_mutation",
            "negative_infinity": "reject_before_mutation",
            "live_bit_exact_evidence": (
                "pending_pinned_live_conformance" if dolt_envelope
                else "profile_conformance_claim"
            ),
        },
        "text_type": "LONGTEXT" if name in MYSQL_WIRE_PROFILES else "TEXT",
        "ddl_atomic": name not in MYSQL_WIRE_PROFILES,
        "failure_cleanup": "compensating_cleanup" if name in MYSQL_WIRE_PROFILES else "transaction_rollback",
        "physical_table_mapping": "dataset.physical_table_schema + dataset.physical_table_name",
        "identifier_policy": "deterministic ASCII mapping; source name remains authoritative",
    }


def server_version_supported(
    profile: str,
    raw_version: str,
    *,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> bool:
    version = _version_tuple(raw_version)
    if profile == "dolt":
        try:
            return _dolt_write_enabled(
                dolt_conformance_source,
                active_product_version=raw_version.strip(),
            )
        except UnsupportedOperationError:
            return False
    if profile == "sqlite":
        return (3, 24) <= version[:2] < (4, 0)
    allowed = {
        "postgresql": {(17,), (18,)},
        "mysql": {(8, 4), (9, 7)},
        "mariadb": {(11, 4), (11, 8), (12, 3)},
        "dolt": {(2, 2, 2)},
    }[profile]
    width = len(next(iter(allowed)))
    return version[:width] in allowed


def _matched_claim(
    profile: str,
    raw_version: str,
    *,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> str | None:
    if not server_version_supported(
        profile,
        raw_version,
        dolt_conformance_source=dolt_conformance_source,
    ):
        return None
    if profile == "dolt":
        return raw_version.strip()
    version = _version_tuple(raw_version)
    for claim in SERVER_POLICIES[profile]["claimed"]:
        numbers = _version_tuple(claim)
        if profile == "sqlite" or version[:2] == numbers[:2] or version[:1] == numbers[:1]:
            return claim
    claims = SERVER_POLICIES[profile]["claimed"]
    return claims[0] if claims else None


def _version_tuple(raw_version: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", raw_version)
    if not match:
        return ()
    return tuple(int(part) for part in match.groups(default="0"))


def _normalized_version(raw_version: str) -> str:
    return ".".join(str(part) for part in _version_tuple(raw_version))
