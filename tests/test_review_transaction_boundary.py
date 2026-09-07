"""Regression tests for bound commit checks and export dataset linkage."""

from sqlalchemy import create_engine

from openstatspec.sql import wide


def test_dolt_completion_identity_is_checked_before_transaction_commit(
    monkeypatch,
):
    engine = create_engine("sqlite://")
    state = {
        "database": "catalog",
        "active_branch": "main",
        "head": "abc123",
    }
    transaction_states = []

    def capture(connection, **_kwargs):
        transaction_states.append(connection.in_transaction())
        return state

    monkeypatch.setattr(wide, "_capture_dolt_state", capture)
    with wide._bound_catalog_transaction(
        engine=engine,
        profile_name="dolt",
        active={
            "working_set_binding": {
                "database": "catalog",
                "active_branch": "main",
            },
        },
        audit_relations=set(),
        phase="test",
    ):
        pass

    assert transaction_states == [False, True]


class _DoltResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one(self):
        return self.rows[0]

    def all(self):
        return self.rows


class _DoltDiffConnection:
    def __init__(self, summary):
        self.summary = summary

    def exec_driver_sql(self, statement):
        if "ACTIVE_BRANCH" in statement:
            return _DoltResult([{
                "database_name": "catalog",
                "active_branch": "main",
                "head_hash": "abc123",
            }])
        if "dolt_status" in statement:
            return _DoltResult([])
        return _DoltResult([self.summary])


def _dolt_summary(**changes):
    result = {
        "from_table_name": "operation",
        "to_table_name": "operation",
        "diff_type": "modified",
        "data_change": 1,
        "schema_change": 0,
    }
    result.update(changes)
    return result


def test_dolt_classifier_allows_owned_table_creation():
    added = _dolt_summary(
        from_table_name="", diff_type="added", schema_change=1,
    )
    for owned in (True, False):
        state = wide._capture_dolt_state(
            _DoltDiffConnection(added), profile_name="dolt",
            audit_relations={"operation"} if owned else set(),
        )
        for evidence in state["unrelated_working_set"]["diff_summaries"].values():
            assert evidence["rows"] == ([] if owned else [added])


def test_dolt_classifier_allows_only_same_table_audit_data_changes():
    allowed = wide._capture_dolt_state(
        _DoltDiffConnection(_dolt_summary()),
        profile_name="dolt",
        audit_relations={"operation", "fidelity_event"},
    )
    assert allowed is not None
    assert all(
        not evidence["rows"]
        for evidence in allowed["unrelated_working_set"]["diff_summaries"].values()
    )

    for unsafe in (
        _dolt_summary(schema_change=1),
        _dolt_summary(diff_type="dropped", to_table_name=None, schema_change=1),
        _dolt_summary(
            diff_type="renamed", to_table_name="fidelity_event", schema_change=1,
        ),
    ):
        classified = wide._capture_dolt_state(
            _DoltDiffConnection(unsafe),
            profile_name="dolt",
            audit_relations={"operation", "fidelity_event"},
        )
        assert classified is not None
        assert all(
            evidence["rows"] == [unsafe]
            for evidence in classified[
                "unrelated_working_set"
            ]["diff_summaries"].values()
        )


def test_dolt_completion_rejects_unrelated_working_set_changes():
    before = {
        "database": "catalog", "active_branch": "main", "head": "abc123",
        "unrelated_sha256": "before",
    }
    after = {**before, "unrelated_sha256": "after"}

    import pytest
    from openstatspec.core import UnsupportedOperationError

    with pytest.raises(UnsupportedOperationError, match="unrelated working-set"):
        wide._require_dolt_success_identity(before, after, phase="export audit")
