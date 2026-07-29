"""Stable, database-connected public API."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import CapabilityDeclaration
from .core.results import result
from .spss import export_dataset, import_dataset, inspect_source
from .spss.sav import engine_identity
from .sql import declared_profiles, validate_dataset



def capability_matrix() -> Mapping[str, Any]:
    """Return the pyspssio-backed SAV/ZSAV fidelity boundary.

    supported means that the adapter has a tested faithful path.
    unobservable means that pyspssio's public reader API cannot expose the
    source semantic. fail-closed-on-export means an imported/catalogued value
    blocks export unless the documented audited loss route exists; a plain
    fail-closed feature has no faithful writer route at all.
    """
    return {
        "required_capabilities": [
            "sav_read", "sav_write", "zsav_read", "zsav_write",
            "file_label", "documents", "source_encoding", "attributes",
            "variable_dictionary", "value_labels", "missing_rules",
            "lowest_highest_missing", "long_utf8_strings", "weight_variable",
            "variable_sets", "multiple_response_sets",
            "multiple_response_string_counted_value",
        ],
        "engine": engine_identity(),
        "spss": {
            "values": "supported", "variable_labels": "supported",
            "value_labels": "supported",
            "print_format": "supported",
            "write_format": "supported",
            "file_label": "supported",
            "documents": "supported",
            "source_encoding": {
                "utf8": "supported",
                "legacy_code_pages": "requires-explicit-legacy-locale",
            },
            "measurement_level": "supported", "user_missing_rules": "supported",
            "multiple_response_sets": "supported",
            "variable_alignment": "supported",
            "variable_sets": "supported",
            "compatible_variable_names": "supported",
            "custom_attributes": {
                "scalar_values": "supported",
                "ordered_value_arrays": "supported",
            },
            "variable_role": "supported",
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
