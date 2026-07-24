import openstatspec
import pytest


def test_capabilities_declares_no_profile() -> None:
    declared = openstatspec.capabilities()
    assert declared["formats"] == {}
    assert declared["database_profiles"] == {}
    assert all(enabled is False for enabled in declared["operations"].values())


@pytest.mark.parametrize(
    "operation, args, kwargs",
    [
        (openstatspec.inspect, ("example.sav",), {}),
        (openstatspec.import_sav, ("example.sav",), {"database_url": "sqlite://", "dataset_id": "example"}),
        (openstatspec.export_sav, (), {"database_url": "sqlite://", "dataset_id": "example", "destination": "example.sav"}),
        (openstatspec.validate, (), {"database_url": "sqlite://", "dataset_id": "example"}),
    ],
)
def test_unsupported_operations_fail_explicitly(operation, args, kwargs) -> None:
    with pytest.raises(openstatspec.UnsupportedOperationError):
        operation(*args, **kwargs)
