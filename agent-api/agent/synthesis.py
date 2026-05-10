"""Post-approval RAG synthesis (Phase 2 Step 1).

Generates a 4-section clinical briefing from approved facts + retrieved
guideline snippets after the physician approves a document. The briefing has
a hard citation contract: every claim must reference an enumerable
citation_id drawn from the approved facts or the retrieved guidelines.

The citation_id grammar is the contract for synthesis grounding. Any future
fact source (problem list, FHIR Condition, etc.) requires extending the
prefix set in one place, this module, not scattered through the prompt or
the rubric. See PHASE_1_SYNTHESIS_DESIGN section in the team brief.

Three prefixes (closed set):
  * ``guideline:{chunk_id}``       , retrieved RAG passages
  * ``fact:obs:{observation_id}``  , approved ``copilot_observations`` rows
  * ``fact:intake:{field_name}``   , approved ``IntakeFormField`` rows

The orchestration entrypoint is :func:`synthesize`. It NEVER raises into the
caller; on terminal failure it returns a :class:`SynthesisOutcome` with
``output=None`` and a ``fallback_reason``, and the caller is responsible for
falling back to the deterministic recap.

PHI handling: per W1_ARCHITECTURE §5.2, this module never logs prompt text,
completion text, approved-fact ``value_repr`` strings, or exception
messages (which may carry PHI fragments). It logs request_id (ambient
ContextVar), document_reference_id, patient_id, attempt count, duration_ms,
cache state, outcome, and fallback_reason set to the *exception class name*
only. Mirrors the structured-event schema in CLAUDE.md "Observability,
verifiable latency claims".

TODO(redis): the project already imports ``redis.asyncio`` widely
(``handoff/generator.py``, ``triage/explainer.py``, ``main.py``). A concrete
:class:`RedisSynthesisCache` is included below; the future wiring step in
``main.py`` constructs it with the existing module-level ``_redis`` client.
Until that wiring lands, the default :class:`_NullCache` keeps this module
import-safe and side-effect-free.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal, Protocol

import anthropic

from agent.metrics import (
    agent_synthesis_latency_seconds,
    agent_synthesis_retries_total,
    agent_synthesis_total,
)
from config import settings
from observability.tool_logging import log_tool_outcome

__all__ = (
    "ApprovedFact",
    "GuidelineSnippet",
    "SynthesisInput",
    "SynthesisOutput",
    "SynthesisOutcome",
    "SynthesisError",
    "ClinicalSignal",
    "GuidelineMapping",
    "SynthesisCache",
    "RedisSynthesisCache",
    "synthesize",
    "PROMPT_VERSION",
)

logger = logging.getLogger(__name__)

PROMPT_VERSION: Final[int] = 1

_MODEL: Final[str] = "claude-sonnet-4-6"
_MAX_TOKENS: Final[int] = 2048
_CACHE_TTL_SECONDS: Final[int] = 3600  # Phase 1 Decision 4: 1 hour.
_TOOL_NAME: Final[str] = "produce_synthesis"
_MAX_ATTEMPTS: Final[int] = 3


# ─────────────────────────────────────────────────────────────────────────────
# Public dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ApprovedFact:
    """One approved fact handed to the synthesizer.

    ``value_repr`` is the human-readable representation; the caller is
    responsible for scrubbing it before construction. This module never
    logs ``value_repr``.
    """

    citation_id: str       # "fact:obs:{id}" or "fact:intake:{field}"
    kind: str              # "observation" | "intake"
    key: str               # LOINC code for obs; field_name for intake
    value_repr: str        # short human-readable; ALREADY scrubbed by caller


@dataclass(frozen=True, slots=True)
class GuidelineSnippet:
    """One retrieved guideline passage handed to the synthesizer.

    ``content`` is already truncated to 400 characters by the caller (matches
    the truncation in ``main.py``'s post-approval-context endpoint).
    """

    citation_id: str       # "guideline:{chunk_id}"
    chunk_id: str
    source_id: str
    document_title: str
    section: str
    page_number: int | None
    content: str
    relevance_score: float


@dataclass(frozen=True, slots=True)
class SynthesisInput:
    approved_facts: tuple[ApprovedFact, ...]
    guidelines: tuple[GuidelineSnippet, ...]
    document_reference_id: str
    patient_id: str

    def fingerprint(self) -> str:
        """SHA256 hex of sorted approved-fact citation_ids.

        Phase 1 Decision 4: a sorted ID list is sufficient for cache
        invalidation. Adding/removing approved facts changes the set and
        therefore the digest; reordering does not. Empty set hashes the
        empty string.
        """
        joined = "\n".join(sorted(f.citation_id for f in self.approved_facts))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def valid_citation_ids(self) -> frozenset[str]:
        """The exhaustive citation_id allowlist for this synthesis call."""
        return frozenset(
            tuple(f.citation_id for f in self.approved_facts)
            + tuple(g.citation_id for g in self.guidelines)
        )


@dataclass(frozen=True, slots=True)
class ClinicalSignal:
    claim: str
    citation_ids: tuple[str, ...]   # >= 1 entry per Phase 1 schema


@dataclass(frozen=True, slots=True)
class GuidelineMapping:
    chunk_id: str       # bare chunk id, not prefixed (matches tool schema)
    claim: str


@dataclass(frozen=True, slots=True)
class SynthesisOutput:
    approved_facts: str
    clinical_signals: tuple[ClinicalSignal, ...]
    guideline_mappings: tuple[GuidelineMapping, ...]
    next_steps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready shape that lands on the API response payload."""
        return {
            "approved_facts": self.approved_facts,
            "clinical_signals": [asdict(s) for s in self.clinical_signals],
            "guideline_mappings": [asdict(m) for m in self.guideline_mappings],
            "next_steps": list(self.next_steps),
        }


class SynthesisError(Exception):
    """Raised after retries are exhausted. Caller falls back to recap.

    Note: :func:`synthesize` itself catches this internally and converts it
    into a :class:`SynthesisOutcome` with ``output=None``. The class is
    exported so callers can inspect ``isinstance`` if a future code path
    re-raises.
    """


@dataclass(frozen=True, slots=True)
class SynthesisOutcome:
    output: SynthesisOutput | None       # None when fell back
    cache: Literal["hit", "miss", "n/a"]
    fallback_reason: str | None          # set iff output is None
    attempts: int                        # 0 on cache hit, 1..3 otherwise
    duration_ms: float


# ─────────────────────────────────────────────────────────────────────────────
# Cache abstraction
# ─────────────────────────────────────────────────────────────────────────────


class SynthesisCache(Protocol):
    """Pluggable cache. Default is :class:`_NullCache`; the wiring step swaps
    in :class:`RedisSynthesisCache` once the redis client is threaded
    through.
    """

    async def get(self, key: str) -> SynthesisOutput | None: ...
    async def set(self, key: str, value: SynthesisOutput, ttl_s: int) -> None: ...


class _NullCache:
    """No-op cache. Always misses; ``set`` is a no-op."""

    async def get(self, key: str) -> SynthesisOutput | None:
        return None

    async def set(self, key: str, value: SynthesisOutput, ttl_s: int) -> None:
        return None


class RedisSynthesisCache:
    """Redis-backed cache.

    Stores the JSON form of :meth:`SynthesisOutput.to_dict`. Read errors and
    decode errors are treated as misses (best-effort: a cache failure must
    never break the synthesis path).
    """

    def __init__(self, redis_client: Any) -> None:
        # Typed as ``Any`` to avoid forcing ``redis.asyncio`` import at
        # module load; this module needs to be importable in environments
        # where redis is wired in by the caller.
        self._redis = redis_client

    async def get(self, key: str) -> SynthesisOutput | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        try:
            signals = tuple(
                ClinicalSignal(
                    claim=s["claim"],
                    citation_ids=tuple(s["citation_ids"]),
                )
                for s in data.get("clinical_signals", [])
            )
            mappings = tuple(
                GuidelineMapping(chunk_id=m["chunk_id"], claim=m["claim"])
                for m in data.get("guideline_mappings", [])
            )
            return SynthesisOutput(
                approved_facts=data["approved_facts"],
                clinical_signals=signals,
                guideline_mappings=mappings,
                next_steps=tuple(data.get("next_steps", [])),
            )
        except Exception:
            return None

    async def set(self, key: str, value: SynthesisOutput, ttl_s: int) -> None:
        if self._redis is None:
            return None
        try:
            await self._redis.setex(key, ttl_s, json.dumps(value.to_dict()))
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Prompt + tool schema (verbatim from Phase 1 brief)
# ─────────────────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT: Final[str] = (
    "You are a clinical co-pilot synthesizing a post-approval briefing for a primary\n"
    "care physician inside an EHR.\n"
    "\n"
    "Your input is two structured lists:\n"
    "1. APPROVED FACTS, clinical fields the physician has already verified and\n"
    "   approved into the patient's chart. Each fact has a stable citation_id of\n"
    "   the form fact:obs:{id} or fact:intake:{field}.\n"
    "2. RETRIEVED GUIDELINES, passages from clinical guideline documents,\n"
    "   retrieved by similarity to the approved facts. Each passage has a stable\n"
    "   citation_id of the form guideline:{chunk_id}.\n"
    "\n"
    "Your job: produce a 4-section briefing that helps the clinician scan the\n"
    "approved record, notice patterns worth attention, see how guideline\n"
    "recommendations map to this patient, and consider possible next steps.\n"
    "\n"
    "CITATION DISCIPLINE\n"
    "- Every claim in clinical_signals MUST cite at least one citation_id drawn\n"
    "  from the APPROVED FACTS or RETRIEVED GUIDELINES enumerated below. No\n"
    "  exceptions.\n"
    "- Every claim in guideline_mappings MUST cite a chunk_id that appears in the\n"
    "  RETRIEVED GUIDELINES list (use the bare chunk_id, not the guideline:\n"
    "  prefix).\n"
    "- Do not invent citation_ids. Do not abbreviate them. Do not synthesize a\n"
    "  fact that is not in the approved list.\n"
    "- If you cannot ground a claim in a listed citation_id, do not include it.\n"
    "\n"
    "VOICE\n"
    "- Trustworthy, terse, deferential. The clinician decides; you surface signals.\n"
    "- Use \"suggested\", never \"recommended\", \"should\", \"must\", or \"indicated\".\n"
    "- Do not propose a specific medication, dose, or diagnosis. You may surface\n"
    "  patterns (for example, \"chest tightness on exertion plus family history of\n"
    "  MI suggests cardiac workup may be warranted\") and reference guideline\n"
    "  recommendations by quoting them, but the act of prescribing is the\n"
    "  clinician's.\n"
    "- Refuse to comment on legal, billing, or insurance matters.\n"
    "\n"
    "LENGTH\n"
    "- Total output across all four sections: 200 to 300 words.\n"
    "- approved_facts: one sentence per category present (meds, allergies,\n"
    "  concerns, family history, etc.). Skip categories with no approved facts.\n"
    "- clinical_signals: 2 to 4 entries, each one short claim sentence.\n"
    "- guideline_mappings: one entry per relevant retrieved guideline, at most 2\n"
    "  sentences each. Skip guidelines that do not apply to this patient.\n"
    "- next_steps: 2 to 4 short imperative phrases, each at most 12 words. Use\n"
    "  \"Consider...\" or \"Suggest...\" framing.\n"
    "\n"
    "FAILURE MODES TO AVOID\n"
    "- Padding (\"Based on the approved facts...\"). Lead with the signal.\n"
    "- Hedging that hides the signal (\"It may possibly be worth considering\").\n"
    "- Em dashes. Use commas, colons, semicolons, periods. No exceptions.\n"
    "- Restating the input verbatim. Synthesize, do not echo.\n"
    "- Generic next steps (\"Continue monitoring\") that are not grounded in the\n"
    "  facts."
)


_PRODUCE_SYNTHESIS: Final[dict] = {
    "name": _TOOL_NAME,
    "description": (
        "Emit the 4-section post-approval briefing. Every clinical_signals "
        "entry must cite at least one citation_id from the enumerated set; "
        "every guideline_mappings entry must reference a chunk_id present "
        "in the retrieved guidelines list."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "approved_facts",
            "clinical_signals",
            "guideline_mappings",
            "next_steps",
        ],
        "properties": {
            "approved_facts": {
                "type": "string",
                "maxLength": 600,
                "description": (
                    "One sentence per category present (meds, allergies, "
                    "concerns, family history, etc.). Skip categories with "
                    "no approved facts."
                ),
            },
            "clinical_signals": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["claim", "citation_ids"],
                    "properties": {
                        "claim": {"type": "string", "maxLength": 240},
                        "citation_ids": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "guideline_mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["chunk_id", "claim"],
                    "properties": {
                        "chunk_id": {"type": "string"},
                        "claim": {"type": "string", "maxLength": 320},
                    },
                },
            },
            "next_steps": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string", "maxLength": 96},
            },
        },
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────


def _build_user_payload(inp: SynthesisInput) -> str:
    lines: list[str] = []
    lines.append("APPROVED FACTS:")
    if inp.approved_facts:
        for fact in inp.approved_facts:
            lines.append(
                f"- citation_id={fact.citation_id} | kind={fact.kind} | "
                f"key={fact.key} | value={fact.value_repr}"
            )
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("RETRIEVED GUIDELINES:")
    if inp.guidelines:
        for g in inp.guidelines:
            page = g.page_number if g.page_number is not None else "n/a"
            lines.append(
                f"- citation_id={g.citation_id} | chunk_id={g.chunk_id} | "
                f"source_id={g.source_id} | document=\"{g.document_title}\" | "
                f"section=\"{g.section}\" | page={page} | "
                f"score={g.relevance_score:.3f}"
            )
            lines.append(f"  content: {g.content}")
    else:
        lines.append("(none)")
    lines.append("")

    valid_ids = sorted(inp.valid_citation_ids())
    lines.append(
        "VALID CITATION_ID SET (exhaustive, anything not in this list is invalid):"
    )
    lines.append(", ".join(valid_ids) if valid_ids else "(empty)")
    lines.append("")
    lines.append("Synthesize the briefing now using the produce_synthesis tool.")
    return "\n".join(lines)


def _cache_key(inp: SynthesisInput) -> str:
    return (
        f"synthesis:v{PROMPT_VERSION}:"
        f"{inp.document_reference_id}:{inp.fingerprint()}"
    )


_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 529})


def _is_retryable(exc: BaseException) -> bool:
    """Match the retry policy spelled out in the brief."""
    if isinstance(exc, anthropic.APITimeoutError):
        return True
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return getattr(exc, "status_code", None) in _RETRYABLE_STATUS_CODES
    return False


def _parse_tool_block(
    tool_input: dict[str, Any],
    inp: SynthesisInput,
) -> SynthesisOutput:
    """Parse + ground-validate the model's tool_use payload.

    Raises :class:`SynthesisError` on shape or grounding violations. The
    caller treats this as a non-retryable failure: re-running the same
    prompt rarely fixes a hallucinated citation, and the rubric will fail
    anyway.
    """
    if not isinstance(tool_input, dict):
        raise SynthesisError("tool_use input is not a dict")

    try:
        approved_facts = tool_input["approved_facts"]
        signals_raw = tool_input["clinical_signals"]
        mappings_raw = tool_input["guideline_mappings"]
        next_steps_raw = tool_input["next_steps"]
    except KeyError as ke:
        raise SynthesisError(f"missing required field: {ke.args[0]}") from None

    if not isinstance(approved_facts, str):
        raise SynthesisError("approved_facts must be a string")
    if not isinstance(signals_raw, list):
        raise SynthesisError("clinical_signals must be a list")
    if not isinstance(mappings_raw, list):
        raise SynthesisError("guideline_mappings must be a list")
    if not isinstance(next_steps_raw, list):
        raise SynthesisError("next_steps must be a list")

    valid_ids = inp.valid_citation_ids()
    valid_chunk_ids = frozenset(g.chunk_id for g in inp.guidelines)

    parsed_signals: list[ClinicalSignal] = []
    for i, item in enumerate(signals_raw):
        if not isinstance(item, dict):
            raise SynthesisError(f"clinical_signals[{i}] is not an object")
        claim = item.get("claim")
        cids = item.get("citation_ids")
        if not isinstance(claim, str) or not claim.strip():
            raise SynthesisError(f"clinical_signals[{i}].claim missing")
        if not isinstance(cids, list) or len(cids) == 0:
            raise SynthesisError(
                f"clinical_signals[{i}].citation_ids must be non-empty"
            )
        for cid in cids:
            if not isinstance(cid, str) or cid not in valid_ids:
                raise SynthesisError(
                    f"clinical_signals[{i}] cites invalid id"
                )
        parsed_signals.append(
            ClinicalSignal(claim=claim, citation_ids=tuple(cids))
        )

    parsed_mappings: list[GuidelineMapping] = []
    for i, item in enumerate(mappings_raw):
        if not isinstance(item, dict):
            raise SynthesisError(f"guideline_mappings[{i}] is not an object")
        chunk_id = item.get("chunk_id")
        claim = item.get("claim")
        if not isinstance(chunk_id, str) or chunk_id not in valid_chunk_ids:
            raise SynthesisError(
                f"guideline_mappings[{i}].chunk_id not in retrieved set"
            )
        if not isinstance(claim, str) or not claim.strip():
            raise SynthesisError(f"guideline_mappings[{i}].claim missing")
        parsed_mappings.append(GuidelineMapping(chunk_id=chunk_id, claim=claim))

    parsed_steps: list[str] = []
    for i, step in enumerate(next_steps_raw):
        if not isinstance(step, str) or not step.strip():
            raise SynthesisError(f"next_steps[{i}] missing or non-string")
        parsed_steps.append(step)

    return SynthesisOutput(
        approved_facts=approved_facts,
        clinical_signals=tuple(parsed_signals),
        guideline_mappings=tuple(parsed_mappings),
        next_steps=tuple(parsed_steps),
    )


def _extract_tool_input(response: Any) -> dict[str, Any]:
    tool_block = next(
        (b for b in getattr(response, "content", []) if getattr(b, "type", None) == "tool_use"),
        None,
    )
    if tool_block is None:
        raise SynthesisError("no tool_use block in response")
    payload = getattr(tool_block, "input", None)
    if not isinstance(payload, dict):
        raise SynthesisError("tool_use input is not a dict")
    return payload


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration entrypoint
# ─────────────────────────────────────────────────────────────────────────────


async def synthesize(
    inp: SynthesisInput,
    *,
    client: anthropic.AsyncAnthropic | None = None,
    cache: SynthesisCache | None = None,
    langfuse: Any | None = None,
) -> SynthesisOutcome:
    """Generate a 4-section clinical briefing for the post-approval flow.

    Returns a :class:`SynthesisOutcome` that the caller turns into a response
    field. On terminal failure, returns ``outcome.output=None`` with a
    ``fallback_reason``; the caller is responsible for falling back to the
    deterministic recap.

    Retries: 3 attempts, exponential backoff 1s/2s/4s. Retried on:
      anthropic.APIStatusError where status_code in {429, 500, 502, 503, 529}
      anthropic.APITimeoutError
      anthropic.APIConnectionError
    Not retried on:
      anthropic.BadRequestError, AuthenticationError, PermissionDeniedError
      Schema-validation failures of the tool_use block (fast fail; retry
      will not help).
    """
    started = time.perf_counter()
    cache = cache or _NullCache()

    log_extra: dict[str, Any] = {
        "document_reference_id": inp.document_reference_id,
        "patient_id": inp.patient_id,
    }

    # 1) Cache probe.
    key = _cache_key(inp)
    try:
        hit = await cache.get(key)
    except Exception:
        hit = None
    if hit is not None:
        duration_ms = (time.perf_counter() - started) * 1000.0
        agent_synthesis_total.labels(outcome="cached").inc()
        agent_synthesis_latency_seconds.observe(duration_ms / 1000.0)
        log_tool_outcome(
            tool_name="synthesis",
            duration_ms=int(duration_ms),
            cache="hit",
            extra={**log_extra, "outcome": "cached", "attempts": 0},
        )
        return SynthesisOutcome(
            output=hit,
            cache="hit",
            fallback_reason=None,
            attempts=0,
            duration_ms=duration_ms,
        )

    # 2) Build the prompt + client.
    user_payload = _build_user_payload(inp)
    if client is None:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    trace = (
        langfuse.trace(name="synthesis", user_id=inp.patient_id)
        if langfuse is not None
        else None
    )

    last_exc_class: str = "UnknownError"
    last_exc_msg: str = ""
    attempt = 0

    # 3) Retry loop.
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        generation = (
            trace.generation(name="synthesis-llm", model=_MODEL, input=user_payload)
            if trace is not None
            else None
        )
        try:
            response = await client.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_payload}],
                tools=[_PRODUCE_SYNTHESIS],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
            )
        except SynthesisError as exc:  # pragma: no cover, defensive
            last_exc_class = type(exc).__name__
            last_exc_msg = str(exc)[:120]
            break
        except Exception as exc:  # noqa: BLE001, retry/terminate decision below
            last_exc_class = type(exc).__name__
            last_exc_msg = str(exc)[:120]
            if generation is not None:
                try:
                    generation.end(level="ERROR")
                except Exception:
                    pass
            if _is_retryable(exc) and attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))
                continue
            break

        # tool_use parsing, never retried.
        try:
            tool_input = _extract_tool_input(response)
            output = _parse_tool_block(tool_input, inp)
        except SynthesisError as exc:
            last_exc_class = type(exc).__name__
            last_exc_msg = str(exc)[:120]
            if generation is not None:
                try:
                    generation.end(level="ERROR")
                except Exception:
                    pass
            break

        if generation is not None:
            try:
                generation.end(output=output.to_dict())
            except Exception:
                pass

        try:
            await cache.set(key, output, _CACHE_TTL_SECONDS)
        except Exception:
            pass

        duration_ms = (time.perf_counter() - started) * 1000.0
        agent_synthesis_total.labels(outcome="success").inc()
        agent_synthesis_latency_seconds.observe(duration_ms / 1000.0)
        agent_synthesis_retries_total.labels(outcome="success").inc(attempt)
        log_tool_outcome(
            tool_name="synthesis",
            duration_ms=int(duration_ms),
            cache="miss",
            extra={**log_extra, "outcome": "success", "attempts": attempt},
        )
        return SynthesisOutcome(
            output=output,
            cache="miss",
            fallback_reason=None,
            attempts=attempt,
            duration_ms=duration_ms,
        )

    # 4) Terminal failure path.
    duration_ms = (time.perf_counter() - started) * 1000.0
    # PHI-safe by construction: every SynthesisError raise site uses static
    # templates with index numbers only (no claim text, no value_repr).
    fallback_reason = (
        f"{last_exc_class}: {last_exc_msg}" if last_exc_msg else last_exc_class
    )
    agent_synthesis_total.labels(outcome="error").inc()
    agent_synthesis_latency_seconds.observe(duration_ms / 1000.0)
    agent_synthesis_retries_total.labels(outcome="failed").inc(attempt or 1)
    logger.warning(
        "synthesis_failed",
        extra={
            **log_extra,
            "attempts": attempt,
            "duration_ms": int(duration_ms),
            "fallback_reason": fallback_reason,
        },
    )
    log_tool_outcome(
        tool_name="synthesis",
        duration_ms=int(duration_ms),
        cache="miss",
        extra={
            **log_extra,
            "outcome": "error",
            "attempts": attempt,
            "fallback_reason": fallback_reason,
        },
    )
    return SynthesisOutcome(
        output=None,
        cache="miss",
        fallback_reason=fallback_reason,
        attempts=attempt or 1,
        duration_ms=duration_ms,
    )
