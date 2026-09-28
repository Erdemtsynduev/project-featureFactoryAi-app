"""Deterministic context selection. No summarization model or silent acceptance trimming."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextRecord:
    id: str
    kind: str
    text: str
    revision: str
    source: str
    priority: int = 0
    required: bool = False


def assemble(
    records: tuple[ContextRecord, ...], revision: str, max_chars: int
) -> tuple[str, tuple[str, ...]]:
    eligible = [r for r in records if not r.revision or r.revision == revision]
    eligible.sort(key=lambda r: (not r.required, -r.priority, r.id))
    parts: list[str] = []
    omitted: list[str] = []
    size = 0
    for record in eligible:
        value = f"[{record.kind}: {record.id}; source={record.source}]\n{record.text}\n"
        if size + len(value) > max_chars:
            if record.required:
                raise ValueError("Required context does not fit")
            omitted.append(record.id)
            continue
        parts.append(value)
        size += len(value)
    omitted.extend(r.id for r in records if r not in eligible)
    return "".join(parts), tuple(omitted)
