"""Bank operating policy, kept strictly separate from contractual covenant terms.

A covenant threshold comes from the credit agreement and lives in `CovenantSpec`.
How cautious the bank is about a thin margin, how often it samples clean passes and
which tie-outs it insists on are the bank's choice and live here. Every result records
the policy hash it was computed under, so changing policy later cannot silently
re-interpret a historical result: a new policy is a new version with a new hash.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from .base import FrozenModel, Key, Money, content_hash, sha256_hex


class TieOutRequirement(FrozenModel):
    """Tie-outs that must be present and passing before a fact for `input_name` is trusted."""

    input_name: Key
    tie_out_names: tuple[str, ...] = Field(min_length=1)


class BankPolicy(FrozenModel):
    policy_id: str
    version: int = Field(ge=1)
    thin_headroom_pct: Money  # fraction of the threshold, e.g. 0.10 = 10%
    clean_pass_sample_pct: Money  # fraction of clean passes routed to human review
    min_ocr_confidence: Money = Decimal("0.90")  # OCR evidence below this forces review
    required_tie_outs: tuple[TieOutRequirement, ...] = ()
    allow_ungrounded_tie_outs: bool = False  # True only for tests; production tie-outs must cite components

    @field_validator("thin_headroom_pct")
    @classmethod
    def _thin_range(cls, v: Decimal) -> Decimal:
        if not (Decimal(0) < v < Decimal(1)):
            raise ValueError("thin_headroom_pct must be strictly between 0 and 1")
        return v

    @field_validator("clean_pass_sample_pct", "min_ocr_confidence")
    @classmethod
    def _unit_interval(cls, v: Decimal) -> Decimal:
        if not (Decimal(0) <= v <= Decimal(1)):
            raise ValueError("value must be between 0 and 1")
        return v

    @model_validator(mode="after")
    def _unique_requirements(self) -> BankPolicy:
        names = [r.input_name for r in self.required_tie_outs]
        if len(set(names)) != len(names):
            raise ValueError("duplicate tie-out requirement for an input")
        return self

    def content_hash(self) -> str:
        return content_hash(self)

    def required_tie_outs_for(self, input_name: str) -> tuple[str, ...]:
        for r in self.required_tie_outs:
            if r.input_name == input_name:
                return r.tie_out_names
        return ()

    def is_sampled(self, key: str) -> bool:
        """Deterministic pseudo-random sampling: the same (policy, key) always gives the
        same answer, so an auditor can reproduce why a clean pass was or was not sampled."""
        bucket = int(sha256_hex(f"{self.content_hash()}|{key}")[:8], 16) % 10_000
        return Decimal(bucket) < self.clean_pass_sample_pct * Decimal(10_000)
