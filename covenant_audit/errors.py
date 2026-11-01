"""Exception hierarchy shared by verification and approval (kept separate to avoid import cycles)."""


class VerificationError(Exception):
    """Base class for every verification failure."""


class SealError(VerificationError):
    """A seal is missing, forged, or no longer matches the artefact it was minted for."""
