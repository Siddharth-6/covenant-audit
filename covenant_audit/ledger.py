"""Append-only, hash-chained audit ledger.

Sealed results, specs, policies, invalidation events and human reviews are appended as
entries; each entry hash covers its payload hash, its sequence number and the previous entry
hash, so editing or deleting history is detectable by `verify_chain()`.

Nothing is ever updated in place. "This result is stale" is not a flag on the result; it is a
later `InvalidationEvent` that names the affected result ids, so the original record, and the
hash chain over it, stay intact. Production considerations (not implemented here): anchor the
head hash in an external write-once store or sign checkpoints with a KMS key, and persist
entries in WORM storage instead of memory.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Literal

from .base import FrozenModel, Sha256Hex, canonical_json, sha256_hex
from .models import (
    CovenantKind,
    CovenantSpec,
    CovenantTestResult,
    ReviewDecision,
)
from .policy import BankPolicy

EntryKind = Literal["policy", "spec_registered", "result", "invalidation", "review"]
GENESIS = "0" * 64


class LedgerError(Exception):
    """An append was refused (duplicate, unknown reference, policy mismatch)."""


class LedgerTamperedError(Exception):
    """The hash chain does not verify."""


class LedgerEntry(FrozenModel):
    seq: int
    kind: EntryKind
    subject_id: str
    payload_hash: Sha256Hex
    prev_hash: Sha256Hex
    recorded_at: datetime
    entry_hash: Sha256Hex


class InvalidationEvent(FrozenModel):
    event_id: str
    kind: Literal["restatement", "spec_revoked", "retroactive_amendment"]
    reason: str
    occurred_at: datetime
    spec_id: str | None = None
    agreement_id: str | None = None
    covenant_kind: CovenantKind | None = None
    effective_from: date | None = None
    fact_ids: tuple[str, ...] = ()  # restated facts
    replaced_by_fact_ids: tuple[str, ...] = ()
    affected_result_ids: tuple[str, ...] = ()


class ReviewRecord(FrozenModel):
    result_id: str
    decision: ReviewDecision


class Checkpoint(FrozenModel):
    length: int
    head_hash: Sha256Hex


_PAYLOAD_MODELS = {
    "policy": BankPolicy,
    "spec_registered": CovenantSpec,
    "result": CovenantTestResult,
    "invalidation": InvalidationEvent,
    "review": ReviewRecord,
}


def _entry_hash(seq: int, kind: str, subject_id: str, payload_hash: str, prev_hash: str, at: datetime) -> str:
    return sha256_hex(f"{seq}|{kind}|{subject_id}|{payload_hash}|{prev_hash}|{at.isoformat()}")


class Ledger:
    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._payloads: list[str] = []

    # -- appending ----------------------------------------------------------

    def _append(self, kind: EntryKind, subject_id: str, model: FrozenModel, at: datetime | None) -> LedgerEntry:
        at = at or datetime.now(timezone.utc)
        payload = canonical_json(model)
        payload_hash = sha256_hex(payload)
        prev = self._entries[-1].entry_hash if self._entries else GENESIS
        seq = len(self._entries)
        entry = LedgerEntry(
            seq=seq, kind=kind, subject_id=subject_id, payload_hash=payload_hash, prev_hash=prev,
            recorded_at=at, entry_hash=_entry_hash(seq, kind, subject_id, payload_hash, prev, at),
        )
        self._entries.append(entry)
        self._payloads.append(payload)
        return entry

    def append_policy(self, policy: BankPolicy, at: datetime | None = None) -> LedgerEntry | None:
        """Store a policy version under its hash (idempotent). Results reference it by hash."""
        h = policy.content_hash()
        if any(e.kind == "policy" and e.subject_id == h for e in self._entries):
            return None
        return self._append("policy", h, policy, at)

    def append_spec(self, spec: CovenantSpec, at: datetime | None = None) -> LedgerEntry:
        return self._append("spec_registered", spec.spec_id, spec, at)

    def append_result(
        self, result: CovenantTestResult, policy: BankPolicy, at: datetime | None = None
    ) -> LedgerEntry:
        """Append a result. The supplied policy must be exactly the one the result was computed
        under, so a changed policy cannot be passed off as the historical one."""
        if policy.content_hash() != result.policy_hash or policy.thin_headroom_pct != result.thin_headroom_pct:
            raise LedgerError(f"result {result.result_id} was not computed under the supplied policy")
        if any(e.kind == "result" and e.subject_id == result.result_id for e in self._entries):
            raise LedgerError(f"result {result.result_id} is already recorded")
        if result.supersedes_result_id is not None and not self._has("result", result.supersedes_result_id):
            raise LedgerError(f"result {result.result_id} supersedes unknown result {result.supersedes_result_id}")
        self.append_policy(policy, at)
        return self._append("result", result.result_id, result, at or result.computed_at)

    def record_review(self, result_id: str, decision: ReviewDecision) -> LedgerEntry:
        if not self._has("result", result_id):
            raise LedgerError(f"cannot review unknown result {result_id}")
        return self._append("review", result_id, ReviewRecord(result_id=result_id, decision=decision), decision.decided_at)

    def _record_invalidation(self, event: InvalidationEvent, affected: Iterable[str]) -> InvalidationEvent:
        final = InvalidationEvent.model_validate({**dict(event), "affected_result_ids": tuple(sorted(set(affected)))})
        self._append("invalidation", final.event_id, final, final.occurred_at)
        return final

    def record_restatement(
        self, original_fact_ids: Iterable[str], replacement_fact_ids: Iterable[str], reason: str, at: datetime
    ) -> InvalidationEvent:
        """A financial restatement: the old facts, and every result computed from them, become stale.
        The specification is untouched; this is not a contract event."""
        originals = tuple(original_fact_ids)
        affected = [r.result_id for r in self.results() if set(r.fact_ids) & set(originals)]
        event = InvalidationEvent(
            event_id=f"INV-{len(self._entries)}", kind="restatement", reason=reason, occurred_at=at,
            fact_ids=originals, replaced_by_fact_ids=tuple(replacement_fact_ids),
        )
        return self._record_invalidation(event, affected)

    def record_spec_revocation(self, spec_id: str, reason: str, at: datetime) -> InvalidationEvent:
        """A spec found wrong: every result computed under it becomes stale."""
        affected = [r.result_id for r in self.results() if r.spec_id == spec_id]
        event = InvalidationEvent(
            event_id=f"INV-{len(self._entries)}", kind="spec_revoked", reason=reason, occurred_at=at, spec_id=spec_id
        )
        return self._record_invalidation(event, affected)

    def record_retroactive_amendment(self, spec: CovenantSpec, at: datetime) -> InvalidationEvent:
        """An amendment that the contract itself makes retroactive: earlier results under older
        specs, for periods it now governs, become stale. Non-retroactive amendments never call this."""
        affected = [
            r.result_id
            for r in self.results()
            if r.agreement_id == spec.agreement_id
            and r.kind is spec.kind
            and r.spec_id != spec.spec_id
            and r.test_date >= spec.effective_from
        ]
        event = InvalidationEvent(
            event_id=f"INV-{len(self._entries)}", kind="retroactive_amendment",
            reason=f"amendment {spec.spec_id} is retroactive to {spec.effective_from}", occurred_at=at,
            spec_id=spec.spec_id, agreement_id=spec.agreement_id, covenant_kind=spec.kind,
            effective_from=spec.effective_from,
        )
        return self._record_invalidation(event, affected)

    # -- reading ------------------------------------------------------------

    def _has(self, kind: str, subject_id: str) -> bool:
        return any(e.kind == kind and e.subject_id == subject_id for e in self._entries)

    def _load(self, kind: EntryKind) -> list:
        model = _PAYLOAD_MODELS[kind]
        return [model.model_validate_json(p) for e, p in zip(self._entries, self._payloads) if e.kind == kind]

    def results(self) -> list[CovenantTestResult]:
        return self._load("result")

    def get_result(self, result_id: str) -> CovenantTestResult:
        for r in self.results():
            if r.result_id == result_id:
                return r
        raise LedgerError(f"unknown result {result_id}")

    def invalidations(self) -> list[InvalidationEvent]:
        return self._load("invalidation")

    def reviews_for(self, result_id: str) -> list[ReviewRecord]:
        return [r for r in self._load("review") if r.result_id == result_id]

    def policy_for(self, result: CovenantTestResult) -> BankPolicy:
        for p in self._load("policy"):
            if p.content_hash() == result.policy_hash:
                return p
        raise LedgerError(f"policy {result.policy_hash[:12]} for result {result.result_id} is not recorded")

    def stale_result_ids(self) -> set[str]:
        return {rid for ev in self.invalidations() for rid in ev.affected_result_ids}

    def is_stale(self, result_id: str) -> bool:
        return result_id in self.stale_result_ids()

    def stale_fact_ids(self) -> set[str]:
        return {fid for ev in self.invalidations() if ev.kind == "restatement" for fid in ev.fact_ids}

    def superseded_result_ids(self) -> set[str]:
        return {r.supersedes_result_id for r in self.results() if r.supersedes_result_id}

    def pending_recompute(self) -> list[str]:
        """Stale results that have not yet been recomputed."""
        done = self.superseded_result_ids()
        return sorted(self.stale_result_ids() - done)

    def has_result_for_spec(self, spec_id: str) -> bool:
        return any(r.spec_id == spec_id for r in self.results())

    def current_results(self) -> list[CovenantTestResult]:
        """Results that are neither stale nor superseded."""
        out_of_date = self.stale_result_ids() | self.superseded_result_ids()
        return [r for r in self.results() if r.result_id not in out_of_date]

    def awaiting_review(self) -> list[CovenantTestResult]:
        """Current results that require a human decision and have none yet."""
        reviewed = {rec.result_id for rec in self._load("review")}
        return [r for r in self.current_results() if r.needs_review and r.result_id not in reviewed]

    # -- integrity ----------------------------------------------------------

    @property
    def head_hash(self) -> str:
        return self._entries[-1].entry_hash if self._entries else GENESIS

    def checkpoint(self) -> Checkpoint:
        return Checkpoint(length=len(self._entries), head_hash=self.head_hash)

    def verify_chain(self, checkpoint: Checkpoint | None = None) -> None:
        """Recompute every payload hash and entry hash. Raises LedgerTamperedError at the first
        broken link; with a previously saved checkpoint it also detects truncation and rewrites."""
        prev = GENESIS
        for i, (entry, payload) in enumerate(zip(self._entries, self._payloads)):
            if entry.seq != i:
                raise LedgerTamperedError(f"entry {i}: sequence number is {entry.seq}")
            if sha256_hex(payload) != entry.payload_hash:
                raise LedgerTamperedError(f"entry {i} ({entry.kind} {entry.subject_id}): payload was modified")
            if entry.prev_hash != prev:
                raise LedgerTamperedError(f"entry {i}: previous-hash link is broken")
            if entry.entry_hash != _entry_hash(i, entry.kind, entry.subject_id, entry.payload_hash, prev, entry.recorded_at):
                raise LedgerTamperedError(f"entry {i}: entry hash does not match its contents")
            prev = entry.entry_hash
        if checkpoint is not None:
            if len(self._entries) < checkpoint.length:
                raise LedgerTamperedError(
                    f"ledger has {len(self._entries)} entries but a checkpoint recorded {checkpoint.length}"
                )
            anchored = GENESIS if checkpoint.length == 0 else self._entries[checkpoint.length - 1].entry_hash
            if anchored != checkpoint.head_hash:
                raise LedgerTamperedError("history before the checkpoint was rewritten")
