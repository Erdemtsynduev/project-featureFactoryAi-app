"""Failures as annotations of a GitHub Actions run, readable without opening its log."""

import os

# GitHub keeps the tail of a long message readable; the end of a failure says why.
MESSAGE_CHARS = 1500


def enabled() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def error(title: str, text: str, file: str = "", line: int = 0) -> str:
    """The workflow command for one error annotation, on one line as GitHub reads it."""
    where = f" file={file},line={line}," if file else " "
    name = title.replace("%", "%25").replace(",", "%2C").replace("::", " ")
    body = text[-MESSAGE_CHARS:].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error{where}title={name}::{body}"
