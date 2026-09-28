import pytest
from sdd_core.execution import ExecutionRequest
from sdd_runtime.plugins import load_executor


def test_separately_installed_executor_needs_only_public_core():
    backend = load_executor("example")
    request = ExecutionRequest("one", 1, "operation", "{}", 100)
    handle = backend.start(request)
    assert backend.start(request) == handle
    assert backend.reconcile(handle).status == "running"
    backend.cancel(handle)
    assert backend.reconcile(handle).status == "terminated"
    assert load_executor("example").reconcile(handle).status == "unknown"
    with pytest.raises(ValueError, match="not installed"):
        load_executor("not-an-installed-executor")
