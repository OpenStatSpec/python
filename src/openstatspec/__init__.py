"""Public API for the OpenStatSpec Python reference implementation."""

from .api import (
    capabilities, capability_matrix, derive_sql_dataset, execute_sql_transformation,
    export_sav, get_dataset, import_sav, inspect, list_datasets,
    register_sql_transformation, reconcile_derived_removals,
    reconcile_sql_transformation_runs,
    remove_derived_physical_relation, retire_derived, validate, validate_derived,
)
from .core import CapabilityDeclaration, LossReport, UnsupportedOperationError
from .sql.workflow import TransformationError

__all__ = [
    "CapabilityDeclaration", "LossReport", "TransformationError",
    "UnsupportedOperationError", "capabilities", "capability_matrix",
    "derive_sql_dataset", "execute_sql_transformation", "export_sav",
    "get_dataset", "import_sav", "inspect", "list_datasets",
    "register_sql_transformation", "reconcile_derived_removals",
    "reconcile_sql_transformation_runs",
    "remove_derived_physical_relation", "retire_derived", "validate",
    "validate_derived",
]
