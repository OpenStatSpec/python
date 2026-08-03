"""Public API for the OpenStatSpec Python reference implementation."""

from .api import (
    apply_spss_in_place, apply_transformation_plan_in_place,
    capabilities, capability_matrix, derive_sql_dataset,
    execute_sql_transformation,
    export_sav, get_dataset, import_sav, inspect,
    install_in_place_transformation_schema, list_datasets,
    register_sql_transformation, reconcile_derived_removals,
    reconcile_sql_transformation_runs,
    remove_derived_physical_relation, retire_derived, validate, validate_derived,
)
from .core import CapabilityDeclaration, LossReport, UnsupportedOperationError
from .sql.workflow import TransformationError
from .frontends.spss import SpssFrontendCompilation, compile_spss_syntax
from .transform import (
    AssignOperation, BooleanExpression, ComparisonExpression,
    ConditionalAssignOperation, ExecuteOperation, Operand, PredicateExpression,
    RecodeMatch, RecodeOperation, RecodeResult, RecodeRule,
    ReplaceValueLabelsOperation, SetFormatOperation,
    SetMeasurementLevelOperation, SetVariableLabelOperation,
    TransformationFrontendError, TransformationPlan, TypedValue, ValueLabel,
    VariableDefinition, VariableSchema, transformation_plan_from_dict,
)

__all__ = [
    "AssignOperation", "BooleanExpression", "ComparisonExpression",
    "ConditionalAssignOperation", "ExecuteOperation", "Operand",
    "PredicateExpression",
    "CapabilityDeclaration", "LossReport", "SpssFrontendCompilation",
    "TransformationError", "TransformationFrontendError",
    "RecodeMatch", "RecodeOperation", "RecodeResult", "RecodeRule",
    "ReplaceValueLabelsOperation", "SetFormatOperation",
    "SetMeasurementLevelOperation", "SetVariableLabelOperation",
    "TransformationPlan", "TypedValue", "ValueLabel",
    "VariableDefinition", "VariableSchema", "transformation_plan_from_dict",
    "UnsupportedOperationError", "capabilities", "capability_matrix",
    "apply_spss_in_place", "apply_transformation_plan_in_place",
    "compile_spss_syntax", "derive_sql_dataset",
    "execute_sql_transformation", "export_sav",
    "get_dataset", "import_sav", "inspect",
    "install_in_place_transformation_schema", "list_datasets",
    "register_sql_transformation", "reconcile_derived_removals",
    "reconcile_sql_transformation_runs",
    "remove_derived_physical_relation", "retire_derived", "validate",
    "validate_derived",
]
