"""HTTP execution adapter. The remote service owns durable deduplication and cancellation."""

from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from sdd_core.codec import canonical, encode, integer, number, object_json, result_load, text
from sdd_core.execution import (
    ExecutionHandle,
    ExecutionObservation,
    ExecutionRequest,
    ExecutionStatus,
    validate_observation,
)


class HttpExecutionBackend:
    def __init__(self, identifier: str, url: str, timeout: float = 5) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Execution endpoint must be an HTTP URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Endpoint cannot contain credentials, query or fragment")
        self.id, self.url, self.timeout = identifier, url.rstrip("/"), timeout

    def _call(self, method: str, path: str, body: str | None = None) -> str:
        request = Request(
            self.url + path,
            method=method,
            data=None if body is None else body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            payload = bytes(response.read(1_048_577))
        if len(payload) > 1_048_576:
            raise ValueError("Execution response exceeds limit")
        return payload.decode("utf-8")

    def _handle(self, raw: str) -> ExecutionHandle:
        doc = object_json(raw)
        return ExecutionHandle(
            text(doc.get("backend"), "backend"),
            text(doc.get("id"), "id"),
            integer(doc.get("generation"), "generation"),
        )

    @staticmethod
    def _path(handle: ExecutionHandle) -> str:
        return f"/executions/{quote(handle.id, safe='')}/{handle.generation}"

    def start(self, request: ExecutionRequest) -> ExecutionHandle:
        handle = self._handle(self._call("POST", "/executions", canonical(encode(request))))
        if handle != ExecutionHandle(self.id, request.id, request.generation):
            raise ValueError("Remote execution changed request identity")
        return handle

    def reconcile(self, handle: ExecutionHandle) -> ExecutionObservation:
        doc = object_json(self._call("GET", self._path(handle)))
        received = self._handle(canonical(doc.get("handle")))
        status = doc.get("status")
        checked: ExecutionStatus
        match status:
            case "running" | "completed" | "terminated" | "unknown":
                checked = status
            case _:
                raise ValueError("Invalid remote execution status")
        observation = ExecutionObservation(
            received,
            checked,
            None if doc.get("result") is None else result_load(canonical(doc["result"])),
            text(doc.get("reason", ""), "reason"),
            None if doc.get("completed_at") is None else number(doc["completed_at"]),
        )
        validate_observation(handle, observation)
        return observation

    def cancel(self, handle: ExecutionHandle) -> None:
        self._call("POST", self._path(handle) + "/cancel", "{}")
