"""Validate this repository's Conventional Commits and attribution policy."""

import argparse
import re
from pathlib import Path

SUBJECT = re.compile(
    r"^(feat|fix|refactor|perf|test|docs|build|ci|chore|style|revert)(\([a-z0-9_-]+\))?!?: \S.+$"
)
ATTRIBUTION = re.compile(
    r"co-authored-by\s*:|(?:generated|assisted|authored|written|created)[ -]by\s*:"
    r"|generated\s+(?:with|by|using)\b|🤖"
    r"|(?:written|created|authored|assisted|made)\s+(?:by|with|using)\s+(?:an?\s+)?(?:ai|agent|llm|codex|claude|chatgpt|copilot)\b"
    r"|signed-off-by\s*:.*(?:bot|agent|codex|claude|openai|anthropic)"
    r"|noreply@(?:openai|anthropic)\.com",
    re.IGNORECASE,
)


def check(message: str) -> tuple[str, ...]:
    lines = message.lstrip("\ufeff").splitlines()
    errors = []
    subject = lines[0] if lines else ""
    if not SUBJECT.fullmatch(subject):
        errors.append("Use <type>(optional-scope): description")
    if len(subject) > 72 or subject.endswith("."):
        errors.append("Subject must be at most 72 characters, without a trailing period")
    if len(lines) > 1 and lines[1].strip():
        errors.append("Separate subject and body with a blank line")
    if ATTRIBUTION.search(message):
        errors.append("Tool/agent attribution is not allowed")
    return tuple(errors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    errors = check(parser.parse_args().file.read_text(encoding="utf-8"))
    print("\n".join(errors) if errors else "Commit message: PASS")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
