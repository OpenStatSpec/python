from dataclasses import replace
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, MetaData, Table
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
        ("mysql", "8.4.6", True), ("mysql", "9.7.0", True),
        ("mysql", "8.0.44", False),
        ("dolt", "2.2.2", True), ("dolt", "2.2.3", False),
        ("mariadb", "11.4.8-MariaDB", True),
        ("mariadb", "11.8.3-MariaDB", True),
        ("mariadb", "12.3.1-MariaDB", True),
        ("postgresql", "17.6", True), ("postgresql", "18.1", True),
        ("postgresql", "16.10", False),
    ],
)
def test_claimed_server_version_policy_is_explicit(
    profile: str, version: str, supported: bool,
) -> None:
    assert server_version_supported(profile, version) is supported


@pytest.mark.services
@pytest.mark.parametrize(
    ("environment_name", "profile"),
    [
        ("OPENSTATSPEC_POSTGRES_URL", "postgresql"),
        ("OPENSTATSPEC_MYSQL_URL", "mysql"),
        ("OPENSTATSPEC_MARIADB_URL", "mariadb"),
        ("OPENSTATSPEC_DOLT_URL", "dolt"),
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
        "select DOLT_VERSION()": "2.2.2",
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
    assert active["profile"] == active["engine"] == active["product"] == "dolt"
    assert active["transport"] == "mysql"
    assert active["driver"] == "pymysql"
    assert active["raw_wire_version"] == "8.0.31"
    assert active["raw_product_version"] == active["raw_server_version"] == "2.2.2"
    assert active["raw_version_comment"] == "  dOlT  "
    assert active["server_version"] == "2.2.2"
    assert active["identity_source"] == "SELECT @@version, @@version_comment, DOLT_VERSION()"
    assert active["claimed_supported"] is True
    assert connection.calls == [
        "select @@version", "select @@version_comment",
        "select DOLT_VERSION()", "select @@max_allowed_packet",
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


def test_effective_profile_selects_dolt_without_changing_url_profile(monkeypatch) -> None:
    active = {
        "profile": "dolt", "server_version": "2.2.2", "claimed_supported": True,
        "observed": {"max_allowed_packet": 1_073_741_824},
    }
    monkeypatch.setattr(capabilities, "active_connection", lambda _url: active)

    profile, observed = effective_profile("mysql+pymysql://user@host/database")

    assert profile is not MYSQL
    assert profile.name == "dolt"
    assert profile.url_schemes == ()
    assert profile.max_physical_variables == 305
    assert profile.max_row_bytes == 65_504
    assert observed is active


def test_dolt_declaration_labels_conservative_envelopes() -> None:
    declaration = capabilities.profile_declarations()["dolt"]

    assert declaration["dialect"] == declaration["transport"] == "mysql"
    assert declaration["profile"] == declaration["engine"] == "dolt"
    assert declaration["claimed_server_versions"] == ["Dolt 2.2.2"]
    assert declaration["ci_tested_server_versions"] == ["Dolt 2.2.2"]
    assert declaration["proposed_adapter_limits"]["maximum_physical_columns"] == 306
    assert declaration["proposed_adapter_limits"]["maximum_source_variables"] == 305
    assert declaration["proposed_adapter_limits"]["maximum_row_bytes"] == 65_504
    assert declaration["observed_limits"]["minimum_observed_physical_columns"] == 307
    assert declaration["observed_limits"]["identifier_limit"]["value"] == 64
    assert declaration["observed_limits"]["rejected_identifier_bytes"] == 65
    assert set(declaration["proposed_adapter_limits"]) == {
        "maximum_physical_columns", "maximum_source_variables", "maximum_row_bytes",
    }
    assert declaration["limit_bases"]["maximum_physical_columns"] == "proposed_adapter_envelope"
    assert declaration["limit_bases"]["identifier_limit"] == "observed_exact_version"
    assert declaration["limit_bases"]["maximum_statement_bytes"] == "active_connection_observation"
    assert declaration["effective_limits"] is None
    assert declaration["text_type"] == "LONGTEXT"
    assert capabilities.profile_declarations()["mysql"]["text_type"] == "LONGTEXT"
    assert capabilities.profile_declarations()["mariadb"]["text_type"] == "LONGTEXT"
    assert declaration["ddl_atomic"] is False
    assert declaration["failure_cleanup"] == "compensating_cleanup"
    assert declaration["storage_evidence"]["binary64"]["maximum_finite_round_trip_exact"] is True
    assert declaration["storage_evidence"]["binary64"]["source"]
    assert declaration["storage_evidence"]["binary64"]["version"] == "2.2.2"
    assert declaration["storage_evidence"]["text"]["observed_value_bytes"] == 65_504
    assert declaration["storage_evidence"]["text"]["source"]
    assert declaration["storage_evidence"]["text"]["version"] == "2.2.2"
    assert declaration["storage_evidence"]["text"]["unit"] == "bytes"
    assert declaration["transformation_workflow"] == "unsupported"


def test_dolt_uses_longtext_without_changing_mysql_storage() -> None:
    dolt_table = Table("dolt_text", MetaData(), Column("value", string_type(DOLT)))
    mysql_table = Table("mysql_text", MetaData(), Column("value", string_type(MYSQL)))

    assert "VALUE LONGTEXT" in str(CreateTable(dolt_table).compile(dialect=mysql.dialect())).upper()
    mysql_ddl = str(CreateTable(mysql_table).compile(dialect=mysql.dialect())).upper()
    assert "VALUE TEXT" in mysql_ddl
    assert "LONGTEXT" not in mysql_ddl


def test_dolt_row_preflight_counts_utf8_values_against_adapter_envelope() -> None:
    variables = [{
        "ordinal": 1, "source_name": "value", "physical_name": "value",
        "storage_kind": "string", "string_width": 65_496,
    }]
    preflight(DOLT, variables, rows=[{"value": "x" * 65_496}])

    with pytest.raises(UnsupportedOperationError) as error:
        preflight(DOLT, variables, rows=[{"value": "x" * 65_497}])
    assert error.value.details["reason"] == "row_size_limit"


def test_validate_identity_failure_happens_before_catalog_access(monkeypatch) -> None:
    def fail_identity(_url):
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

    def fail_identity(_url):
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
