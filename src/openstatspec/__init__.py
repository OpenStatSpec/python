"""Public API for the OpenStatSpec Python reference implementation."""

from .api import (
    apply_spss_in_place, capabilities, capability_matrix, derive_sql_dataset,
    execute_sql_transformation,
    export_sav, get_dataset, import_sav, inspect,
    install_in_place_transformation_schema, list_datasets,
    register_sql_transformation, reconcile_derived_removals,
    reconcile_sql_transformation_runs,
    remove_derived_physical_relation, retire_derived, validate, validate_derived,
)
from .core import CapabilityDeclaration, LossReport, UnsupportedOperationError
from .sql.workflow import TransformationError
from .transform import (
    SpssFrontendCompilation, TransformationFrontendError,
    VariableDefinition, VariableSchema, compile_spss_syntax,
)

__all__ = [
    "CapabilityDeclaration", "LossReport", "SpssFrontendCompilation",
    "TransformationError", "TransformationFrontendError",
    "VariableDefinition", "VariableSchema",
    "UnsupportedOperationError", "capabilities", "capability_matrix",
    "apply_spss_in_place", "compile_spss_syntax", "derive_sql_dataset",
    "execute_sql_transformation", "export_sav",
    "get_dataset", "import_sav", "inspect",
    "install_in_place_transformation_schema", "list_datasets",
    "register_sql_transformation", "reconcile_derived_removals",
    "reconcile_sql_transformation_runs",
    "remove_derived_physical_relation", "retire_derived", "validate",
    "validate_derived",
]
