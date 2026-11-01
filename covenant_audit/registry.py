"""Effective-dated registry of approved covenant specs.

A contract amendment is a *new spec version* with its own effective date. The registry never
overwrites or edits an earlier version; it chooses, for each test date, the version that governed
that date. A new version applies only to periods on or after its `effective_from`, unless the
contract itself makes it retroactive (then the spec must cite that clause and earlier results for
the affected periods are invalidated, not silently changed).

A financial restatement is a different event and is handled by the ledger, not here: the spec is
unchanged, but the facts and results derived from the restated figures become stale.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from .ledger import Ledger
from .models import CovenantKind, CovenantSpec
from .verification import SourceVerifier


class RegistryError(Exception):
    """A spec was refused (not approved, wrong version chain, ambiguous effective date)."""


class NoApplicableSpecError(RegistryError):
    """No registered spec governs the requested test date."""


class SpecRevokedError(RegistryError):
    """The spec that governs this date was revoked and no corrected spec has been approved yet."""


Key = tuple[str, CovenantKind]


class SpecRegistry:
    def __init__(self, verifier: SourceVerifier, ledger: Ledger | None = None) -> None:
        self._verifier = verifier
        self._ledger = ledger
        self._versions: dict[Key, list[CovenantSpec]] = {}
        self._revoked: dict[str, str] = {}

    def versions(self, agreement_id: str, kind: CovenantKind) -> tuple[CovenantSpec, ...]:
        return tuple(self._versions.get((agreement_id, kind), ()))

    def is_revoked(self, spec_id: str) -> bool:
        return spec_id in self._revoked

    def register(self, spec: CovenantSpec, at: datetime | None = None) -> None:
        """Add an approved spec as the next version of its (agreement, covenant) chain."""
        at = at or datetime.now(timezone.utc)
        self._verifier.check_spec(spec, require_approved=True)
        chain = self._versions.setdefault((spec.agreement_id, spec.kind), [])
        if any(s.spec_id == spec.spec_id for s in chain):
            raise RegistryError(f"spec {spec.spec_id} is already registered; versions are immutable")
        if not chain:
            if spec.version != 1 or spec.supersedes is not None:
                raise RegistryError("the first spec of a covenant must be version 1 and supersede nothing")
        else:
            last = chain[-1]
            if spec.version != last.version + 1 or spec.supersedes != last.spec_id:
                raise RegistryError(
                    f"spec {spec.spec_id} must be version {last.version + 1} and supersede {last.spec_id}"
                )
            last_valid = next((s for s in reversed(chain) if s.spec_id not in self._revoked), None)
            correcting = last.spec_id in self._revoked
            if last_valid is not None and spec.effective_from <= last_valid.effective_from:
                raise RegistryError(
                    f"effective_from {spec.effective_from} must be after {last_valid.effective_from}, "
                    f"the effective date of the version it amends"
                )
            if not correcting and spec.executed_on is None:
                raise RegistryError("an amendment must record the date it was executed (executed_on)")
        chain.append(spec)
        if self._ledger is not None:
            self._ledger.append_spec(spec, at)
            for approval in spec.approvals:
                if approval.attestation is not None:
                    self._ledger.append_approval(approval.attestation, at)
            if spec.is_retroactive:
                self._ledger.record_retroactive_amendment(spec, at)

    def revoke(self, spec_id: str, reason: str, at: datetime | None = None) -> None:
        """Mark a spec as wrong. Periods it governed are blocked until a corrected spec is registered,
        and results computed under it become stale. The earlier version is deliberately not reused."""
        at = at or datetime.now(timezone.utc)
        known = {s.spec_id for chain in self._versions.values() for s in chain}
        if spec_id not in known:
            raise RegistryError(f"unknown spec {spec_id}")
        self._revoked[spec_id] = reason
        if self._ledger is not None:
            self._ledger.record_spec_revocation(spec_id, reason, at)

    def spec_for(self, agreement_id: str, kind: CovenantKind, test_date: date) -> CovenantSpec:
        """The spec governing `test_date`: the latest version whose effective_from is on or before it."""
        chain = self._versions.get((agreement_id, kind), [])
        replaced = {s.supersedes for s in chain if s.supersedes}
        candidates = [s for s in chain if not (s.spec_id in self._revoked and s.spec_id in replaced)]
        applicable = [s for s in candidates if s.effective_from <= test_date]
        if not applicable:
            raise NoApplicableSpecError(f"no spec for {agreement_id}/{kind.value} governs {test_date}")
        spec = max(applicable, key=lambda s: (s.effective_from, s.version))
        if spec.spec_id in self._revoked:
            raise SpecRevokedError(
                f"spec {spec.spec_id} was revoked ({self._revoked[spec.spec_id]}); "
                f"a corrected approved spec is required before {test_date} can be tested"
            )
        return spec
