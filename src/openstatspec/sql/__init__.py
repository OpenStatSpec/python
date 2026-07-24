"""SQL connection and strict wide-table/catalog operations."""

from typing import Any

from ..core import UnsupportedOperationError


def validate_dataset(*, database_url: Any, dataset_id: str, **options: Any) -> dict[str, Any]:
    raise UnsupportedOperationError("No OpenStatSpec SQL profile is implemented; validation cannot guess a schema.")
