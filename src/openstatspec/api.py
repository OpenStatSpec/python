"""Stable, database-connected public API."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core.results import result
from .spss import export_dataset, import_dataset, inspect_source
from .spss.sav import engine_identity
from .sql import (
    DoltConformanceSource,
    declared_profiles,
    dolt_state_snapshot as _dolt_state_snapshot,
    initialize_catalog as _initialize_catalog,
    validate_dataset,
)
from .sql.catalog_api import catalog_dataset as _catalog_dataset, catalog_datasets as _catalog_datasets
from .sql.workflow import (
    derive_dataset as _derive_dataset,
    execute_transformation as _execute_transformation,
    reconcile_started_runs as _reconcile_started_runs,
    register_transformation as _register_transformation,
    validate_derived_dataset as _validate_derived_dataset,
    transformation_capabilities,
    reconcile_physical_removals as _reconcile_physical_removals,
    remove_derived_relation as _remove_derived_relation,
    retire_derived_dataset as _retire_derived_dataset,
)
from .frontends.spss.execution import apply_spss_in_place as _apply_spss_in_place
from .sql.inplace_transform import (
    apply_transformation_plan_in_place as _apply_transformation_plan_in_place,
    in_place_transformation_capabilities,
    install_in_place_transformation_schema as _install_in_place_schema,
)
from .transform import TransformationPlan
from .sql.capabilities import (
    SPECIFICATION_COMMIT, SPECIFICATION_RELEASE, SPECIFICATION_STATUS,
    active_connection, catalog_binding,
)



def capability_matrix(
    database_url: str | None = None,
    *,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> Mapping[str, Any]:
    """Return the pyspssio-backed SAV/ZSAV fidelity boundary.

    supported means that the adapter has a tested faithful path.
    unobservable means that pyspssio's public reader API cannot expose the
    source semantic. fail-closed-on-export means an imported/catalogued value
    blocks export unless the documented audited loss route exists; a plain
    fail-closed feature has no faithful writer route at all.
    """
    declaration = {
        "specification": "OpenStatSpec",
        "specification_status": SPECIFICATION_STATUS,
        "specification_release": SPECIFICATION_RELEASE,
        "specification_commit": SPECIFICATION_COMMIT,
        "profile": "SPSS SAV/ZSAV 1.0",
        "directions": ["import", "export", "semantic_round_trip"],
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
        "resource_behavior": {
            "streaming_import": False, "streaming_export": False,
            "buffering": "fully_buffered", "maximum_cases": None,
            "maximum_source_file_bytes": None,
            "limit_basis": "runtime memory and active SQL connection limits",
        },
        "active_connection": (
            active_connection(
                database_url,
                dolt_conformance_source=dolt_conformance_source,
            )
            if database_url else None
        ),
        "catalog_binding": catalog_binding(database_url) if database_url else None,
        "sql_profiles": declared_profiles(
            database_url,
            dolt_conformance_source=dolt_conformance_source,
        ),
        "optional_profiles": {
            "sql_transformation_workflow": transformation_capabilities(database_url),
            "spss_in_place_transformation": (
                in_place_transformation_capabilities()
            ),
        },
    }
    return declaration

def capabilities(
    database_url: str | None = None,
    *,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> Mapping[str, Any]:
    return capability_matrix(
        database_url, dolt_conformance_source=dolt_conformance_source,
    )


def inspect(source: str | Path, /, **options: Any) -> Mapping[str, Any]:
    return result(inspect_source(source, **options))


def dolt_state_snapshot(
    *,
    database_url: Any,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> Mapping[str, Any]:
    """Return read-only Dolt branch, HEAD, status, and diff-summary evidence."""
    return result(_dolt_state_snapshot(
        database_url=database_url,
        dolt_conformance_source=dolt_conformance_source,
    ))


def initialize_catalog(
    *,
    database_url: Any,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> Mapping[str, Any]:
    """Install or explicitly migrate a dedicated OpenStatSpec catalog."""
    return result(_initialize_catalog(
        database_url=database_url,
        dolt_conformance_source=dolt_conformance_source,
    ))


def import_sav(
    source: str | Path, /, *, database_url: Any, dataset_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
    **options: Any,
) -> Mapping[str, Any]:
    """Import one source file into one dedicated wide SQL table."""
    return result(import_dataset(
        source, database_url=database_url, dataset_id=dataset_id,
        dolt_conformance_source=dolt_conformance_source,
        **options,
    ))


def export_sav(
    *, database_url: Any, dataset_id: str, destination: str | Path,
    dolt_conformance_source: DoltConformanceSource | None = None,
    **options: Any,
) -> Mapping[str, Any]:
    """Export one database-resident conforming dataset to SAV/ZSAV."""
    return result(export_dataset(
        database_url=database_url, dataset_id=dataset_id,
        destination=destination,
        dolt_conformance_source=dolt_conformance_source,
        **options,
    ))


def validate(
    *,
    database_url: Any,
    dataset_id: str,
    dolt_conformance_source: DoltConformanceSource | None = None,
    **options: Any,
) -> Mapping[str, Any]:
    return result(validate_dataset(
        database_url=database_url,
        dataset_id=dataset_id,
        dolt_conformance_source=dolt_conformance_source,
        **options,
    ))


def list_datasets(*, database_url: Any, kind: str | None = None) -> Mapping[str, Any]:
    return result(_catalog_datasets(database_url=str(database_url), kind=kind))


def get_dataset(*, database_url: Any, dataset_id: str, kind: str) -> Mapping[str, Any]:
    return result(_catalog_dataset(database_url=str(database_url), dataset_id=dataset_id, kind=kind))


def register_sql_transformation(*, database_url: Any, **options: Any) -> Mapping[str, Any]:
    return result(_register_transformation(database_url=str(database_url), **options))


def execute_sql_transformation(*, database_url: Any, **options: Any) -> Mapping[str, Any]:
    return result(_execute_transformation(database_url=str(database_url), **options))


def derive_sql_dataset(*, database_url: Any, **options: Any) -> Mapping[str, Any]:
    return result(_derive_dataset(database_url=str(database_url), **options))


def apply_spss_in_place(
    *, database_url: Any, dataset_id: str, source_text: str,
    actor: str, expected_branch: str | None = None,
    expected_head: str | None = None,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> Mapping[str, Any]:
    """Apply bounded sequential SPSS syntax to one SQL dataset/table.

    Supports typed COMPUTE/IF predicates and dictionary metadata operations.
    """
    return result(_apply_spss_in_place(
        database_url=str(database_url),
        dataset_id=dataset_id,
        source_text=source_text,
        actor=actor,
        expected_branch=expected_branch,
        expected_head=expected_head,
        dolt_conformance_source=dolt_conformance_source,
    ))


def apply_transformation_plan_in_place(
    *, database_url: Any, dataset_id: str,
    plan: TransformationPlan | Mapping[str, Any],
    actor: str, expected_branch: str | None = None,
    expected_head: str | None = None,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> Mapping[str, Any]:
    """Apply a canonical plan to the same logical dataset and physical table."""
    return result(_apply_transformation_plan_in_place(
        database_url=str(database_url),
        dataset_id=dataset_id,
        plan=plan,
        actor=actor,
        expected_branch=expected_branch,
        expected_head=expected_head,
        dolt_conformance_source=dolt_conformance_source,
    ))


def install_in_place_transformation_schema(
    *,
    database_url: Any,
    dolt_conformance_source: DoltConformanceSource | None = None,
) -> None:
    """Install the compact apply-audit relation before the first apply."""
    _install_in_place_schema(
        database_url=str(database_url),
        dolt_conformance_source=dolt_conformance_source,
    )


def validate_derived(*, database_url: Any, derived_dataset_id: str) -> Mapping[str, Any]:
    return result(_validate_derived_dataset(
        database_url=str(database_url), derived_dataset_id=derived_dataset_id,
    ))


def retire_derived(*, database_url: Any, **options: Any) -> Mapping[str, Any]:
    return result(_retire_derived_dataset(database_url=str(database_url), **options))


def remove_derived_physical_relation(
    *, database_url: Any, **options: Any,
) -> Mapping[str, Any]:
    return result(_remove_derived_relation(database_url=str(database_url), **options))


def reconcile_derived_removals(
    *, database_url: Any, **options: Any,
) -> Mapping[str, Any]:
    return result(_reconcile_physical_removals(database_url=str(database_url), **options))


def reconcile_sql_transformation_runs(
    *, database_url: Any, **options: Any,
) -> Mapping[str, Any]:
    return result(_reconcile_started_runs(database_url=str(database_url), **options))
