from dataclasses import replace

import pytest
from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from openstatspec.sql.normative import catalog as normative_catalog

from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.profiles import MYSQL, POSTGRESQL, SQLITE, preflight, profile_for_url
from openstatspec.sql.capabilities import server_version_supported
from openstatspec.sql.wide import binary64_type


def test_profile_detection_tracks_supported_dialect_urls() -> None:
    assert profile_for_url("sqlite:///dataset.sqlite") is SQLITE
    assert profile_for_url("postgresql+psycopg://user@host/database") is POSTGRESQL
    assert profile_for_url("mysql+pymysql://user@host/database") is MYSQL
    assert profile_for_url("mariadb+mariadbconnector://user@host/database") is MYSQL


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
