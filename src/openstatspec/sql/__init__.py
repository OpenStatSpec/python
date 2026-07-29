"""SQL connection and strict wide-table/catalog operations."""

from typing import Any

from .capabilities import profile_declarations
from .wide import validate_wide_dataset


def declared_profiles(database_url: str | None = None) -> dict[str, dict[str, object]]:
    return profile_declarations(database_url)


def validate_dataset(*, database_url: Any, dataset_id: str, **options: Any) -> dict[str, Any]:
    return validate_wide_dataset(database_url=str(database_url), dataset_id=dataset_id)
