"""SQL connection and strict wide-table/catalog operations."""

from typing import Any

from .profiles import PROFILES
from .wide import validate_wide_dataset


def declared_profiles() -> dict[str, dict[str, object]]:
    return {profile.name: profile.as_dict() for profile in PROFILES}


def validate_dataset(*, database_url: Any, dataset_id: str, **options: Any) -> dict[str, Any]:
    return validate_wide_dataset(database_url=str(database_url), dataset_id=dataset_id)
