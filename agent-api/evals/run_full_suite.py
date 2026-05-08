"""Run the full W2 eval suite (156 cases at submission lock) and emit JSON + Markdown results.

Outputs:
  --output  : JSON file consumable by diff_baseline.py
  --md      : Markdown file with per-case status + per-rubric pass-rates
              (defaults next to --output, replacing .json with .md)

Wires the contracted public APIs:
  tests.fixtures.w2_eval_cases.CASES
  evals.runner.run_case
  evals.scoring.score_case
  evals.scoring.aggregate
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import inspect
import json
import logging
import os
import random
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default batch size for asyncio.gather over cases. Tunable via env var so
# CI can drop it without a code change if upstream rate limits tighten.
DEFAULT_BATCH_SIZE = 8

# Backoff schedule for HTTP-429 (or transient) errors emitted by run_case /
# score_case. Three retries: 1s, 2s, 4s — total worst-case 7s additional
# wall-time per case. Anything beyond that is treated as a permanent failure
# and surfaced as an "ERROR" row in the results table.
_RETRY_BACKOFF_SCHEDULE = (1.0, 2.0, 4.0)
_MAX_RETRIES = len(_RETRY_BACKOFF_SCHEDULE)


# Indirected so tests can monkeypatch the backoff sleep without touching
# ``asyncio.sleep`` globally (which would also slow down asyncio.gather's
# internal scheduling).
async def _retry_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)

# Allow running both via `python3 -m evals.run_full_suite` (cwd=agent-api)
# and directly. Tests patch the module-level symbols, so import lazily inside main.

REPO_AGENT_API = Path(__file__).resolve().parent.parent


def _serialize(obj):
    if is_dataclass(obj):
        return asdict(obj)
    return obj


def _iter_extraction_citations(extraction: dict) -> Any:
    """Yield every citation dict embedded in an extraction payload.

    Walks both list-shaped containers (``values``, ``key_facts``,
    ``current_medications``, …) and the demographic ``TextField`` shapes
    so the Wave 2C ``verification_pass_rate`` rubric sees every cited
    item without owning a copy of the schema topology.
    """
    if not isinstance(extraction, dict):
        return
    list_keys = (
        "values",
        "key_facts",
        "current_medications",
        "allergies",
        "family_history",
    )
    for key in list_keys:
        items = extraction.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for citation in item.get("citations") or []:
                yield citation
    for key in ("chief_concern", "code_status"):
        item = extraction.get(key)
        if isinstance(item, dict):
            for citation in item.get("citations") or []:
                yield citation
    demographics = extraction.get("demographics") or {}
    if isinstance(demographics, dict):
        for sub_key in ("name", "dob", "sex", "mrn", "address"):
            item = demographics.get(sub_key)
            if isinstance(item, dict):
                for citation in item.get("citations") or []:
                    yield citation


def _markdown_report(case_rows: list[dict], aggregates: dict) -> str:
    lines: list[str] = []
    lines.append("# W2 Eval Suite Results")
    lines.append("")
    lines.append("## Per-rubric pass rates")
    lines.append("")
    lines.append("| rubric | rate |")
    lines.append("| --- | --- |")
    for k, v in aggregates.items():
        if k == "nearest_label_grounded":
            detail = aggregates.get("nearest_label_grounded_detail") or {}
            n_pass = detail.get("n_passed")
            n_eval = detail.get("n_evaluated")
            if v is None or not n_eval:
                lines.append(
                    f"| nearest_label_grounded (info) | n/a (0 cases with labels) |"
                )
            else:
                pct = float(v) * 100
                lines.append(
                    f"| nearest_label_grounded (info) | "
                    f"{pct:.1f}% ({n_pass}/{n_eval} cases with labels) |"
                )
            continue
        if k == "nearest_label_grounded_detail":
            # Rendered above alongside ``nearest_label_grounded``.
            continue
        try:
            lines.append(f"| {k} | {float(v) * 100:.1f}% |")
        except (TypeError, ValueError):
            lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## Per-case results")
    lines.append("")
    lines.append("| case_id | bucket | status | notes |")
    lines.append("| --- | --- | --- | --- |")
    for row in case_rows:
        lines.append(
            f"| {row.get('case_id', '?')} | {row.get('bucket', '?')} | "
            f"{row.get('status', '?')} | {row.get('notes', '')} |"
        )
    lines.append("")
    return "\n".join(lines)


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` if it is awaitable; otherwise return it as-is.

    Tests patch ``run_case`` / ``score_case`` with plain (sync) lambdas — the
    serial implementation tolerated this because awaiting a non-coroutine
    raises a TypeError that the outer try/except swallowed. The parallel
    implementation must be more careful: we don't want a "TypeError: object
    int can't be used in 'await' expression" failure to silently turn every
    case into an ERROR row.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def _is_retryable_exception(exc: BaseException) -> bool:
    """Return True for transient errors worth retrying (429 / 503 / network).

    We do not import the Anthropic / Voyage SDKs here — that would couple the
    eval driver to specific client libraries. Instead we sniff the exception's
    type name and string for the classic transient signals.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "ratelimit" in name or "rate_limit" in name:
        return True
    if "429" in msg or "rate limit" in msg or "rate-limit" in msg:
        return True
    if "503" in msg or "overloaded" in msg or "service unavailable" in msg:
        return True
    if "timeout" in name or "timeout" in msg:
        return True
    return False


async def _call_with_retry(
    fn: Any,
    *args: Any,
    case_id: str,
    request_id: str,
    op: str,
    **kwargs: Any,
) -> Any:
    """Invoke ``fn`` with up to _MAX_RETRIES backoff retries on transient errors.

    Logs every retry with PSR-3-style structured ``extra`` so the JSON log
    formatter preserves request_id / case_id / attempt for the eval gate's
    post-mortem.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            result = fn(*args, **kwargs)
            return await _maybe_await(result)
        except Exception as exc:
            last_exc = exc
            if attempt >= _MAX_RETRIES or not _is_retryable_exception(exc):
                raise
            backoff = _RETRY_BACKOFF_SCHEDULE[attempt]
            # Tiny jitter so 8 cases sharing a batch don't synchronise their
            # retries into the same 1s window.
            jitter = random.uniform(0.0, 0.25)
            logger.warning(
                "eval.retry",
                extra={
                    "case_id": case_id,
                    "request_id": request_id,
                    "op": op,
                    "attempt": attempt + 1,
                    "max_attempts": _MAX_RETRIES,
                    "backoff_seconds": backoff,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc)[:200],
                },
            )
            await _retry_sleep(backoff + jitter)
    # Defensive: loop should have either returned or re-raised.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("eval._call_with_retry: unreachable")


async def _process_one_case(
    case: Any,
    *,
    run_case: Any,
    score_case: Any,
    fixtures_root: Path,
) -> dict[str, Any]:
    """Run + score one case. Always returns a dict (never raises).

    The dict has keys ``row`` (always present), ``score`` / ``case`` /
    ``outcome`` (present only on success), and ``error`` (present on failure).
    """
    case_id = getattr(case, "case_id", "?")
    request_id = uuid.uuid4().hex[:12]
    _token = _REPOINT_CASE_ID.set(case_id)
    try:
        outcome = await _call_with_retry(
            run_case,
            case,
            fixtures_root=fixtures_root,
            case_id=case_id,
            request_id=request_id,
            op="run_case",
        )
        score = await _call_with_retry(
            score_case,
            case,
            outcome,
            case_id=case_id,
            request_id=request_id,
            op="score_case",
        )
        score_d = _serialize(score)
        case_d = _serialize(case)
        status = score_d.get("status") if isinstance(score_d, dict) else "?"
        row = {
            "case_id": (case_d.get("case_id") if isinstance(case_d, dict) else case_id),
            "bucket": (case_d.get("bucket") if isinstance(case_d, dict) else getattr(case, "bucket", "?")),
            "status": status if status is not None else "scored",
            "notes": (score_d.get("notes", "") if isinstance(score_d, dict) else ""),
        }
        return {
            "row": row,
            "score": score,
            "case": case,
            "outcome": outcome,
            "case_id": case_id,
            "ok": True,
        }
    except Exception as exc:
        return {
            "row": {
                "case_id": case_id,
                "bucket": getattr(case, "bucket", "?"),
                "status": "ERROR",
                "notes": str(exc),
            },
            "case_id": case_id,
            "ok": False,
            "error": str(exc),
        }
    finally:
        _REPOINT_CASE_ID.reset(_token)


# ---------------------------------------------------------------------------
# Wave 2E — repoint_trace.jsonl persistence.
# ---------------------------------------------------------------------------


class _RepointTraceHandler(logging.Handler):
    """Capture extractor_citation_repointed records into a JSONL sink."""

    _FIELDS = (
        "tool", "field_name", "outcome", "from", "to", "candidate_count",
        "chosen_bbox_id", "chosen_granularity", "anchor_bbox_id",
        "anchor_text_preview", "y_distance", "value_preview",
        "nearest_label_score", "nearest_label_used",
    )

    def __init__(self, path: Path) -> None:
        super().__init__(level=logging.INFO)
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("w", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.getMessage() != "extractor_citation_repointed":
                return
            payload: dict[str, Any] = {"event": "extractor_citation_repointed"}
            for k in self._FIELDS:
                if hasattr(record, k):
                    payload[k] = getattr(record, k)
            cid = _REPOINT_CASE_ID.get()
            if cid is not None:
                payload["case_id"] = cid
            self._fh.write(json.dumps(payload, default=str) + "\n")
            self._fh.flush()
        except Exception:  # pragma: no cover
            self.handleError(record)

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()


_REPOINT_CASE_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "repoint_case_id", default=None
)


@contextlib.contextmanager
def _repoint_trace_capture(path: Optional[Path]):
    """Attach a _RepointTraceHandler at the root logger for the run."""
    if path is None:
        yield None
        return
    handler = _RepointTraceHandler(path)
    root = logging.getLogger()
    prior_level = root.level
    if prior_level > logging.INFO or prior_level == logging.NOTSET:
        root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(prior_level)


async def _run_async(args: argparse.Namespace) -> tuple[list[dict], list[Any], list[Any], dict[str, Any], dict[str, Any]]:
    # Lazy imports — let tests patch these.
    from tests.fixtures.w2_eval_cases import CASES  # type: ignore
    from evals.runner import run_case as _run_case_raw  # type: ignore
    from evals.scoring import aggregate, score_case  # type: ignore
    from evals._response_cache import (  # type: ignore
        cache_reads_enabled,
        derive_cache_key,
        get_default_cache,
        resolve_cache_mode,
    )
    from evals.runner import resolve_fixture_path  # type: ignore

    # Resolve cache mode from CLI flag (overrides env var when provided).
    _cache_mode_arg: Optional[str] = getattr(args, "cache", None)
    _effective_cache_mode = resolve_cache_mode(_cache_mode_arg)
    _cache_obj = get_default_cache()

    # Running tallies — written back into the _run_async return tuple's
    # accompanying log at batch completion.
    _cache_hits = 0
    _cache_misses = 0

    async def run_case(case: Any, *, fixtures_root: Any, **kw: Any) -> Any:  # type: ignore[misc]
        """Thin wrapper that injects cache kwargs into the underlying run_case."""
        nonlocal _cache_hits, _cache_misses
        # Pre-sniff: check the cache ourselves (cheap) so we can tally.
        _hit = False
        if cache_reads_enabled(_effective_cache_mode):
            try:
                bucket = getattr(case, "bucket", None)
                if bucket == "evidence_retrieval":
                    _key = derive_cache_key(b"")
                else:
                    _fp = resolve_fixture_path(getattr(case, "fixture_key", ""), fixtures_root)
                    _fb = _fp.read_bytes() if _fp.exists() else b""
                    _key = derive_cache_key(_fb) if _fb else ""
                if _key and _cache_obj.read(_key) is not None:
                    _hit = True
            except Exception:
                pass
        if _hit:
            _cache_hits += 1
        elif _effective_cache_mode != "off":
            _cache_misses += 1

        return await _run_case_raw(
            case,
            fixtures_root=fixtures_root,
            cache=_cache_obj,
            cache_mode=_effective_cache_mode,
            **kw,
        )

    # Counter / histogram are imported lazily — they live in agent.metrics
    # which pulls in prometheus_client. The eval suite is the only consumer
    # outside the FastAPI app, so do this once per run.
    try:
        from agent.metrics import (  # type: ignore
            agent_eval_batch_duration_seconds,
            agent_eval_cases_completed_total,
        )
    except Exception:  # pragma: no cover — metrics are best-effort
        agent_eval_batch_duration_seconds = None
        agent_eval_cases_completed_total = None

    cases = list(CASES)

    # --smoke overrides --max-cases: select the deterministic 10-case subset.
    # --failing-only is mutually exclusive with --smoke (enforced at parse time).
    smoke = getattr(args, "smoke", False)
    failing_only_path = getattr(args, "failing_only", None)
    if smoke and failing_only_path is not None:
        raise SystemExit("--smoke and --failing-only are mutually exclusive")
    if smoke:
        from evals._smoke_subset import SMOKE_CASE_IDS  # type: ignore
        case_by_id = {c.case_id: c for c in cases}
        cases = [case_by_id[cid] for cid in SMOKE_CASE_IDS if cid in case_by_id]
        logger.info(
            "eval.smoke_mode",
            extra={
                "mode": "SMOKE",
                "n_cases": len(cases),
                "case_ids": list(SMOKE_CASE_IDS),
            },
        )
        print(f"eval running in SMOKE mode, n={len(cases)} cases")
    elif failing_only_path is not None:
        failing_ids = _load_failing_case_ids(failing_only_path)
        case_by_id = {c.case_id: c for c in cases}
        cases = [case_by_id[cid] for cid in sorted(failing_ids) if cid in case_by_id]
        logger.info(
            "eval.failing_only_mode",
            extra={
                "mode": "FAILING_ONLY",
                "n_cases": len(cases),
                "prior_results": str(failing_only_path),
                "case_ids": [c.case_id for c in cases],
            },
        )
        print(
            f"eval running in FAILING_ONLY mode, n={len(cases)} cases "
            f"(from {failing_only_path})"
        )
    else:
        max_cases = getattr(args, "max_cases", None)
        if max_cases is not None and max_cases > 0:
            cases = cases[:max_cases]

    batch_size = max(1, int(args.batch_size))

    case_rows: list[dict] = []
    scores: list[Any] = []
    scored_cases: list[Any] = []
    outcomes_by_case_id: dict[str, Any] = {}

    total = len(cases)
    completed = 0
    for batch_index in range(0, total, batch_size):
        batch = cases[batch_index : batch_index + batch_size]
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *(
                _process_one_case(
                    c,
                    run_case=run_case,
                    score_case=score_case,
                    fixtures_root=args.fixtures_root,
                )
                for c in batch
            ),
            return_exceptions=False,
        )
        wall = time.perf_counter() - t0

        success = sum(1 for r in results if r.get("ok"))
        errors = len(results) - success
        completed += len(results)

        if agent_eval_batch_duration_seconds is not None:
            try:
                agent_eval_batch_duration_seconds.observe(wall)
            except Exception:  # pragma: no cover
                pass
        if agent_eval_cases_completed_total is not None:
            try:
                if success:
                    agent_eval_cases_completed_total.labels(outcome="success").inc(success)
                if errors:
                    agent_eval_cases_completed_total.labels(outcome="error").inc(errors)
            except Exception:  # pragma: no cover
                pass

        logger.info(
            "eval.batch_complete",
            extra={
                "batch_index": batch_index // batch_size,
                "batch_size": len(batch),
                "duration_seconds": round(wall, 3),
                "success": success,
                "errors": errors,
                "completed": completed,
                "total": total,
                "cache_mode": _effective_cache_mode,
                "cache_hits_cumulative": _cache_hits,
                "cache_misses_cumulative": _cache_misses,
            },
        )

        for r in results:
            case_rows.append(r["row"])
            if r.get("ok"):
                scores.append(r["score"])
                scored_cases.append(r["case"])
                outcomes_by_case_id[r["case_id"]] = r["outcome"]

    # Deterministic output ordering — sort by case_id so the JSON / Markdown
    # diff cleanly across runs regardless of asyncio.gather completion order.
    case_rows.sort(key=lambda row: str(row.get("case_id") or ""))

    logger.info(
        "eval.cache_summary",
        extra={
            "cache_mode": _effective_cache_mode,
            "cache_hits": _cache_hits,
            "cache_misses": _cache_misses,
            "total_cases": total,
        },
    )
    return case_rows, scores, scored_cases, outcomes_by_case_id, {
        "cache_mode": _effective_cache_mode,
        "cache_hits": _cache_hits,
        "cache_misses": _cache_misses,
    }


def _load_failing_case_ids(prior_results_path: Path) -> set[str]:
    """Parse a prior eval_results.json artifact and return the set of case_ids
    that failed any rubric (or errored).

    The prior artifact is the per-case detail JSON written alongside results.
    We accept either:
      - a direct list of {case_id, rubric_results: {...bool...}, status?} rows
      - a dict with key "cases" pointing at such a list
      - the top-level results JSON where case-level data lives under
        "_case_rows" (forward-compat).

    A case is "failing" if any rubric_results value is False, or status is
    "ERROR", or any explicit "passed" key is False. Empty/malformed files
    return the empty set so callers can short-circuit cleanly.
    """
    try:
        raw = json.loads(prior_results_path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    rows: list[Any] = []
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        for key in ("_case_rows", "cases", "case_rows"):
            v = raw.get(key)
            if isinstance(v, list):
                rows = v
                break
    failing: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = row.get("case_id") or row.get("id")
        if not isinstance(cid, str):
            continue
        if row.get("status") == "ERROR" or row.get("error"):
            failing.add(cid)
            continue
        rubric_results = row.get("rubric_results") or row.get("rubrics") or {}
        if isinstance(rubric_results, dict):
            for v in rubric_results.values():
                if v is False:
                    failing.add(cid)
                    break
        if row.get("passed") is False:
            failing.add(cid)
    return failing


def _load_gt_sidecar(fixture_path: Path) -> Optional[dict]:
    """Load the bbox GT sidecar (``<fixture>.gt.json``) if present."""
    sidecar = fixture_path.with_suffix(fixture_path.suffix + ".gt.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _first_citation_bbox(extraction: Any) -> Optional[dict]:
    """Best-effort extraction-side bbox lookup for the first cited item.

    Returns ``None`` when no bbox is available — the rubric treats that
    as a skip rather than a fail.
    """
    if not isinstance(extraction, dict):
        return None
    pools: list[Any] = []
    kind = extraction.get("kind")
    if kind == "lab_report":
        pools = list(extraction.get("values") or [])
    elif kind == "unknown":
        pools = list(extraction.get("key_facts") or [])
    elif kind == "intake_form":
        for key in (
            "current_medications",
            "allergies",
            "family_history",
            "pertinent_labs",
        ):
            pools.extend(extraction.get(key) or [])
    for item in pools:
        if not isinstance(item, dict):
            continue
        for cit in item.get("citations") or []:
            if not isinstance(cit, dict):
                continue
            bbox = cit.get("bbox")
            if isinstance(bbox, dict):
                return bbox
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                return {"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3]}
    return None


def _gt_first_field_bbox(gt: dict) -> Optional[dict]:
    """Return the first GT field's bbox dict (or None if the sidecar is empty)."""
    fields = gt.get("fields") or []
    if not fields:
        return None
    f0 = fields[0]
    bbox = (f0 or {}).get("bbox") if isinstance(f0, dict) else None
    return bbox if isinstance(bbox, dict) else None


def _resolve_fixture_path_local(case: Any, fixtures_root: Path) -> Path:
    from evals.runner import resolve_fixture_path  # type: ignore

    return resolve_fixture_path(getattr(case, "fixture_key", ""), fixtures_root)


def _score_bbox_rubrics(
    cases: list[Any], outcomes_by_case_id: dict[str, Any], fixtures_root: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Compute citation_iou + citation_pixel_distance globally + per-modality.

    Both rubrics are GT-gated: a case is in the denominator only if it
    has a sidecar AND the run produced an extracted bbox to compare to.
    """
    from evals.rubrics_mechanical import citation_iou, citation_pixel_distance  # type: ignore

    iou_total = 0
    iou_passed = 0
    pix_total = 0
    pix_sum = 0.0
    per_mod: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"iou_n": 0, "iou_pass": 0, "pix_n": 0, "pix_sum": 0.0}
    )

    for case in cases:
        case_id = getattr(case, "case_id", None)
        outcome = outcomes_by_case_id.get(case_id) if case_id else None
        if outcome is None:
            continue
        try:
            fixture_path = _resolve_fixture_path_local(case, fixtures_root)
        except Exception:
            continue
        gt = _load_gt_sidecar(fixture_path)
        if gt is None:
            continue
        gt_bbox = _gt_first_field_bbox(gt)
        if gt_bbox is None:
            continue
        extracted = _first_citation_bbox(getattr(outcome, "extraction", None))
        if extracted is None:
            continue
        modality = str(getattr(case, "document_modality", "unknown") or "unknown")
        iou_total += 1
        per_mod[modality]["iou_n"] += 1
        if citation_iou(extracted, gt_bbox):
            iou_passed += 1
            per_mod[modality]["iou_pass"] += 1
        dist = citation_pixel_distance(extracted, gt_bbox)
        if dist is not None:
            pix_total += 1
            pix_sum += dist
            per_mod[modality]["pix_n"] += 1
            per_mod[modality]["pix_sum"] += dist

    global_block: dict[str, Any] = {
        "citation_iou": {
            "pass_rate": (iou_passed / iou_total) if iou_total else None,
            "n_evaluated": iou_total,
        },
        "citation_pixel_distance": {
            "mean_px": (pix_sum / pix_total) if pix_total else None,
            "n_evaluated": pix_total,
            "info_only": True,
        },
    }
    per_modality_block: dict[str, dict[str, Any]] = {}
    for mod, agg in per_mod.items():
        per_modality_block[mod] = {
            "citation_iou": (agg["iou_pass"] / agg["iou_n"]) if agg["iou_n"] else None,
            "citation_iou_detail": {
                "pass_rate": (agg["iou_pass"] / agg["iou_n"]) if agg["iou_n"] else None,
                "n_evaluated": agg["iou_n"],
            },
            "citation_pixel_distance_detail": {
                "mean_px": (agg["pix_sum"] / agg["pix_n"]) if agg["pix_n"] else None,
                "n_evaluated": agg["pix_n"],
                "info_only": True,
            },
        }
    return global_block, per_modality_block


def _layout_blocks_for_rubric(outcome: Any) -> list[Any]:
    """Adapt ``outcome.ocr_layout`` (list of dicts from the runner) into
    objects exposing ``.text`` / ``.page`` / ``.bbox`` so the
    ``nearest_label_grounded`` rubric (which uses ``getattr``) can read
    them. ``LayoutBlock`` dataclass instances are passed through.
    """
    raw = getattr(outcome, "ocr_layout", None) or []
    from types import SimpleNamespace
    out: list[Any] = []
    for blk in raw:
        if isinstance(blk, dict):
            out.append(SimpleNamespace(
                text=blk.get("text", "") or "",
                page=blk.get("page"),
                bbox=blk.get("bbox"),
            ))
        else:
            out.append(blk)
    return out


def _score_nearest_label_grounded(
    cases: list[Any], outcomes_by_case_id: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Compute the info-only ``nearest_label_grounded`` rubric.

    Cases for which the rubric returns ``None`` (no ``nearest_label``
    emitted) are excluded from BOTH numerator and denominator. Per-modality
    pass-rates are computed the same way.

    Errors raised by the rubric are logged and treated as a skip — the
    eval run must never crash on an info-only rubric.
    """
    try:
        from evals.rubrics_llm import nearest_label_grounded  # type: ignore
    except Exception as exc:  # pragma: no cover — defensive (tests stub modules)
        logger.warning(
            "eval.nearest_label_grounded.import_error",
            extra={
                "exception_type": type(exc).__name__,
                "exception_message": str(exc)[:200],
            },
        )
        return (
            {"pass_rate": None, "n_passed": 0, "n_evaluated": 0, "info_only": True},
            {},
        )

    n_total = 0
    n_passed = 0
    per_mod: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "pass": 0})

    for case in cases:
        case_id = getattr(case, "case_id", None)
        outcome = outcomes_by_case_id.get(case_id) if case_id else None
        if outcome is None:
            continue
        layout = _layout_blocks_for_rubric(outcome)
        try:
            result = nearest_label_grounded(outcome, case, layout_blocks=layout)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "eval.nearest_label_grounded.error",
                extra={
                    "case_id": case_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc)[:200],
                },
            )
            continue
        if result is None:
            continue
        modality = str(getattr(case, "document_modality", "unknown") or "unknown")
        n_total += 1
        per_mod[modality]["n"] += 1
        if result:
            n_passed += 1
            per_mod[modality]["pass"] += 1

    global_block: dict[str, Any] = {
        "pass_rate": (n_passed / n_total) if n_total else None,
        "n_passed": n_passed,
        "n_evaluated": n_total,
        "info_only": True,
    }
    per_mod_block: dict[str, dict[str, Any]] = {}
    for mod, agg in per_mod.items():
        per_mod_block[mod] = {
            "nearest_label_grounded": (agg["pass"] / agg["n"]) if agg["n"] else None,
            "nearest_label_grounded_detail": {
                "pass_rate": (agg["pass"] / agg["n"]) if agg["n"] else None,
                "n_passed": agg["pass"],
                "n_evaluated": agg["n"],
                "info_only": True,
            },
        }
    return global_block, per_mod_block


def _per_modality_breakdown(scores: list[Any], cases: list[Any]) -> dict[str, dict[str, float | int]]:
    """Group scores by ``case.document_modality`` and compute pass-rates.

    Returns ``{modality: {n_cases, schema_valid, citation_present,
    citation_resolvable, citation_row_match, citation_token_match,
    correct_critic_decision, no_phi_in_logs}}``. Skips LLM rubrics
    (factually_consistent / safe_refusal) — those are not in this rubric
    bucket. ``provenance_chain`` is tri-state and excluded from the
    per-modality table to avoid surprising None semantics.
    """
    rubric_fields = (
        "schema_valid",
        "citation_present",
        "citation_resolvable",
        "citation_row_match",
        "citation_token_match",
        "correct_critic_decision",
        "no_phi_in_logs",
    )
    grouped: dict[str, list[Any]] = defaultdict(list)
    for case, score in zip(cases, scores):
        modality = getattr(case, "document_modality", None) or "unknown"
        grouped[str(modality)].append(score)

    out: dict[str, dict[str, float | int]] = {}
    for modality, group in grouped.items():
        n = len(group)
        if n == 0:
            continue
        entry: dict[str, float | int] = {"n_cases": n}
        for name in rubric_fields:
            passed = sum(1 for s in group if getattr(s, name, False))
            entry[name] = passed / n
        out[modality] = entry
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Path to JSON results")
    parser.add_argument("--md", type=Path, default=None, help="Path to Markdown report")
    parser.add_argument("--fixtures-root", type=Path, default=REPO_AGENT_API / "tests" / "fixtures" / "eval")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("EVAL_PARALLEL_BATCH_SIZE", DEFAULT_BATCH_SIZE)),
        help="asyncio.gather batch size (default: $EVAL_PARALLEL_BATCH_SIZE or 8)",
    )
    parser.add_argument(
        "--max-cases",
        type=int,
        default=None,
        help="If set, only run the first N cases (for dry-run smoke tests)",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        default=False,
        help=(
            "Run the deterministic 10-case smoke subset instead of the full suite. "
            "Overrides --max-cases when both are passed. "
            "Costs ~$1 API spend vs ~$15-30 for the full 124-case suite."
        ),
    )
    parser.add_argument(
        "--failing-only",
        type=Path,
        default=None,
        metavar="PRIOR_RESULTS_JSON",
        help=(
            "Cost-saving mode: only re-run cases that failed any rubric in the "
            "prior eval_results.json artifact. Mutually exclusive with --smoke. "
            "When the prior run had zero failures, the script short-circuits and "
            "writes a results JSON with _mode='failing_only_skipped' (gate passes). "
            "diff_baseline.py recognizes _mode='failing_only' and drops the "
            "global pass-rate gate (the subset is structurally biased), retaining "
            "only ABSOLUTE_RUBRICS (no_phi_in_logs) as a hard gate."
        ),
    )
    parser.add_argument(
        "--cache",
        type=str,
        default=None,
        choices=["off", "read", "write", "readwrite"],
        help=(
            "Eval response cache mode. Overrides EVAL_USE_CACHE env var. "
            "off=disabled (default), read=read-only, write=write-only, "
            "readwrite=read then write on miss. "
            "Cache key: sha256(fixture_bytes || EVAL_CACHE_PROMPT_HASH || "
            "EVAL_MODEL_ID || EVAL_CACHE_VERSION). "
            "Bump EVAL_CACHE_VERSION to invalidate when prompts change."
        ),
    )
    parser.add_argument(
        "--repoint-trace",
        type=Path,
        default=None,
        help=(
            "Path for the per-case repoint_trace.jsonl artifact. "
            "Defaults to <output_dir>/repoint_trace.jsonl."
        ),
    )
    args = parser.parse_args(argv)

    if getattr(args, "smoke", False) and getattr(args, "failing_only", None) is not None:
        parser.error("--smoke and --failing-only are mutually exclusive")

    md_path = args.md or args.output.with_suffix(".md")
    trace_path = args.repoint_trace or (args.output.parent / "repoint_trace.jsonl")

    # Short-circuit failing-only mode when the prior artifact has no failures.
    # We avoid even spinning up the case loop / event loop in that case so PR
    # CI runs cost ~$0 when the previous run was clean.
    failing_only_path = getattr(args, "failing_only", None)
    if failing_only_path is not None:
        prior_failing = _load_failing_case_ids(failing_only_path)
        if not prior_failing:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            stub = {
                "_mode": "failing_only_skipped",
                "_note": (
                    "prior eval_results.json had no failing cases — rerun-failing "
                    "skipped to save Anthropic API spend"
                ),
                "_prior_results": str(failing_only_path),
                "no_phi_in_logs": 1.0,
            }
            args.output.write_text(json.dumps(stub, indent=2) + "\n")
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(
                "# W2 Eval Suite — failing-only mode (skipped)\n\n"
                f"Prior results `{failing_only_path}` had no failing cases. "
                "No re-run executed.\n"
            )
            print(
                f"FAILING_ONLY: prior artifact {failing_only_path} had no "
                f"failures — skipping run, wrote stub to {args.output}"
            )
            return 0

    with _repoint_trace_capture(trace_path):
        case_rows, scores, scored_cases, outcomes_by_case_id, cache_stats = asyncio.run(_run_async(args))

    from evals.scoring import aggregate  # type: ignore
    # Existing rubric pass-rates are computed over the original (pre-Wave-2C)
    # case set only — i.e. exclude the ``bbox_gt`` bucket. The bbox_gt cases
    # exist to drive the new ``citation_iou`` rubric; folding them into the
    # legacy aggregates would shift values that have separately-baselined
    # floors. The Wave 3 rebaseline PR (separate) will recompute from scratch.
    legacy_scores = [
        s for s, c in zip(scores, scored_cases)
        if getattr(c, "bucket", None) != "bbox_gt"
    ]
    legacy_cases = [c for c in scored_cases if getattr(c, "bucket", None) != "bbox_gt"]
    agg = aggregate(legacy_scores)

    # Ensure the JSON contains all rubric pass-rates + critic_false_positive_rate.
    expected_keys = (
        "schema_valid",
        "citation_present",
        "citation_resolvable",
        "citation_row_match",
        "citation_token_match",
        "correct_critic_decision",
        "factually_consistent",
        "safe_refusal",
        "no_phi_in_logs",
        "provenance_chain",
        "synthesis_grounded",
        "icd10_grounded",
        "critic_false_positive_rate",
        "keyword_match_in_citation",
    )
    results: dict[str, Any] = {}
    for k in expected_keys:
        v = agg.get(k)
        results[k] = float(v) if v is not None else 0.0

    # Wave 2C — per-modality breakdown (consumed by diff_baseline.py).
    # Use legacy_* lists so existing per-modality entries match their
    # pre-Wave-2C baselines. The new bbox_gt cases contribute their
    # citation_iou pass-rate via _score_bbox_rubrics below.
    results["per_modality"] = _per_modality_breakdown(legacy_scores, legacy_cases)

    # Wave 2C — bbox-GT-gated rubrics (citation_iou + citation_pixel_distance).
    # GT-gated: only synthetic_v2 cases that produced an extracted bbox count.
    bbox_global, bbox_per_mod = _score_bbox_rubrics(
        scored_cases, outcomes_by_case_id, args.fixtures_root,
    )
    iou_pr = bbox_global["citation_iou"]["pass_rate"]
    pix_mean = bbox_global["citation_pixel_distance"]["mean_px"]
    # Top-level: flat float for diff_baseline.py compatibility.
    results["citation_iou"] = float(iou_pr) if iou_pr is not None else 0.0
    results["citation_iou_detail"] = bbox_global["citation_iou"]
    results["citation_pixel_distance_detail"] = bbox_global["citation_pixel_distance"]
    if pix_mean is not None:
        results["citation_pixel_distance_mean_px"] = float(pix_mean)
    for mod, block in bbox_per_mod.items():
        results["per_modality"].setdefault(mod, {})
        for k, v in block.items():
            results["per_modality"][mod][k] = v

    # Wave 2B+ — info-only ``nearest_label_grounded`` rubric. Skipped cases
    # (no LLM-emitted nearest_label) are excluded from numerator AND
    # denominator. NOT in baseline.json — purely diagnostic.
    nlg_global, nlg_per_mod = _score_nearest_label_grounded(
        scored_cases, outcomes_by_case_id,
    )
    nlg_pr = nlg_global["pass_rate"]
    results["nearest_label_grounded"] = float(nlg_pr) if nlg_pr is not None else None
    results["nearest_label_grounded_detail"] = nlg_global
    for mod, block in nlg_per_mod.items():
        results["per_modality"].setdefault(mod, {})
        for k, v in block.items():
            results["per_modality"][mod][k] = v

    # Wave 2C — INFO-only ``verification_pass_rate`` rubric. Walks every
    # citation that carries a ``verification`` block (i.e. ones the
    # citation_verifier ran for) and computes the % that came back ``yes``.
    # Cases where the verifier never ran (off / sampled-out) contribute
    # zero to numerator AND denominator. Not in baseline.json — purely
    # diagnostic; surfaces "is the verifier prompt over-rejecting?".
    n_yes = 0
    n_seen = 0
    for case in scored_cases:
        outcome = outcomes_by_case_id.get(getattr(case, "case_id", None))
        extraction = getattr(outcome, "extraction", None) if outcome else None
        if not isinstance(extraction, dict):
            continue
        for citation in _iter_extraction_citations(extraction):
            verification = citation.get("verification") if isinstance(citation, dict) else None
            if not isinstance(verification, dict):
                continue
            n_seen += 1
            if verification.get("status") == "yes":
                n_yes += 1
    results["verification_pass_rate"] = {
        "pass_rate": (n_yes / n_seen) if n_seen else None,
        "n_yes": n_yes,
        "n_verified": n_seen,
        "info_only": True,
    }

    # Cache statistics — written to results JSON for CI dashboards.
    results["_cache"] = cache_stats

    # Tag the run mode so diff_baseline.py can adjust gating. failing_only
    # mode runs a structurally biased subset (only previously-failing cases),
    # so the global pass-rate gate is dropped — only ABSOLUTE_RUBRICS gate.
    if getattr(args, "smoke", False):
        results["_mode"] = "smoke"
    elif getattr(args, "failing_only", None) is not None:
        results["_mode"] = "failing_only"
        results["_prior_results"] = str(getattr(args, "failing_only"))
        results["_scored_case_ids"] = sorted(
            getattr(c, "case_id", "") for c in scored_cases if getattr(c, "case_id", None)
        )
    else:
        results["_mode"] = "full"

    # Persist per-case rows so a future failing-only re-run can read this
    # artifact directly. Keep the row shape minimal — case_id + status +
    # rubric pass/fail booleans — to keep the JSON small.
    _case_row_summaries: list[dict] = []
    for row in case_rows:
        if not isinstance(row, dict):
            continue
        cid = row.get("case_id")
        if not isinstance(cid, str):
            continue
        rubric_results: dict[str, bool] = {}
        for k, v in row.items():
            if isinstance(v, bool):
                rubric_results[k] = v
        _case_row_summaries.append({
            "case_id": cid,
            "status": row.get("status", "OK"),
            "error": row.get("error"),
            "rubric_results": rubric_results,
        })
    results["_case_rows"] = _case_row_summaries

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_markdown_report(case_rows, results))

    _cache_hits = cache_stats.get("cache_hits", 0)
    _cache_misses = cache_stats.get("cache_misses", 0)
    _cache_mode_str = cache_stats.get("cache_mode", "off")
    print(
        f"Wrote {args.output} and {md_path} "
        f"[cache={_cache_mode_str} hits={_cache_hits} misses={_cache_misses}]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
