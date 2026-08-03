"""Public API for the OpenStatSpec Python reference implementation."""

from .api import (
    apply_spss_in_place, apply_transformation_plan_in_place,
    capabilities, capability_matrix, derive_sql_dataset, dolt_state_snapshot, execute_sql_transformation,
    export_sav, get_dataset, import_sav, initialize_catalog, inspect, list_datasets,
    install_in_place_transformation_schema,
    register_sql_transformation, reconcile_derived_removals,
    reconcile_sql_transformation_runs,
    remove_derived_physical_relation, retire_derived, validate, validate_derived,
)
from .core import CapabilityDeclaration, LossReport, UnsupportedOperationError
from .frontends.spss import SpssFrontendCompilation, compile_spss_syntax
from .spss import compare_sav_semantics
from .sql import DoltConformanceSource
from .sql.workflow import TransformationError
from .transform import (
    RecodeMatch, RecodeOperation, RecodeResult, RecodeRule,
    ReplaceValueLabelsOperation, SetVariableLabelOperation,
    TransformationFrontendError, TransformationPlan, TypedValue, ValueLabel,
    VariableDefinition, VariableSchema, transformation_plan_from_dict,
)

__all__ = [
    "CapabilityDeclaration", "DoltConformanceSource", "LossReport", "TransformationError",
    "SpssFrontendCompilation", "TransformationFrontendError",
    "RecodeMatch", "RecodeOperation", "RecodeResult", "RecodeRule",
    "ReplaceValueLabelsOperation", "SetVariableLabelOperation",
    "TransformationPlan", "TypedValue", "ValueLabel",
    "VariableDefinition", "VariableSchema", "transformation_plan_from_dict",
    "apply_spss_in_place", "apply_transformation_plan_in_place",
    "compile_spss_syntax", "install_in_place_transformation_schema",
    "UnsupportedOperationError", "capabilities", "capability_matrix", "compare_sav_semantics",
    "derive_sql_dataset", "dolt_state_snapshot", "execute_sql_transformation", "export_sav", "get_dataset",
    "import_sav", "initialize_catalog", "inspect", "list_datasets",
    "register_sql_transformation", "reconcile_derived_removals",
    "reconcile_sql_transformation_runs",
    "remove_derived_physical_relation", "retire_derived", "validate",
    "validate_derived",
]
