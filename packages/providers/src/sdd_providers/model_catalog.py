"""Version-sensitive catalog formats belong to adapters, never to the scheduler."""

import re


def opencode_models(content: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                line.strip()
                for line in content.splitlines()
                if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./:#-]+", line.strip())
            }
        )
    )


def cursor_models(content: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                match.group(1)
                for line in content.splitlines()
                if (match := re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.:#-]*) - .+", line.strip()))
            }
        )
    )
