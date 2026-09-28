import json
import sys

from sdd_core.models import Attempt, Step
from sdd_core.sdk import Packet
from sdd_providers.handlers import CliHandler


def test_claude_total_input_includes_both_cache_categories(tmp_path):
    folder = tmp_path / ".sdd-engine" / "attempt"
    folder.mkdir(parents=True)
    (folder / "stdout.log").write_text(
        json.dumps(
            {
                "structured_output": {
                    "outcome": "done",
                    "reason": "ok",
                    "standards": True,
                    "specification": True,
                },
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20,
                },
            }
        )
    )
    handler = CliHandler("claude", sys.executable)
    packet = Packet(
        "run",
        Attempt("a", "s", 1, 0, 100, "rev"),
        Step("s", "agent", "claude"),
        str(tmp_path),
        str(folder),
        "",
    )
    result = handler.collect(packet, 0, "rev")
    assert result.usage.input_tokens == 123
    assert result.usage.cache_read == 100 and result.usage.cache_write == 20
