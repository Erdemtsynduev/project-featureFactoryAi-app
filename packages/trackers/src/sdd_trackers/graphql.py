"""A small GraphQL-over-HTTPS client on the standard library, for tracker adapters."""

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

type Transport = Callable[[str, Mapping[str, object]], dict[str, Any]]

DETAIL_CHARS = 500


class TrackerError(RuntimeError):
    """The tracker refused a request or could not be reached. Never carries secrets."""


class GraphQL:
    """POST `{query, variables}` to `endpoint`; `headers()` is read per request, so a
    token comes from the environment at call time and is never stored."""

    def __init__(
        self, endpoint: str, headers: Callable[[], dict[str, str]], timeout: float = 20.0
    ) -> None:
        self.endpoint, self.headers, self.timeout = endpoint, headers, timeout

    def __call__(self, query: str, variables: Mapping[str, object]) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": dict(variables)}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json", **self.headers()},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read()[:DETAIL_CHARS].decode("utf-8", "replace")
            raise TrackerError(f"HTTP {error.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TrackerError(f"Unreachable: {error}") from None
        if not isinstance(payload, dict):
            raise TrackerError("Malformed response")
        errors = payload.get("errors")
        if errors:
            messages = [str(e.get("message", e)) for e in errors if isinstance(e, dict)]
            raise TrackerError("; ".join(messages)[:DETAIL_CHARS] or "GraphQL error")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise TrackerError("Response without data")
        return data
