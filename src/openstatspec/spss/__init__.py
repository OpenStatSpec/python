"""SAV/ZSAV adapter boundary; never a data-model transformation layer."""

from pathlib import Path
from typing import Any

from ..sql.dolt_conformance import DoltConformanceSource
from .sav import export_sav_dataset, import_sav_dataset, inspect_sav
from .semantics import compare_sav_semantics


def inspect_source(source: str | Path, **options: Any) -> dict[str, Any]:
    return inspect_sav(source)


def import_dataset(
    source: str | Path, *, database_url: Any, dataset_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
    **options: Any,
) -> dict[str, Any]:
    return import_sav_dataset(
        source=source, database_url=str(database_url), dataset_id=dataset_id,
        dolt_conformance_source=dolt_conformance_source,
    )


def export_dataset(
    *, database_url: Any, dataset_id: str, destination: str | Path,
    dolt_conformance_source: DoltConformanceSource | None = None,
    **options: Any,
) -> dict[str, Any]:
    return export_sav_dataset(
        database_url=str(database_url), dataset_id=dataset_id,
        destination=destination,
        allow_loss=tuple(options.get("allow_loss", ())),
        legacy_locale=options.get("legacy_locale"),
        dolt_conformance_source=dolt_conformance_source,
    )
