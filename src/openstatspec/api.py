"""Stable, database-connected public API."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import CapabilityDeclaration
from .spss import export_dataset, import_dataset, inspect_source
from .sql import validate_dataset


def capabilities() -> Mapping[str, Any]:
    return CapabilityDeclaration.empty().as_dict()


def inspect(source: str | Path, /, **options: Any) -> Mapping[str, Any]:
    return inspect_source(source, **options)


def import_sav(source: str | Path, /, *, database_url: Any, dataset_id: str, **options: Any) -> Mapping[str, Any]:
    """Import one source file into one dedicated wide SQL table."""
    return import_dataset(source, database_url=database_url, dataset_id=dataset_id, **options)


def export_sav(*, database_url: Any, dataset_id: str, destination: str | Path, **options: Any) -> Mapping[str, Any]:
    """Export one database-resident conforming dataset to SAV/ZSAV."""
    return export_dataset(database_url=database_url, dataset_id=dataset_id, destination=destination, **options)


def validate(*, database_url: Any, dataset_id: str, **options: Any) -> Mapping[str, Any]:
    return validate_dataset(database_url=database_url, dataset_id=dataset_id, **options)
