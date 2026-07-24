"""SAV/ZSAV adapter boundary; never a data-model transformation layer."""

from pathlib import Path
from typing import Any

from .sav import export_sav_dataset, import_sav_dataset, inspect_sav


def inspect_source(source: str | Path, **options: Any) -> dict[str, Any]:
    return inspect_sav(source)


def import_dataset(source: str | Path, *, database_url: Any, dataset_id: str, **options: Any) -> dict[str, Any]:
    return import_sav_dataset(source=source, database_url=str(database_url), dataset_id=dataset_id)


def export_dataset(*, database_url: Any, dataset_id: str, destination: str | Path, **options: Any) -> dict[str, Any]:
    return export_sav_dataset(database_url=str(database_url), dataset_id=dataset_id, destination=destination, allow_loss=tuple(options.get("allow_loss", ())) )
