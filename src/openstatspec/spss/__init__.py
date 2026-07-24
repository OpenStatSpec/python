"""SAV/ZSAV adapter boundary; never a data-model transformation layer."""

from pathlib import Path
from typing import Any

from ..core import UnsupportedOperationError


def inspect_source(source: str | Path, **options: Any) -> dict[str, Any]:
    raise UnsupportedOperationError("No SAV/ZSAV profile is implemented; source metadata cannot be guessed.")


def import_dataset(source: str | Path, *, database_url: Any, dataset_id: str, **options: Any) -> dict[str, Any]:
    raise UnsupportedOperationError("No SAV/ZSAV-to-SQL profile is implemented; no partial import was attempted.")


def export_dataset(*, database_url: Any, dataset_id: str, destination: str | Path, **options: Any) -> dict[str, Any]:
    raise UnsupportedOperationError("No SQL-to-SAV/ZSAV profile is implemented; no lossy export was attempted.")
