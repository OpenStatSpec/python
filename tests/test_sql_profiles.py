from dataclasses import replace

import pytest
from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.profiles import MYSQL, POSTGRESQL, SQLITE, preflight, profile_for_url
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
