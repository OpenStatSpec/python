from dataclasses import replace
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

import openstatspec.sql.capabilities as capability_module
import openstatspec.sql.wide as wide
from openstatspec.sql.normative import catalog as normative_catalog

from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.profiles import DOLT, MYSQL, POSTGRESQL, SQLITE, preflight, profile_for_url
from openstatspec.sql.capabilities import active_connection, server_version_supported
from openstatspec.sql.wide import binary64_type, lossless_text_type


def test_profile_detection_tracks_supported_dialect_urls() -> None:
    assert profile_for_url("sqlite:///dataset.sqlite") is SQLITE
    assert profile_for_url("postgresql+psycopg://user@host/database") is POSTGRESQL
    assert profile_for_url("mysql+pymysql://user@host/database") is MYSQL
    assert profile_for_url("mariadb+mariadbconnector://user@host/database") is MYSQL


def test_profile_preflight_fails_without_transforming_a_wide_dataset() -> None:
    with pytest.raises(UnsupportedOperationError, match="Target capability exceeded"):
        preflight(POSTGRESQL, POSTGRESQL.max_source_variables + 1)


def test_numeric_columns_compile_to_explicit_binary64_types() -> None:
    table = Table("precision_fixture", MetaData(), Column("value", binary64_type()))

    mysql_ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).upper()
    postgres_ddl = str(CreateTable(table).compile(dialect=postgresql.dialect())).upper()
    sqlite_ddl = str(CreateTable(table).compile(dialect=sqlite.dialect())).upper()

    assert "VALUE DOUBLE" in mysql_ddl
    assert "VALUE FLOAT" not in mysql_ddl
    assert "VALUE DOUBLE PRECISION" in postgres_ddl
    assert "VALUE REAL" in sqlite_ddl


def test_mysql_wire_text_columns_compile_to_declared_longtext() -> None:
    table = Table("text_fixture", MetaData(), Column("value", lossless_text_type()))

    mysql_ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).upper()

    assert "VALUE LONGTEXT" in mysql_ddl
    assert "VALUE TEXT" not in mysql_ddl


@pytest.mark.parametrize("wire_profile", [MYSQL, DOLT], ids=["mysql", "dolt"])
def test_mysql_wire_additive_metadata_migration_uses_longtext(
    monkeypatch, wire_profile,
) -> None:
    metadata = MetaData()
    datasets, variables, _, _ = wide.catalog(metadata)
    multiple_response = wide.multiple_response_set_catalog(metadata)
    existing_columns = {
        datasets.name: {"case_weight_variable"},
        variables.name: {
            "role", "compat_name", "print_format", "write_format",
        },
        multiple_response.name: {
            "is_dichotomy", "use_category_labels", "use_first_var_label",
            "counted_value_type", "counted_numeric",
        },
    }

    class SyntheticInspector:
        def has_table(self, table_name):
            return table_name in existing_columns

        def get_columns(self, table_name):
            return [{"name": name} for name in existing_columns[table_name]]

    class SyntheticConnection:
        def __init__(self):
            self.dialect = mysql.dialect()
            self.profile = wire_profile
            self.statements = []

        def execute(self, statement):
            self.statements.append(str(statement))

    connection = SyntheticConnection()
    monkeypatch.setattr(wide, "inspect", lambda _connection: SyntheticInspector())

    wide._migrate_catalog_columns(
        connection, datasets, variables, multiple_response,
    )

    statements = "\n".join(connection.statements).upper()
    assert len(connection.statements) == 3
    assert "ADD COLUMN FILE_ATTRIBUTES LONGTEXT" in statements
    assert "ADD COLUMN ATTRIBUTES LONGTEXT" in statements
    assert "ADD COLUMN COUNTED_TEXT LONGTEXT" in statements
    assert "ADD COLUMN FILE_ATTRIBUTES TEXT" not in statements
    assert "ADD COLUMN ATTRIBUTES TEXT" not in statements
    assert "ADD COLUMN COUNTED_TEXT TEXT" not in statements


def test_unknown_target_is_explicitly_rejected() -> None:
    with pytest.raises(UnsupportedOperationError, match="No OpenStatSpec SQL profile"):
        profile_for_url("oracle://host/database")

def test_identifier_length_preflight_uses_profile_limit() -> None:
    limited = replace(SQLITE, identifier_limit=3)
    variables = [{"ordinal": 1, "source_name": "name", "physical_name": "name"}]
    with pytest.raises(UnsupportedOperationError, match="Target capability exceeded"):
        preflight(limited, variables)


def test_preflight_enforces_value_and_row_limits() -> None:
    limited = replace(SQLITE, max_text_value_bytes=3, max_row_bytes=10)
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 2,
    }]
    with pytest.raises(UnsupportedOperationError, match="Target capability exceeded"):
        preflight(limited, variables, rows=[{"name": "four"}])
    with pytest.raises(UnsupportedOperationError, match="Target capability exceeded"):
        preflight(
            replace(limited, max_text_value_bytes=100),
            variables, rows=[{"name": "abc"}],
        )


def test_normative_catalog_compiles_for_every_sql_family() -> None:
    catalog = normative_catalog(MetaData())
    for dialect in (sqlite.dialect(), mysql.dialect(), postgresql.dialect()):
        compiled = [str(CreateTable(table).compile(dialect=dialect)) for table in catalog.all()]
        assert len(compiled) == 17
        assert all("CREATE TABLE" in ddl for ddl in compiled)


def test_dolt_adapter_envelope_accepts_305_and_rejects_306_source_variables() -> None:
    preflight(DOLT, 305)
    with pytest.raises(UnsupportedOperationError) as error:
        preflight(DOLT, 306)
    assert error.value.details == {
        "reason": "source_variable_limit",
        "source_count": 306,
        "max_source": 305,
        "physical_count": 307,
        "max_physical": 306,
    }


@pytest.mark.parametrize(
    ("profile", "version", "supported"),
    [
        ("mysql", "8.4.6", True), ("mysql", "9.7.0", True),
        ("mysql", "8.0.44", False),
        ("mariadb", "11.4.8-MariaDB", True),
        ("mariadb", "11.8.3-MariaDB", True),
        ("mariadb", "12.3.1-MariaDB", True),
        ("postgresql", "17.6", True), ("postgresql", "18.1", True),
        ("postgresql", "16.10", False),
        ("dolt", "2.2.2", False), ("dolt", " 2.2.2 ", False),
        ("dolt", "2.2.2.1", False), ("dolt", "2.2.3", False),
    ],
)
def test_claimed_server_version_policy_is_explicit(
    profile: str, version: str, supported: bool,
) -> None:
    assert server_version_supported(profile, version) is supported


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _FakeMySqlConnection:
    dialect = SimpleNamespace(name="mysql")

    def __init__(self, probes):
        self.probes = probes

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement):
        return _ScalarResult(self.probes[str(statement).strip().casefold()])


class _FakeMySqlEngine:
    def __init__(self, probes):
        self.probes = probes

    def connect(self):
        return _FakeMySqlConnection(self.probes)


def _fake_mysql_identity(
    monkeypatch, *, wire="8.0.33", comment="Dolt", dolt="2.2.2",
    branch="feature/synthetic",
):
    probes = {
        "select @@version": wire,
        "select @@version_comment": comment,
        "select dolt_version()": dolt,
        "select active_branch()": branch,
        "select @@max_allowed_packet": 16_777_216,
    }
    monkeypatch.setattr(
        capability_module, "create_engine", lambda _database_url: _FakeMySqlEngine(probes),
    )


def test_active_identity_recognizes_only_exact_trimmed_casefolded_dolt_comment(monkeypatch):
    _fake_mysql_identity(monkeypatch, comment="  dOlT  ")
    active = capability_module.active_connection("mysql+pymysql://user@host/catalog")
    assert active["profile"] == "dolt"
    assert active["product"] == "Dolt"
    assert active["raw_wire_version"] == "8.0.33"
    assert active["raw_product_version"] == "2.2.2"
    assert active["claimed_supported"] is False
    assert active["working_set_binding"] == {
        "database": "catalog",
        "active_branch": "feature/synthetic",
    }
    with pytest.raises(UnsupportedOperationError, match="not bound"):
        capability_module.profile_declarations(
            "mysql+pymysql://user@host/catalog"
        )


def test_active_identity_does_not_guess_dolt_from_nonexact_comment(monkeypatch):
    _fake_mysql_identity(monkeypatch, comment="Dolt database")
    active = capability_module.active_connection("mysql+pymysql://user@host/catalog")
    assert active["profile"] == "mysql"


@pytest.mark.parametrize(
    ("wire", "comment", "dolt", "message"),
    [
        ("8.0.33-MariaDB", "Dolt", "2.2.2", "Conflicting Dolt and MariaDB"),
        ("8.0.33", "Dolt", "", "DOLT_VERSION"),
        ("8.0.33", "", "2.2.2", "@@version_comment"),
    ],
)
def test_active_dolt_identity_fails_closed(monkeypatch, wire, comment, dolt, message):
    _fake_mysql_identity(monkeypatch, wire=wire, comment=comment, dolt=dolt)
    with pytest.raises(UnsupportedOperationError, match=message):
        capability_module.active_connection("mysql+pymysql://user@host/catalog")


def test_active_dolt_identity_requires_nonempty_active_branch(monkeypatch):
    _fake_mysql_identity(monkeypatch, branch="")
    with pytest.raises(UnsupportedOperationError, match="ACTIVE_BRANCH"):
        capability_module.active_connection(
            "mysql+pymysql://user@host/catalog"
        )


def test_dolt_capabilities_separate_adapter_envelope_from_unclaimed_server_limits():
    declaration = capability_module.profile_declarations()["dolt"]
    assert declaration["claimed_server_versions"] == []
    assert declaration["ci_tested_server_versions"] == []
    assert declaration["operational_write_enabled"] is False
    assert declaration["write_conformance"] == {
        "declaration_schema_id": "openstatspec-dolt-adapter-declaration-v1",
        "write_enabled": False,
        "declarations_available": False,
        "declaration_count": 0,
        "status": "blocked_no_concrete_declarations",
        "active_declaration_id": None,
    }
    assert declaration["theoretical_limits"]["limit_basis"] == "server_limits_not_claimed"
    assert declaration["theoretical_limits"]["maximum_source_variables"] is None
    assert declaration["adapter_envelope"]["maximum_source_variables"] == 305
    assert declaration["adapter_envelope"]["maximum_physical_columns"] == 306
    assert declaration["adapter_envelope"]["evidence_status"] == "pending_pinned_live_conformance"


@pytest.mark.services
@pytest.mark.parametrize(
    ("environment_name", "profile"),
    [
        ("OPENSTATSPEC_POSTGRES_URL", "postgresql"),
        ("OPENSTATSPEC_MYSQL_URL", "mysql"),
        ("OPENSTATSPEC_MARIADB_URL", "mariadb"),
    ],
)
def test_active_server_identity_matches_claimed_ci_profile(
    environment_name: str, profile: str,
) -> None:
    database_url = os.environ.get(environment_name)
    if not database_url:
        pytest.skip(f"{environment_name} is not configured")
    active = active_connection(database_url)
    assert active["profile"] == profile
    assert active["claimed_supported"] is True
    assert active["matched_claim"] is not None


def _numeric_variables():
    return [{
        "ordinal": 1, "source_name": "score", "physical_name": "score",
        "storage_kind": "numeric",
    }]


def test_dolt_numeric_policy_accepts_finite_or_null_and_rejects_nonfinite_input():
    preflight(DOLT, _numeric_variables(), rows=[{"score": 1.25}, {"score": None}])
    for value, classification in (
        (float("nan"), "nan"),
        (float("inf"), "positive_infinity"),
        (float("-inf"), "negative_infinity"),
    ):
        with pytest.raises(UnsupportedOperationError) as error:
            preflight(DOLT, _numeric_variables(), rows=[{"score": value}])
        assert error.value.details["reason"] == "numeric_value_not_finite"
        assert error.value.details["classification"] == classification
    with pytest.raises(UnsupportedOperationError) as error:
        preflight(DOLT, _numeric_variables(), rows=[{"score": True}])
    assert error.value.details["reason"] == "numeric_value_type"


def test_dolt_numeric_capability_declares_spss_missing_canonicalization():
    policy = capability_module.profile_declarations()["dolt"]["numeric_value_policy"]
    assert policy == {
        "sql_null": "canonical_system_missing",
        "spss_nan": "canonicalize_to_sql_null_during_spss_decode",
        "adapter_input": "finite_binary64_or_null",
        "positive_infinity": "reject_before_mutation",
        "negative_infinity": "reject_before_mutation",
        "live_bit_exact_evidence": "pending_pinned_live_conformance",
    }


class _MappingRows:
    def __init__(self, *, one=None, rows=()):
        self._one = one
        self._rows = list(rows)

    def mappings(self):
        return self

    def one(self):
        return self._one

    def all(self):
        return self._rows


class _FakeDoltStateConnection:
    dialect = SimpleNamespace(name="mysql")

    def __init__(self, *, status=(), summary=()):
        self.status = list(status)
        self.summary = list(summary)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def exec_driver_sql(self, statement):
        self.calls.append(statement)
        normalized = " ".join(statement.split()).casefold()
        if normalized.startswith("select database()"):
            return _MappingRows(one={
                "database_name": "synthetic_catalog",
                "active_branch": "feature/synthetic",
                "head_hash": "0123456789abcdef",
            })
        if "from dolt_diff_summary" in normalized:
            return _MappingRows(rows=self.summary)
        if "from dolt_status" in normalized:
            return _MappingRows(rows=self.status)
        raise AssertionError(statement)


class _FakeDoltStateEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


def test_dolt_state_capture_uses_only_exact_read_only_probes_and_fixed_shapes():
    connection = _FakeDoltStateConnection()
    snapshot = wide._capture_dolt_state(
        connection, profile_name="dolt", audit_relations={"operation_catalog"},
    )

    assert snapshot["database"] == "synthetic_catalog"
    assert snapshot["active_branch"] == "feature/synthetic"
    assert snapshot["head"] == "0123456789abcdef"
    assert len(snapshot["snapshot_sha256"]) == 64
    joined = " ".join(connection.calls).casefold()
    assert "dolt_diff_summary('head', 'working')" in joined
    assert "dolt_diff_summary('head', 'staged')" in joined
    assert "dolt_diff_summary('staged', 'working')" in joined
    assert not any(
        keyword in joined
        for keyword in ("dolt_add", "dolt_commit", "checkout", "reset")
    )


def test_dolt_state_capture_fails_closed_on_shape_drift():
    connection = _FakeDoltStateConnection(status=[{"unexpected": "shape"}])
    with pytest.raises(UnsupportedOperationError, match="unexpected column shape"):
        wide._capture_dolt_state(
            connection, profile_name="dolt", audit_relations=set(),
        )


def test_dolt_failure_boundary_allows_only_audit_catalog_delta():
    before = wide._capture_dolt_state(
        _FakeDoltStateConnection(),
        profile_name="dolt", audit_relations={"operation_catalog"},
    )
    after = wide._capture_dolt_state(
        _FakeDoltStateConnection(status=[{
            "table_name": "operation_catalog",
            "staged": False,
            "status": "modified",
        }]),
        profile_name="dolt", audit_relations={"operation_catalog"},
    )

    evidence = wide._dolt_failure_boundary_evidence(before, after)
    assert evidence["verified"] is True
    changed = dict(after)
    changed["active_branch"] = "other-branch"
    evidence = wide._dolt_failure_boundary_evidence(before, changed)
    assert evidence["verified"] is False
    assert "active_branch_changed" in evidence["invariant_failures"]


def test_public_dolt_state_snapshot_is_read_only(monkeypatch):
    connection = _FakeDoltStateConnection()
    monkeypatch.setattr(
        wide, "active_connection",
        lambda _url, **_kwargs: {
            "profile": "dolt",
            "server_version": "2.2.2",
            "working_set_binding": {
                "database": "synthetic_catalog",
                "active_branch": "feature/synthetic",
            },
            "claimed_supported": False,
        },
    )
    monkeypatch.setattr(wide, "create_engine", lambda _url: _FakeDoltStateEngine(connection))

    result = wide.dolt_state_snapshot(
        database_url="mysql+pymysql://user@host/synthetic_catalog",
    )

    assert result["profile"] == "dolt"
    assert result["server_version"] == "2.2.2"
    assert result["read_only"] is True
    assert result["operational_write_enabled"] is False


def test_dolt_operational_profile_fails_closed_without_live_conformance(monkeypatch):
    _fake_mysql_identity(monkeypatch)
    with pytest.raises(UnsupportedOperationError, match="not bound"):
        capability_module.effective_profile(
            "mysql+pymysql://user@host/catalog"
        )


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ({"name": None}, "string_value_type"),
        ({"name": 123}, "string_value_type"),
        ({}, "string_value_missing"),
    ],
)
def test_string_preflight_requires_actual_string_values(row, reason):
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8,
    }]
    with pytest.raises(UnsupportedOperationError) as error:
        preflight(SQLITE, variables, rows=[row])
    assert error.value.details["reason"] == reason


def test_string_preflight_accepts_empty_spss_missing_string():
    variables = [{
        "ordinal": 1, "source_name": "name", "physical_name": "name",
        "storage_kind": "string", "string_width": 8,
    }]
    preflight(SQLITE, variables, rows=[{"name": ""}])
