"""Pure canonical transformation models; no syntax or database adapter."""

from .errors import SourcePosition, SourceSpan, TransformationFrontendError
from .plan import (
    TRANSFORMATION_PLAN_SCHEMA_CHANGE_CONTRACT,
    TRANSFORMATION_PLAN_CONTRACT,
    AssignOperation, BooleanExpression, ComparisonExpression,
    ConditionalAssignOperation, CreateVariableOperation, DeleteVariableOperation,
    ExecuteOperation, Operand, PredicateExpression,
    RecodeMatch, RecodeOperation, RecodeResult, RecodeRule,
    ReplaceValueLabelsOperation, SetFormatOperation,
    SetMeasurementLevelOperation, SetVariableLabelOperation, TransformationPlan,
    TypedValue, ValueLabel, canonical_plan_hash, canonical_plan_json,
    transformation_plan_from_dict,
)
from .schema import (
    BoundTransformation, StorageKind, VariableDefinition, VariableSchema,
)
from .validation import bind_transformation_plan
__all__ = [
    "AssignOperation", "BooleanExpression", "ComparisonExpression",
    "ConditionalAssignOperation", "CreateVariableOperation", "DeleteVariableOperation",
    "PredicateExpression",
    "BoundTransformation",
    "RecodeMatch", "RecodeOperation", "RecodeResult",
    "RecodeRule", "ReplaceValueLabelsOperation", "SPSS_FRONTEND_CONTRACT",
    "SetFormatOperation", "SetMeasurementLevelOperation",
    "SetVariableLabelOperation", "SourcePosition", "SourceSpan", "StorageKind",
    "SpssFrontendCompilation",
    "SpssSyntaxProgram", "TRANSFORMATION_PLAN_CONTRACT", "TransformationFrontendError",
    "TransformationPlan", "TypedValue", "ValueLabel",
    "VariableDefinition",
    "VariableSchema", "bind_spss_syntax", "bind_transformation_plan",
    "canonical_plan_hash",
    "compile_spss_syntax",
    "canonical_plan_json", "normalize_spss_source", "parse_spss_syntax",
    "spss_source_hash", "tokenize_spss",
    "transformation_plan_from_dict",
]


_SPSS_COMPAT_EXPORTS = {
    "SPSS_FRONTEND_CONTRACT",
    "SpssFrontendCompilation",
    "SpssSyntaxProgram",
    "bind_spss_syntax",
    "compile_spss_syntax",
    "normalize_spss_source",
    "parse_spss_syntax",
    "spss_source_hash",
    "tokenize_spss",
}


def __getattr__(name: str):
    """Temporarily preserve the v0.3 SPSS re-export surface."""
    if name in _SPSS_COMPAT_EXPORTS:
        from ..frontends import spss

        return getattr(spss, name)
    raise AttributeError(name)
