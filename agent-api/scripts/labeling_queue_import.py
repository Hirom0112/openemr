#!/usr/bin/env python3
"""Import a Label Studio export back into bbox-GT sidecars.

Reads a Label Studio JSON export (the format produced by
``Project -> Export -> JSON`` in Label Studio's UI) and emits one
``<case_id>.gt.json`` file per task into
``agent-api/tests/fixtures/annotated/``.

The output sidecar shape matches the existing Wave 2C contract used by
``evals/run_full_suite.py:_score_bbox_rubrics`` — ``{"fields": [{"name":
str, "bbox": {"x", "y", "w", "h"}, "page": int}, ...]}`` — so the
existing IoU + pixel-distance rubrics pick up the new GT without any
rubric-side change.

This script never invents data. If a Label Studio task has no
completion (``annotations`` empty) it is skipped with a warning. PHI
sanitization: we drop the ``data.value_preview`` and any free-text
``rectanglelabels`` value before persisting (the sidecar carries
geometry only).

Usage:
    python3 -m scripts.labeling_queue_import \
        --export queue/label_studio_export.json \
        --output-dir agent-api/tests/fixtures/annotated
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional


def _first_bbox_result(annotation: dict) -> Optional[dict]:
    """Pull the first rectanglelabels result out of a Label Studio annotation."""
    for r in annotation.get("result") or []:
        if r.get("type") not in ("rectanglelabels", "rectangle"):
            continue
        v = r.get("value") or {}
        if all(k in v for k in ("x", "y", "width", "height")):
            return v
    return None


def _ls_value_to_bbox(v: dict) -> dict:
    """Translate Label Studio's percentage bbox into the sidecar's ``{x,y,w,h}``.

    Label Studio reports x/y/width/height as percentages of the original
    image (0-100). The sidecar contract is the same percentage-based
    shape used by the existing Wave 2C generator, so we only rename
    width->w and height->h.
    """
    return {
        "x": float(v["x"]),
        "y": float(v["y"]),
        "w": float(v["width"]),
        "h": float(v["height"]),
    }


def _task_to_sidecar(task: dict) -> Optional[tuple[str, dict]]:
    """Convert one LS task to a (case_id, sidecar) pair.

    Returns ``None`` when the task has no annotation we can persist
    (no completion, or a completion with zero rectangle results).
    """
    data = task.get("data") or {}
    case_id = data.get("case_id")
    if not case_id:
        return None
    annotations = task.get("annotations") or task.get("completions") or []
    if not annotations:
        return None
    # Use the most-recent annotation; LS appends new completions.
    annotation = annotations[-1]
    bbox_value = _first_bbox_result(annotation)
    if bbox_value is None:
        return None
    page = int(data.get("page", 1))
    field_name = str(data.get("field_name") or "value")
    sidecar = {
        "fields": [
            {
                "name": field_name,
                "bbox": _ls_value_to_bbox(bbox_value),
                "page": page,
            }
        ],
        "_provenance": {
            "source": "label_studio",
            "annotation_id": annotation.get("id"),
            "annotator": (annotation.get("completed_by") if isinstance(annotation.get("completed_by"), str) else None),
        },
    }
    return case_id, sidecar


def import_export(
    export: list[dict] | dict,
    output_dir: Path,
) -> list[Path]:
    """Persist sidecars and return the list of paths written."""
    if isinstance(export, dict):
        # Some LS export shapes wrap the task list in a top-level key.
        tasks = export.get("tasks") or export.get("data") or []
    else:
        tasks = export
    if not isinstance(tasks, list):
        raise ValueError("Label Studio export must decode to a list of tasks")

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        result = _task_to_sidecar(task)
        if result is None:
            continue
        case_id, sidecar = result
        # case_id is trusted but we still defang any path separators that
        # would let an annotation file write outside the target directory.
        safe = case_id.replace("/", "_").replace("\\", "_")
        path = output_dir / f"{safe}.gt.json"
        path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True,
                        help="Path to a Label Studio JSON export")
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "annotated",
        help="Where to write per-case .gt.json files",
    )
    args = parser.parse_args(argv)

    if not args.export.exists():
        print(f"ERROR: export file not found: {args.export}", file=sys.stderr)
        return 2

    export = json.loads(args.export.read_text(encoding="utf-8"))
    written = import_export(export, args.output_dir)
    print(f"Wrote {len(written)} sidecars under {args.output_dir}")
    for p in written:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
