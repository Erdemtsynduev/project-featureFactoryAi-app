"""JSON wire primitives: canonical spelling, digests and strict value readers.

Nothing here knows the engine's models, so every module (models included) can
depend on it without an import cycle.
"""

import hashlib
import json
from typing import cast

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _refuse_constant(value: str) -> object:
    raise ValueError(value)


def object_json(raw: str) -> dict[str, Json]:
    value: object = json.loads(raw, parse_constant=_refuse_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return cast(dict[str, Json], value)


def text(value: Json, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


def integer(value: Json, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be integer")
    return value


def flag(value: Json, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be boolean")
    return value


def mapping(value: Json) -> dict[str, Json]:
    if not isinstance(value, dict):
        raise ValueError("Expected mapping")
    return value


def sequence(value: Json) -> list[Json]:
    if not isinstance(value, list):
        raise ValueError("Expected array")
    return value


def number(value: Json) -> float:
    if type(value) not in (int, float):
        raise ValueError("Expected number")
    return float(cast(int | float, value))
