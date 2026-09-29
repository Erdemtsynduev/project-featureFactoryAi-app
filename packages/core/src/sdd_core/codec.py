"""Strict serialization at the boundary; no arbitrary class loading.

Documents decode through one reader driven by the dataclass declarations in
`sdd_core.models`: field types, defaults and closed vocabularies are declared
once, and a field added to a model is read without a second hand-written copy.
"""

from dataclasses import MISSING, Field, fields, is_dataclass, replace
from functools import cache
from types import NoneType, UnionType
from typing import (
    Any,
    Literal,
    TypeAliasType,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from sdd_core.machine import legacy_cause
from sdd_core.models import Result, Run, Step, Workflow
from sdd_core.wire import (
    Json,
    canonical,
    digest,
    flag,
    integer,
    mapping,
    number,
    object_json,
    sequence,
    text,
)

__all__ = [
    "canonical",
    "decode",
    "digest",
    "encode",
    "flag",
    "integer",
    "mapping",
    "number",
    "object_json",
    "result_json",
    "result_load",
    "run_json",
    "run_load",
    "sequence",
    "text",
    "workflow_json",
    "workflow_load",
]


@cache
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


def _value(hint: Any, raw: Json, name: str, closed: frozenset[type]) -> Any:
    if isinstance(hint, TypeAliasType):
        hint = hint.__value__
    origin = get_origin(hint)
    if origin in (Union, UnionType):
        options = get_args(hint)
        if raw is None and NoneType in options:
            return None
        (inner,) = (option for option in options if option is not NoneType)
        return _value(inner, raw, name, closed)
    if origin is Literal:
        if raw not in get_args(hint):
            raise ValueError(f"Unknown {name}")
        return raw
    if origin is tuple:
        items = sequence(raw)
        args = get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_value(args[0], item, name, closed) for item in items)
        if len(items) != len(args):
            raise ValueError(f"{name} must have {len(args)} elements")
        return tuple(_value(arg, item, name, closed) for arg, item in zip(args, items, strict=True))
    if origin is dict or hint is dict:
        return mapping(raw)
    if is_dataclass(hint) and isinstance(hint, type):
        return decode(hint, raw, closed)
    if hint is str:
        return text(raw, name)
    if hint is bool:
        return flag(raw, name)
    if hint is int:
        return integer(raw, name)
    if hint is float:
        return number(raw)
    raise TypeError(f"No wire reader for {hint!r}")


def _flat(item: Field[Any]) -> bool:
    return item.metadata.get("wire") == "flat"


def _names(cls: type) -> set[str]:
    """Keys a document of `cls` may carry, flattened values included."""
    names: set[str] = set()
    for item in fields(cls):
        names |= _names(_hints(cls)[item.name]) if _flat(item) else {item.name}
    return names


def decode[T](cls: type[T], raw: Json, closed: frozenset[type] = frozenset()) -> T:
    """Read a dataclass from JSON: declared types, declared defaults, nothing else.

    Classes in `closed` refuse fields they do not declare; others ignore them, so
    documents written by a newer release stay readable where that is safe. A field
    declared with `models.FLAT` metadata reads its value's fields at this level.
    """
    document = mapping(raw)
    kind = cast(type, cls)
    if cls in closed and document.keys() - _names(kind):
        raise ValueError(f"Unknown {cls.__name__.lower()} fields")
    hints = _hints(kind)
    values: dict[str, Any] = {}
    for item in fields(kind):
        name = item.name
        if _flat(item):
            values[name] = decode(hints[name], document, closed)
        elif name in document:
            values[name] = _value(hints[name], document[name], name, closed)
        elif item.default is MISSING and item.default_factory is MISSING:
            raise ValueError(f"{cls.__name__} needs {name}")
    return cls(**values)


def encode(value: object) -> dict[str, Any]:
    """The wire document of a model: tuples as lists, `FLAT` values merged upward.

    This is the one spelling of a model outside the process: storage, HTTP answers
    and command responses all use it, so they agree with `decode`.
    """
    if not is_dataclass(value) or isinstance(value, type):
        raise TypeError("Expected a model instance")
    result: dict[str, Any] = {}
    for item in fields(value):
        part = getattr(value, item.name)
        if _flat(item):
            result.update(encode(part))
        else:
            result[item.name] = _plain(part)
    return result


def _plain(value: object) -> Json:
    if is_dataclass(value) and not isinstance(value, type):
        return encode(value)
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return cast(Json, value)


def workflow_json(workflow: Workflow) -> str:
    return canonical(encode(workflow))


def workflow_load(raw: str) -> Workflow:
    return decode(Workflow, object_json(raw), frozenset({Workflow, Step}))


def run_json(run: Run) -> str:
    return canonical(encode(run))


def run_load(raw: str) -> Run:
    """A stored run; one written before `cause` existed gets it from its reason."""
    stored = object_json(raw)
    run = decode(Run, stored)
    return run if "cause" in stored else replace(run, cause=legacy_cause(run))


def result_json(result: Result) -> str:
    return canonical(encode(result))


def result_load(raw: str) -> Result:
    return decode(Result, object_json(raw), frozenset({Result}))
