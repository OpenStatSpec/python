from dataclasses import replace
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, Table, Text
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from openstatspec.sql.normative import catalog as normative_catalog

from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.profiles import DOLT, MYSQL, POSTGRESQL, SQLITE, preflight, profile_for_url
import openstatspec.sql.capabilities as capabilities
import openstatspec.sql.wide as wide
import openstatspec.spss.sav as sav
from openstatspec.sql.capabilities import active_connection, effective_profile, server_version_supported
from openstatspec.sql.wide import binary64_type, string_type


def test_profile_detection_tracks_supported_dialect_urls() -> None:
    assert profile_for_url("sqlite:///dataset.sqlite") is SQLITE
    assert profile_for_url("postgresql+psycopg://user@host/database") is POSTGRESQL
    assert profile_for_url("mysql+pymysql://user@host/database") is MYSQL
    assert profile_for_url("mariadb+mariadbconnector://user@host/database") is MYSQL
    assert profile_for_url("mysql+pymysql://user@host/dolt_database") is MYSQL

def test_profile_declarations_publish_released_specification_provenance() -> None:
    for declaration in capabilities.profile_declarations().values():
        assert declaration["specification_status"] == "released"
        assert declaration["specification_release"] == "v0.2.1"
        assert (
            declaration["specification_commit"]
            == "5b62bce1d2f4d719ac6ca42d73f07e7a127c7093"
        )

def test_profile_preflight_fails_without_transforming_a_wide_dataset() -> None:
    with pytest.raises(UnsupportedOperationError, match="Target capability exceeded"):
        preflight(POSTGRESQL, POSTGRESQL.max_physical_variables + 1)


def test_numeric_columns_compile_to_explicit_binary64_types() -> None:
    table = Table("precision_fixture", MetaData(), Column("value", binary64_type()))

    mysql_ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).upper()
    postgres_ddl = str(CreateTable(table).compile(dialect=postgresql.dialect())).upper()
    sqlite_ddl = str(CreateTable(table).compile(dialect=sqlite.dialect())).upper()

    assert "VALUE DOUBLE" in mysql_ddl
    assert "VALUE FLOAT" not in mysql_ddl
    assert "VALUE DOUBLE PRECISION" in postgres_ddl
    assert "VALUE REAL" in sqlite_ddl


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


@pytest.mark.parametrize(
    ("profile", "version", "supported"),
    [
        ("mysql", "8.4.0", True), ("mysql", "8.4.999", True),
        ("mysql", "9.7.2", True), ("mysql", "8.0.44", False),
        ("mysql", "8.5.0", False), ("mysql", "9.8.0", False),
        ("mariadb", "11.4.0-MariaDB", True),
        ("mariadb", "11.4.999-MariaDB", True),
        ("mariadb", "11.8.8-MariaDB", True),
        ("mariadb", "12.3.2-MariaDB", True),
        ("mariadb", "11.5.0-MariaDB", False),
        ("mariadb", "12.4.0-MariaDB", False),
        ("postgresql", "17.0", True), ("postgresql", "17.99", True),
        ("postgresql", "18.4", True), ("postgresql", "16.10", False),
        ("postgresql", "19.0", False),
    ],
)
def test_claimed_server_version_policy_is_explicit(
    profile: str, version: str, supported: bool,
) -> None:
    assert server_version_supported(profile, version) is supported


@pytest.mark.parametrize(
    "version",
    [
        "2.2.2", " 2.2.2 ", "2.2.3", "2.2.999", "2.2.0", "2.2.1",
        "2.3.0", "2.2", "2.2.3-rc1", "2.2.3+build.1", "v2.2.3",
        "2.2.03", "garbage",
    ],
)
def test_dolt_without_concrete_declaration_is_not_claimed(version: str) -> None:
    assert server_version_supported("dolt", version) is False


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


@pytest.mark.services
def test_live_dolt_identity_is_observed_but_not_claimed_without_declarations() -> None:
    database_url = os.environ.get("OPENSTATSPEC_DOLT_URL")
    if not database_url:
        pytest.skip("OPENSTATSPEC_DOLT_URL is not configured")

    active = active_connection(database_url)
    assert active["profile"] == "dolt"
    assert active["claimed_supported"] is False
    assert active["matched_claim"] is None


@pytest.mark.services
@pytest.mark.parametrize(
    ("database_environment", "version_environment", "profile"),
    [
        (
            "OPENSTATSPEC_POSTGRES_URL",
            "OPENSTATSPEC_EXPECTED_POSTGRES_VERSION",
            "postgresql",
        ),
        (
            "OPENSTATSPEC_MYSQL_URL",
            "OPENSTATSPEC_EXPECTED_MYSQL_VERSION",
            "mysql",
        ),
        (
            "OPENSTATSPEC_MARIADB_URL",
            "OPENSTATSPEC_EXPECTED_MARIADB_VERSION",
            "mariadb",
        ),
        (
            "OPENSTATSPEC_DOLT_URL",
            "OPENSTATSPEC_EXPECTED_DOLT_VERSION",
            "dolt",
        ),
    ],
)
def test_live_server_version_matches_exact_ci_evidence(
    database_environment: str, version_environment: str, profile: str,
) -> None:
    database_url = os.environ.get(database_environment)
    if not database_url:
        pytest.skip(f"{database_environment} is not configured")
    expected_version = os.environ.get(version_environment)
    assert expected_version, f"{version_environment} is required for CI provenance"

    active = active_connection(database_url)

    assert active["profile"] == profile
    assert active["server_version"] == expected_version


def test_server_policy_distinguishes_claimed_families_from_exact_ci_evidence() -> None:
    declarations = capabilities.profile_declarations()
    assert declarations["mysql"]["claimed_server_versions"] == [
        "MySQL 8.4.x", "MySQL 9.7.x",
    ]
    assert declarations["mysql"]["ci_tested_server_versions"] == [
        "MySQL 8.4.11", "MySQL 9.7.2",
    ]
    assert declarations["mariadb"]["claimed_server_versions"] == [
        "MariaDB 11.4.x", "MariaDB 11.8.x", "MariaDB 12.3.x",
    ]
    assert declarations["mariadb"]["ci_tested_server_versions"] == [
        "MariaDB 11.4.12", "MariaDB 11.8.8", "MariaDB 12.3.2",
    ]
    assert declarations["postgresql"]["claimed_server_versions"] == [
        "PostgreSQL 17.x", "PostgreSQL 18.x",
    ]
    assert declarations["postgresql"]["ci_tested_server_versions"] == [
        "PostgreSQL 17.10", "PostgreSQL 18.4",
    ]
    assert declarations["dolt"]["claimed_server_versions"] == []
    assert declarations["dolt"]["ci_tested_server_versions"] == []


class _ProbeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class _ProbeConnection:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        self.dialect = SimpleNamespace(name="mysql")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement):
        query = str(statement)
        self.calls.append(query)
        value = self.responses[query]
        if isinstance(value, Exception):
            raise value
        return _ProbeResult(value)


class _ProbeEngine:
    def __init__(self, connection):
        self.connection = connection
        self.dialect = SimpleNamespace(driver="pymysql")

    def connect(self):
        return self.connection


def _mock_mysql_probes(monkeypatch, **overrides):
    responses = {
        "select @@version": "8.0.31",
        "select @@version_comment": "Dolt",
        "select DOLT_VERSION()": "2.2.3",
        "select ACTIVE_BRANCH()": "main",
        "select @@max_allowed_packet": 1_073_741_824,
    }
    responses.update(overrides)
    connection = _ProbeConnection(responses)
    monkeypatch.setattr(capabilities, "create_engine", lambda _url: _ProbeEngine(connection))
    return connection


def test_dolt_identity_is_exact_and_publishes_wire_and_product_versions(monkeypatch) -> None:
    connection = _mock_mysql_probes(
        monkeypatch, **{"select @@version_comment": "  dOlT  "},
    )

    active = active_connection("mysql+pymysql://user@host/database")

    assert active["dialect"] == "mysql"
    assert active["profile"] == "dolt"
    assert active["product"] == "Dolt"
    assert active["raw_wire_version"] == "8.0.31"
    assert active["raw_product_version"] == active["raw_server_version"] == "2.2.3"
    assert active["raw_version_comment"] == "dOlT"
    assert active["server_version"] == "2.2.3"
    assert active["identity_source"] == (
        "SELECT @@version, @@version_comment, DOLT_VERSION(), ACTIVE_BRANCH()"
    )
    assert active["claimed_supported"] is False
    assert active["matched_claim"] is None
    assert active["working_set_binding"]["active_branch"] == "main"
    assert connection.calls == [
        "select @@version", "select @@version_comment", "select DOLT_VERSION()",
        "select ACTIVE_BRANCH()", "select @@max_allowed_packet",
    ]

@pytest.mark.parametrize("comment", [None, False, "", "   "])
def test_mysql_wire_identity_requires_a_nonempty_version_comment(monkeypatch, comment) -> None:
    connection = _mock_mysql_probes(
        monkeypatch, **{"select @@version_comment": comment},
    )

    with pytest.raises(UnsupportedOperationError, match="@@version_comment"):
        active_connection("mysql+pymysql://user@host/database")

    assert "select DOLT_VERSION()" not in connection.calls
    assert "select @@max_allowed_packet" not in connection.calls


def test_mysql_wire_identity_fails_closed_when_comment_probe_raises(monkeypatch) -> None:
    connection = _mock_mysql_probes(
        monkeypatch, **{"select @@version_comment": RuntimeError("probe unavailable")},
    )

    with pytest.raises(UnsupportedOperationError, match="@@version_comment"):
        active_connection("mysql+pymysql://user@host/database")

    assert "select DOLT_VERSION()" not in connection.calls


def test_dolt_comment_requires_a_nonempty_product_version(monkeypatch) -> None:
    _mock_mysql_probes(monkeypatch, **{"select DOLT_VERSION()": None})

    with pytest.raises(UnsupportedOperationError, match=r"DOLT_VERSION\(\)"):
        active_connection("mysql+pymysql://user@host/database")


@pytest.mark.parametrize(
    "product_version",
    [
        "2.2.0", "2.2.1", "2.3.0", "2.2",
        "2.2.3-rc1", "2.2.3+build.1", "v2.2.3", "2.2.03", "garbage",
    ],
)
def test_dolt_identity_observes_but_does_not_claim_unbound_versions(
    monkeypatch, product_version,
) -> None:
    connection = _mock_mysql_probes(
        monkeypatch, **{"select DOLT_VERSION()": product_version},
    )

    active = active_connection("mysql+pymysql://user@host/database")

    assert active["raw_product_version"] == product_version
    assert active["claimed_supported"] is False
    assert active["matched_claim"] is None
    assert connection.calls == [
        "select @@version", "select @@version_comment", "select DOLT_VERSION()",
        "select ACTIVE_BRANCH()", "select @@max_allowed_packet",
    ]

def test_unknown_mysql_wire_product_fails_closed_without_dolt_probe(monkeypatch) -> None:
    connection = _mock_mysql_probes(
        monkeypatch,
        **{
            "select @@version": "8.4.6",
            "select @@version_comment": "Percona Server",
        },
    )

    with pytest.raises(UnsupportedOperationError, match="product is unknown"):
        active_connection("mysql+pymysql://user@host/database")

    assert "select DOLT_VERSION()" not in connection.calls
    assert "select @@max_allowed_packet" not in connection.calls


def test_non_dolt_products_never_call_the_dolt_function(monkeypatch) -> None:
    connection = _mock_mysql_probes(
        monkeypatch,
        **{
            "select @@version": "8.4.6",
            "select @@version_comment": "MySQL Community Server",
        },
    )

    active = active_connection("mysql+pymysql://user@host/database")

    assert active["profile"] == "mysql"
    assert active["claimed_supported"] is True
    assert "select DOLT_VERSION()" not in connection.calls


def test_effective_profile_fails_closed_without_dolt_declarations(monkeypatch) -> None:
    active = {
        "profile": "dolt",
        "raw_product_version": "2.2.3",
    }
    monkeypatch.setattr(
        capabilities, "active_connection", lambda _url, **_kwargs: active,
    )

    with pytest.raises(UnsupportedOperationError, match="no concrete declarations"):
        effective_profile("mysql+pymysql://user@host/database")

def test_dolt_declaration_is_fail_closed_without_concrete_evidence() -> None:
    declaration = capabilities.profile_declarations()["dolt"]

    assert declaration["claimed_server_versions"] == []
    assert declaration["ci_tested_server_versions"] == []
    assert declaration["operational_write_enabled"] is False
    assert declaration["effective_limits"] is None
    assert declaration["effective_limits_status"] == "not_connected"
    assert declaration["write_conformance"]["declaration_count"] == 0
    assert declaration["write_conformance"]["write_enabled"] is False
    assert declaration["adapter_envelope"]["limit_basis"] == "proposed_adapter_envelope"
    assert declaration["theoretical_limits"]["limit_basis"] == (
        "server_limits_not_claimed"
    )
    assert declaration["text_type"] == "LONGTEXT"
    assert capabilities.profile_declarations()["mysql"]["text_type"] == "TEXT"
    assert capabilities.profile_declarations()["mariadb"]["text_type"] == "TEXT"

def test_dolt_uses_longtext_without_changing_mysql_storage() -> None:
    dolt_table = Table("dolt_text", MetaData(), Column("value", string_type(DOLT)))
    mysql_table = Table("mysql_text", MetaData(), Column("value", string_type(MYSQL)))

    assert "VALUE LONGTEXT" in str(CreateTable(dolt_table).compile(dialect=mysql.dialect())).upper()
    mysql_ddl = str(CreateTable(mysql_table).compile(dialect=mysql.dialect())).upper()
    assert "VALUE TEXT" in mysql_ddl
    assert "LONGTEXT" not in mysql_ddl


def test_dolt_row_preflight_enforces_configured_utf8_value_limit() -> None:
    limited = replace(DOLT, max_text_value_bytes=3)
    variables = [{
        "ordinal": 1, "source_name": "value", "physical_name": "value",
        "storage_kind": "string", "string_width": 3,
    }]
    preflight(limited, variables, rows=[{"value": "xxx"}])

    with pytest.raises(UnsupportedOperationError) as error:
        preflight(limited, variables, rows=[{"value": "xxxx"}])
    assert error.value.details["reason"] == "text_value_limit"
    assert error.value.details["maximum"] == 3

@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_dolt_numeric_preflight_rejects_nonfinite_values(value) -> None:
    variables = [{
        "ordinal": 1, "source_name": "value", "physical_name": "value",
        "storage_kind": "numeric", "string_width": None,
    }]

    with pytest.raises(UnsupportedOperationError) as error:
        preflight(DOLT, variables, rows=[{"value": value}])

    assert error.value.details == {
        "reason": "nonfinite_numeric_value",
        "row_ordinal": 1,
        "source_name": "value",
    }


def test_validate_identity_failure_happens_before_catalog_access(monkeypatch) -> None:
    def fail_identity(_url, **_kwargs):
        raise UnsupportedOperationError("identity unavailable")

    monkeypatch.setattr(wide, "effective_profile", fail_identity)
    monkeypatch.setattr(
        wide, "create_engine",
        lambda _url: pytest.fail("database access continued after identity failure"),
    )

    with pytest.raises(UnsupportedOperationError, match="identity unavailable"):
        wide.validate_wide_dataset(
            database_url="mysql+pymysql://user@host/database", dataset_id="fixture",
        )


def test_export_identity_failure_happens_before_read_or_destination(monkeypatch, tmp_path) -> None:
    destination = tmp_path / "blocked.sav"

    def fail_identity(_url, **_kwargs):
        raise UnsupportedOperationError("identity unavailable")

    monkeypatch.setattr(sav, "effective_profile", fail_identity)
    monkeypatch.setattr(
        sav, "read_wide_dataset",
        lambda **_kwargs: pytest.fail("catalog read continued after identity failure"),
    )

    with pytest.raises(UnsupportedOperationError, match="identity unavailable"):
        sav.export_sav_dataset(
            database_url="mysql+pymysql://user@host/database",
            dataset_id="fixture", destination=destination,
        )

    assert not destination.exists()

def test_effective_profile_preserves_explicit_driver_validation(monkeypatch) -> None:
    def unexpected_active_connection(*_args, **_kwargs):
        raise AssertionError("active connection must not be probed for a rejected driver")

    monkeypatch.setattr(
        capabilities, "active_connection", unexpected_active_connection,
    )

    with pytest.raises(
        UnsupportedOperationError,
        match=r"PostgreSQL requires an explicit postgresql\+psycopg URL",
    ):
        effective_profile("postgresql+psycopg2://user@host/database")


def test_effective_profile_applies_declared_identifier_limit(monkeypatch) -> None:
    active = {
        "profile": "postgresql",
        "claimed_supported": True,
        "server_version": "17.10",
        "observed": {},
    }
    limits = {
        "identifier_limit": {"value": 7},
        "maximum_source_variables": 10,
        "maximum_value_bytes": 100,
        "maximum_row_bytes": 200,
        "maximum_statement_bytes": 300,
    }
    monkeypatch.setattr(
        capabilities, "active_connection", lambda _url, **_kwargs: active,
    )
    monkeypatch.setattr(
        capabilities, "_profile", lambda *_args, **_kwargs: {
            "effective_limits": limits,
        },
    )

    profile, observed = effective_profile(
        "postgresql+psycopg://user@host/database",
    )

    assert observed is active
    assert profile.identifier_limit == 7


def test_wide_string_column_uses_effective_dolt_storage() -> None:
    table = Table(
        "dolt_wide_text",
        MetaData(),
        Column("value", wide._wide_column_type(DOLT, "string")),
    )

    ddl = str(CreateTable(table).compile(dialect=mysql.dialect())).upper()
    assert "VALUE LONGTEXT" in ddl

def test_capability_inspection_reports_unbound_dolt_as_disabled(monkeypatch) -> None:
    active = {
        "profile": "dolt",
        "raw_product_version": "2.2.3",
        "observed": {"max_allowed_packet": 1_073_741_824},
    }
    monkeypatch.setattr(
        capabilities, "active_connection", lambda _url, **_kwargs: active,
    )

    declaration = capabilities.profile_declarations(
        "mysql+pymysql://user@host/database",
    )["dolt"]

    assert declaration["operational_write_enabled"] is False
    assert declaration["effective_limits"] is None
    assert declaration["effective_limits_status"] == (
        "blocked_pending_pinned_live_conformance"
    )


def test_bound_catalog_transaction_allows_gated_dolt_audit(monkeypatch) -> None:
    class TransactionConnection:
        def __init__(self):
            self.rollback_called = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def rollback(self):
            self.rollback_called = True

        def begin(self):
            return self

    class TransactionEngine:
        def __init__(self, connection):
            self.connection = connection

        def connect(self):
            return self.connection

    connection = TransactionConnection()
    monkeypatch.setattr(
        wide, "_capture_dolt_state", lambda *_args, **_kwargs: {"profile": "dolt"},
    )
    monkeypatch.setattr(
        wide, "_require_dolt_working_set_binding", lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        wide, "_require_dolt_success_identity", lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        wide,
        "_dolt_failure_boundary_evidence",
        lambda *_args, **_kwargs: {"applicable": False},
    )

    with wide._bound_catalog_transaction(
        engine=TransactionEngine(connection),
        profile_name="dolt",
        active={"profile": "dolt"},
        audit_relations={"operation"},
        phase="test export audit",
    ) as yielded:
        assert yielded is connection

    assert connection.rollback_called is True

def test_effective_profile_rechecks_driver_after_dolt_identity(monkeypatch) -> None:
    active = {
        "profile": "dolt",
        "raw_product_version": "2.2.3",
    }
    monkeypatch.setattr(
        capabilities, "active_connection", lambda _url, **_kwargs: active,
    )

    with pytest.raises(
        UnsupportedOperationError,
        match=r"Dolt requires an explicit mysql\+pymysql URL",
    ):
        effective_profile(
            "mariadb+mariadbconnector://user@host/database",
        )

def test_dolt_operational_write_flag_requires_claimed_driver() -> None:
    assert capabilities.dolt_operational_write_enabled(
        {"driver_eligible": True}, declaration_matched=True,
    ) is True
    assert capabilities.dolt_operational_write_enabled(
        {"driver_eligible": False}, declaration_matched=True,
    ) is False
    assert capabilities.dolt_operational_write_enabled(
        {"driver_eligible": True}, declaration_matched=False,
    ) is False

def test_mysql_preflight_matches_emitted_text_limit() -> None:
    assert MYSQL.max_text_value_bytes == 65_535
    variables = [{
        "ordinal": 1,
        "source_name": "value",
        "physical_name": "value",
        "storage_kind": "string",
        "string_width": 65_536,
    }]

    with pytest.raises(UnsupportedOperationError) as error:
        preflight(MYSQL, variables)

    assert error.value.details["reason"] == "declared_string_width_limit"
    assert error.value.details["maximum"] == 65_535


@pytest.mark.parametrize(
    ("claimed_supported", "driver_eligible"),
    [(False, True), (True, False)],
)
def test_active_non_dolt_write_flag_requires_version_and_driver(
    monkeypatch, claimed_supported, driver_eligible,
) -> None:
    active = {
        "profile": "postgresql",
        "claimed_supported": claimed_supported,
        "driver_eligible": driver_eligible,
        "observed": {},
    }
    monkeypatch.setattr(
        capabilities, "active_connection", lambda _url, **_kwargs: active,
    )

    declaration = capabilities.profile_declarations(
        "postgresql+psycopg://user@host/database",
    )["postgresql"]

    assert declaration["operational_write_enabled"] is False

def test_mariadb_accepts_tested_pymysql_driver_for_capability_flag() -> None:
    assert capabilities._active_driver_eligible(
        "mysql+pymysql://user@host/database", "mariadb",
    ) is True


def test_dolt_validation_requires_longtext_not_generic_text() -> None:
    assert wide._valid_wide_string_type(DOLT, mysql.LONGTEXT()) is True
    assert wide._valid_wide_string_type(DOLT, Text()) is False
    assert wide._valid_wide_string_type(MYSQL, Text()) is True
