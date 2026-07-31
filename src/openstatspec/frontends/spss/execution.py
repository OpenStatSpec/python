"""SPSS convenience adapter over the generic canonical-plan executor."""

from __future__ import annotations

from typing import Any

from ...sql.inplace_transform import (
    InPlacePlanSubmission,
    _run_in_place_submission,
    load_transformation_schema,
)
from . import SPSS_FRONTEND_CONTRACT
from .compiler import compile_spss_syntax


def apply_spss_in_place(
    *,
    database_url: str,
    dataset_id: str,
    source_text: str,
    actor: str,
    expected_branch: str | None = None,
    expected_head: str | None = None,
) -> dict[str, Any]:
    """Compile SPSS syntax and apply its canonical plan in one transaction."""

    def prepare(connection: Any, live_dataset_id: str) -> InPlacePlanSubmission:
        schema = load_transformation_schema(connection, live_dataset_id)
        compilation = compile_spss_syntax(
            source_text,
            schema,
            input_alias="parent",
        )
        return InPlacePlanSubmission(
            plan=compilation.plan,
            source_kind="spss_syntax",
            source_hash=compilation.source_hash,
            frontend_contract=SPSS_FRONTEND_CONTRACT,
        )

    return _run_in_place_submission(
        database_url=database_url,
        dataset_id=dataset_id,
        actor=actor,
        prepare=prepare,
        expected_branch=expected_branch,
        expected_head=expected_head,
    )
