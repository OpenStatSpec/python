"""Database URL invariants shared by persistent catalog operations."""

from sqlalchemy.engine import make_url

from ..core import UnsupportedOperationError


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
