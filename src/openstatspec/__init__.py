"""Public API for the OpenStatSpec Python reference implementation."""

from .api import (
    capabilities, capability_matrix, derive_sql_dataset, dolt_state_snapshot, execute_sql_transformation,
    export_sav, get_dataset, import_sav, initialize_catalog, inspect, list_datasets,
    register_sql_transformation, reconcile_derived_removals,
    reconcile_sql_transformation_runs,
    remove_derived_physical_relation, retire_derived, validate, validate_derived,
)
from .core import CapabilityDeclaration, LossReport, UnsupportedOperationError
from .spss import compare_sav_semantics
from .sql import DoltConformanceSource
from .sql.workflow import TransformationError

__all__ = [
    "CapabilityDeclaration", "DoltConformanceSource", "LossReport", "TransformationError",
    "UnsupportedOperationError", "capabilities", "capability_matrix", "compare_sav_semantics",
    "derive_sql_dataset", "dolt_state_snapshot", "execute_sql_transformation", "export_sav", "get_dataset",
    "import_sav", "initialize_catalog", "inspect", "list_datasets",
    "register_sql_transformation", "reconcile_derived_removals",
    "reconcile_sql_transformation_runs",
    "remove_derived_physical_relation", "retire_derived", "validate",
    "validate_derived",
]
