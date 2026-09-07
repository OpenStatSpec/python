"""Database URL invariants shared by persistent catalog operations."""

from pathlib import Path
from urllib.parse import unquote

from sqlalchemy.engine import make_url

from ..core import UnsupportedOperationError


def require_existing_database_url(database_url: str) -> None:
    """Prevent SQLite's implicit file creation during a read operation."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return
    database = url.database or ""
    if database in {"", ":memory:", "file::memory:"} or url.query.get("mode") == "memory":
        return
    if str(url.query.get("uri", "")).lower() == "true" and database.startswith("file:"):
        database = unquote(database[5:])
    if not Path(database).is_file():
        raise UnsupportedOperationError("Read operations require an existing SQLite database file.")


def require_persistent_database_url(database_url: str) -> None:
    """Reject SQLite URLs whose catalog disappears with a connection or engine."""
    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() != "sqlite":
        return
    database = parsed_url.database or ""
    mode = str(parsed_url.query.get("mode", "")).lower()
    if (
        database in {"", ":memory:"}
        or database.lower() == "file::memory:"
        or mode == "memory"
    ):
        raise UnsupportedOperationError(
            "OpenStatSpec catalogs require a persistent SQLite database URL."
        )
