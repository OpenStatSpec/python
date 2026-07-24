import pytest

from openstatspec.core import UnsupportedOperationError
from openstatspec.sql.profiles import MYSQL, POSTGRESQL, SQLITE, preflight, profile_for_url


def test_profile_detection_tracks_supported_dialect_urls() -> None:
    assert profile_for_url("sqlite:///dataset.sqlite") is SQLITE
    assert profile_for_url("postgresql+psycopg://user@host/database") is POSTGRESQL
    assert profile_for_url("mysql+pymysql://user@host/database") is MYSQL
    assert profile_for_url("mariadb+mariadbconnector://user@host/database") is MYSQL


def test_profile_preflight_fails_without_transforming_a_wide_dataset() -> None:
    with pytest.raises(UnsupportedOperationError, match="Target capability exceeded"):
        preflight(POSTGRESQL, POSTGRESQL.max_physical_variables + 1)


def test_unknown_target_is_explicitly_rejected() -> None:
    with pytest.raises(UnsupportedOperationError, match="No OpenStatSpec SQL profile"):
        profile_for_url("oracle://host/database")