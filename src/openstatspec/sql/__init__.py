"""SQL connection and strict wide-table/catalog operations."""

from typing import Any

from .wide import validate_wide_dataset


def validate_dataset(*, database_url: Any, dataset_id: str, **options: Any) -> dict[str, Any]:
    return validate_wide_dataset(database_url=str(database_url), dataset_id=dataset_id)
