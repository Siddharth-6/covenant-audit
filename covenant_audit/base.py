"""Shared primitives: frozen base model, Decimal-only money type, canonical hashing."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Any, Iterable

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

SCHEMA_VERSION = "2"


def _reject_float(value: Any) -> Any:
    """Binary floats cannot represent most decimal amounts exactly (0.1 + 0.2 != 0.3),
    so they are refused at the model boundary instead of being silently converted."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(
            "binary floats and booleans are not accepted for financial values; "
            "pass a Decimal, an int or a numeric string such as '4.50'"
        )
    return value


Money = Annotated[Decimal, BeforeValidator(_reject_float), Field(allow_inf_nan=False)]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class FrozenModel(BaseModel):
    """Immutable, closed-schema base class.

    Sequence fields are declared as tuples across the package, so approved artefacts are
    deeply immutable (a frozen model holding a list can still be mutated in place)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def canonical_json(model: BaseModel, exclude: Iterable[str] | None = None) -> str:
    """Deterministic JSON: sorted keys, no whitespace, Decimals as exact strings."""
    data = model.model_dump(mode="json", exclude=set(exclude) if exclude else None)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(model: BaseModel, exclude: Iterable[str] | None = None) -> str:
    return sha256_hex(canonical_json(model, exclude))


def evolve(model: BaseModel, **updates: Any) -> Any:
    """Return a re-validated copy. Unlike `model_copy(update=...)`, validators run again,
    so a copy can never skip the invariants that the original had to satisfy."""
    return type(model).model_validate({**dict(model), **updates})
