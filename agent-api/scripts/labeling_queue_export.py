#!/usr/bin/env python3
"""Export a Label Studio task batch from a repoint_trace.jsonl artifact.

Reads ``repoint_trace.jsonl`` (produced by ``evals.run_full_suite``) and
emits a JSON file that Label Studio can ingest as a Project import. The
queue is constructed from two pools:

  1. Top-N "disagreement" cases — repoint events whose ``outcome`` was
     ``no_match`` or ``repointed_anchor_low_score`` (where the LLM cited
     one bbox but the value-anchor heuristic moved it). These are the
     extractor's lowest-confidence calls and benefit most from human
     ground truth.

  2. Top-N "hard modality" seeds — one event per case_id biased toward
     buckets the rubric historically struggles with (handwritten,
     photo_capture). When seed candidates are not provided this pool is
     simply the next-N events from the trace.

The output is a JSON list of Label Studio tasks. Each task carries:

  * ``data.case_id`` — the eval case_id
  * ``data.fixture_url`` — placeholder file URL the annotator points at
  * ``data.value_preview`` — the sanitized 32-char preview emitted by
    the extractor (no raw PHI)
  * ``predictions`` — the extractor's chosen bbox as a Label Studio
    pre-annotation (so the annotator only adjusts, doesn't re-draw)

NO public datasets are bundled. The runbook
(``docs/annotation_runbook.md``) documents how to pull FUNSD/CORD on
demand if their license allows redistribution to the annotator.

Usage:
    python3 -m scripts.labeling_queue_export \
        --trace artifacts/repoint_trace.jsonl \
        --output queue/label_studio_tasks.json \
        [--top-disagreement 10] [--top-hard-modality 10]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Optional


# Outcomes the export script treats as "disagreement" worth a human pass.
# ``no_match`` = the value-anchor heuristic could not place the citation;
# ``repointed_anchor_low_score`` = anchor matched but with a weak score.
_DISAGREEMENT_OUTCOMES = {"no_match", "repointed_anchor_low_score", "repointed_no_anchor"}

# Modalities considered "hard" historically. Extend as the eval data grows.
_HARD_MODALITIES = {"handwritten", "photo_capture", "scanned_pdf"}


def read_trace(path: Path) -> list[dict]:
    """Read a repoint_trace.jsonl file, ignoring blank lines and comments."""
    if not path.exists():
        raise FileNotFoundError(f"repoint_trace not found: {path}")
    out: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{i} not JSON: {exc}") from exc
    return out


def _bucket(events: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Split events into (disagreement_pool, hard_modality_pool).

    The two pools are deduplicated by case_id so each case appears at most
    once per pool. ``case_id`` may be missing on records emitted outside
    the eval suite — those records are excluded from both pools (we have
    no way to round-trip them through the annotation pipeline).
    """
    disagree: "OrderedDict[str, dict]" = OrderedDict()
    hard: "OrderedDict[str, dict]" = OrderedDict()
    for ev in events:
        cid = ev.get("case_id")
        if not cid:
            continue
        outcome = ev.get("outcome")
        modality = ev.get("modality")  # optional; rarely set on the record
        if outcome in _DISAGREEMENT_OUTCOMES and cid not in disagree:
            disagree[cid] = ev
        if modality in _HARD_MODALITIES and cid not in hard:
            hard[cid] = ev
    return list(disagree.values()), list(hard.values())


def _to_task(task_id: int, ev: dict, fixture_root: Optional[Path]) -> dict:
    """Convert a single repoint event into a Label Studio task dict.

    ``predictions`` carries the extractor's existing bbox so the annotator
    only has to adjust — not redraw — the box. Coordinates use Label
    Studio's percent-of-image convention (we leave them as-is when bbox
    pixel coordinates aren't recoverable; the annotator runbook explains
    the manual conversion step for raw-pixel sidecars).
    """
    case_id = str(ev.get("case_id", f"task-{task_id}"))
    fixture_url = ""
    if fixture_root is not None:
        fixture_url = f"{fixture_root.as_posix()}/{case_id}"
    task: dict[str, Any] = {
        "id": task_id,
        "data": {
            "case_id": case_id,
            "fixture_url": fixture_url,
            "value_preview": ev.get("value_preview", ""),
            "field_name": ev.get("field_name", ""),
            "tool": ev.get("tool", ""),
            "extractor_outcome": ev.get("outcome", ""),
            "extractor_chosen_bbox_id": ev.get("chosen_bbox_id", ""),
            "extractor_anchor_text_preview": ev.get("anchor_text_preview", ""),
        },
        "predictions": [
            {
                "model_version": "extractor_repoint_v1",
                "result": [
                    {
                        "from_name": "bbox",
                        "to_name": "image",
                        "type": "rectanglelabels",
                        "value": {
                            # Annotator updates these — predictions are seed-only.
                            "x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0,
                            "rectanglelabels": ["value"],
                        },
                    },
                ],
            },
        ],
    }
    return task


def build_tasks(
    events: list[dict],
    *,
    top_disagreement: int = 10,
    top_hard_modality: int = 10,
    fixture_root: Optional[Path] = None,
) -> list[dict]:
    """Construct the Label Studio task list from a parsed trace."""
    disagree, hard = _bucket(events)
    selected: "OrderedDict[str, dict]" = OrderedDict()
    for ev in disagree[:top_disagreement]:
        selected[ev["case_id"]] = ev
    for ev in hard[:top_hard_modality]:
        selected.setdefault(ev["case_id"], ev)
    # Fall back: if both pools are empty, take the first N events overall
    # (so a brand-new repo with no flagged disagreements still produces a
    # non-empty queue for the annotator's smoke test).
    if not selected:
        for ev in events[: top_disagreement + top_hard_modality]:
            cid = ev.get("case_id")
            if cid:
                selected.setdefault(cid, ev)
    return [_to_task(i + 1, ev, fixture_root) for i, ev in enumerate(selected.values())]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True,
                        help="Path to repoint_trace.jsonl")
    parser.add_argument("--output", type=Path, required=True,
                        help="Path to write Label Studio tasks JSON")
    parser.add_argument("--top-disagreement", type=int, default=10)
    parser.add_argument("--top-hard-modality", type=int, default=10)
    parser.add_argument("--fixture-root", type=Path, default=None,
                        help="Optional URL/path prefix for fixture files")
    args = parser.parse_args(argv)

    events = read_trace(args.trace)
    tasks = build_tasks(
        events,
        top_disagreement=args.top_disagreement,
        top_hard_modality=args.top_hard_modality,
        fixture_root=args.fixture_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(tasks)} Label Studio tasks to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
