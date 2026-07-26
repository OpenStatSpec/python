"""Stable, database-connected public API."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import CapabilityDeclaration
from .core.results import result
from .spss import export_dataset, import_dataset, inspect_source
from .sql import declared_profiles, validate_dataset



def capability_matrix() -> Mapping[str, Any]:
    return {
        "spss": {
            "values": "supported", "variable_labels": "supported",
            "value_labels": "supported",
            "print_format": "supported", "write_format": "unobservable",
            "source_encoding": "preserved only when UTF-8",
            "measurement_level": "supported", "user_missing_rules": "supported",
            "documents": "supported", "multiple_response_sets": "lossy",
            "variable_alignment": "lossy", "variable_sets": "unobservable",
            "custom_attributes": "unobservable", "variable_role": "unobservable",
        },
        "sql_profiles": declared_profiles(),
    }

def capabilities() -> Mapping[str, Any]:
    return CapabilityDeclaration(
        specification="OpenStatSpec strict wide-table SPSS profile (initial)",
        formats={"SAV": {"import": True, "export": True}, "ZSAV": {"import": True, "export": True}},
        database_profiles=declared_profiles(),
    ).as_dict()


def inspect(source: str | Path, /, **options: Any) -> Mapping[str, Any]:
    return result(inspect_source(source, **options))


def import_sav(source: str | Path, /, *, database_url: Any, dataset_id: str, **options: Any) -> Mapping[str, Any]:
    """Import one source file into one dedicated wide SQL table."""
    return result(import_dataset(source, database_url=database_url, dataset_id=dataset_id, **options))


def export_sav(*, database_url: Any, dataset_id: str, destination: str | Path, **options: Any) -> Mapping[str, Any]:
    """Export one database-resident conforming dataset to SAV/ZSAV."""
    return result(export_dataset(database_url=database_url, dataset_id=dataset_id, destination=destination, **options))


def validate(*, database_url: Any, dataset_id: str, **options: Any) -> Mapping[str, Any]:
    return result(validate_dataset(database_url=database_url, dataset_id=dataset_id, **options))
