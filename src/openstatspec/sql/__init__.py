"""SQL connection and strict wide-table/catalog operations."""

from typing import Any

from .capabilities import profile_declarations
from .dolt_conformance import DoltConformanceSource
from .wide import (
    dolt_state_snapshot as _dolt_state_snapshot,
    initialize_wide_catalog,
    validate_wide_dataset,
)


def declared_profiles(
    database_url: str | None = None,
    *,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, dict[str, object]]:
    return profile_declarations(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )


def dolt_state_snapshot(
    *,
    database_url: Any,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    return _dolt_state_snapshot(
        database_url=str(database_url),
        dolt_conformance_source=dolt_conformance_source,
    )


def initialize_catalog(
    *,
    database_url: Any,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> dict[str, Any]:
    return initialize_wide_catalog(
        database_url=str(database_url),
        dolt_conformance_source=dolt_conformance_source,
    )


def validate_dataset(
    *,
    database_url: Any,
    dataset_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
    **options: Any,
) -> dict[str, Any]:
    return validate_wide_dataset(
        database_url=str(database_url),
        dataset_id=dataset_id,
        dolt_conformance_source=dolt_conformance_source,
    )


__all__ = [
    "DoltConformanceSource",
    "declared_profiles",
    "dolt_state_snapshot",
    "initialize_catalog",
    "validate_dataset",
]
