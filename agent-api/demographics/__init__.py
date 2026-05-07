"""Demographics — pre- and post-extraction patient identity machinery.

* :mod:`demographics.check` — post-extraction §5.6 wrong-patient detection.
* :mod:`demographics.resolver` — pre-extraction patient resolver (Slice 9.2).
* :mod:`demographics.quarantine` — quarantine state machine (Slice 9.2).
"""

from demographics.check import DemographicCheckResult, check_demographics
from demographics.quarantine import (
    CLAIM_TTL_SECONDS,
    QUARANTINE_TTL_SECONDS,
    QuarantineError,
    TransitionResult,
)
from demographics.resolver import (
    ParsedIdentity,
    Pass,
    Quarantine,
    ResolverOutcome,
    resolve,
)

__all__ = [
    "CLAIM_TTL_SECONDS",
    "DemographicCheckResult",
    "ParsedIdentity",
    "Pass",
    "QUARANTINE_TTL_SECONDS",
    "Quarantine",
    "QuarantineError",
    "ResolverOutcome",
    "TransitionResult",
    "check_demographics",
    "resolve",
]
