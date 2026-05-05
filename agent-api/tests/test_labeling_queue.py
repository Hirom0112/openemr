"""Wave 2E — round-trip tests for the labeling queue export/import scripts.

The export pipeline reads a ``repoint_trace.jsonl`` artifact and emits a
Label Studio task list. The import pipeline reads a Label Studio export
and emits per-case ``.gt.json`` sidecars. Both paths must round-trip
without losing case_id identity.

These tests are isolated — they touch tmp_path only and pull no live
fixtures.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.hard_failure


# Ensure the agent-api root is on sys.path so ``scripts.*`` imports resolve
# regardless of pytest invocation cwd.
_AGENT_API = Path(__file__).resolve().parent.parent
if str(_AGENT_API) not in sys.path:
    sys.path.insert(0, str(_AGENT_API))


from scripts import labeling_queue_export, labeling_queue_import  # noqa: E402


def _write_trace(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )


def test_export_reads_trace_and_produces_label_studio_tasks(tmp_path: Path):
    trace = tmp_path / "repoint_trace.jsonl"
    events = [
        # 3 disagreement-pool events (no_match) — should land in the queue.
        {
            "event": "extractor_citation_repointed",
            "case_id": f"case_d{i}",
            "tool": "intake",
            "field_name": "current_medications",
            "outcome": "no_match",
            "from": "block-A", "to": "block-A",
            "candidate_count": 0,
            "chosen_bbox_id": "block-A",
            "chosen_granularity": "line",
            "anchor_bbox_id": None,
            "anchor_text_preview": None,
            "y_distance": None,
            "value_preview": "lisinopril 10mg",
            "nearest_label_score": 0.0,
            "nearest_label_used": False,
        }
        for i in range(3)
    ] + [
        # 2 "kept" events — outside the disagreement pool, should NOT be picked.
        {
            "event": "extractor_citation_repointed",
            "case_id": f"case_k{i}",
            "outcome": "kept",
            "value_preview": "irrelevant",
        }
        for i in range(2)
    ]
    _write_trace(trace, events)

    out = tmp_path / "tasks.json"
    rc = labeling_queue_export.main([
        "--trace", str(trace),
        "--output", str(out),
        "--top-disagreement", "10",
        "--top-hard-modality", "10",
    ])
    assert rc == 0
    tasks = json.loads(out.read_text())

    assert isinstance(tasks, list) and len(tasks) == 3
    case_ids = {t["data"]["case_id"] for t in tasks}
    assert case_ids == {"case_d0", "case_d1", "case_d2"}
    # Predictions seed must be present so the annotator only adjusts.
    for t in tasks:
        assert t["predictions"]
        assert t["predictions"][0]["result"][0]["type"] == "rectanglelabels"
    # PHI safety: no field beyond ``value_preview`` (32 chars max from the
    # extractor) leaks into the task data.
    for t in tasks:
        assert len(t["data"]["value_preview"]) <= 64


def test_export_falls_back_to_first_n_when_no_disagreement(tmp_path: Path):
    """An empty disagreement pool should still produce a non-empty queue."""
    trace = tmp_path / "trace.jsonl"
    events = [
        {
            "event": "extractor_citation_repointed",
            "case_id": f"case_{i}",
            "outcome": "kept",
            "value_preview": "v",
        }
        for i in range(5)
    ]
    _write_trace(trace, events)
    out = tmp_path / "tasks.json"
    rc = labeling_queue_export.main([
        "--trace", str(trace),
        "--output", str(out),
        "--top-disagreement", "3",
        "--top-hard-modality", "0",
    ])
    assert rc == 0
    tasks = json.loads(out.read_text())
    assert len(tasks) == 3


def test_import_reads_label_studio_export_and_emits_sidecars(tmp_path: Path):
    export = [
        {
            "id": 1,
            "data": {"case_id": "case_d0", "field_name": "current_medications"},
            "annotations": [
                {
                    "id": 11,
                    "completed_by": "alice",
                    "result": [
                        {
                            "from_name": "bbox",
                            "to_name": "image",
                            "type": "rectanglelabels",
                            "value": {
                                "x": 12.0, "y": 30.0,
                                "width": 18.0, "height": 5.0,
                                "rectanglelabels": ["value"],
                            },
                        }
                    ],
                }
            ],
        },
        {
            "id": 2,
            "data": {"case_id": "case_d1", "field_name": "allergies"},
            "annotations": [
                {
                    "result": [
                        {
                            "type": "rectanglelabels",
                            "value": {
                                "x": 5.0, "y": 10.0,
                                "width": 20.0, "height": 4.0,
                                "rectanglelabels": ["value"],
                            },
                        }
                    ]
                }
            ],
        },
        # Skipped: no annotations completed.
        {"id": 3, "data": {"case_id": "case_skip"}, "annotations": []},
    ]
    export_path = tmp_path / "ls_export.json"
    export_path.write_text(json.dumps(export))

    out_dir = tmp_path / "annotated"
    rc = labeling_queue_import.main([
        "--export", str(export_path),
        "--output-dir", str(out_dir),
    ])
    assert rc == 0

    files = sorted(out_dir.glob("*.gt.json"))
    assert [p.name for p in files] == ["case_d0.gt.json", "case_d1.gt.json"]

    sidecar = json.loads((out_dir / "case_d0.gt.json").read_text())
    assert sidecar["fields"][0]["bbox"] == {"x": 12.0, "y": 30.0, "w": 18.0, "h": 5.0}
    assert sidecar["fields"][0]["name"] == "current_medications"
    assert sidecar["_provenance"]["source"] == "label_studio"


def test_round_trip_export_then_import(tmp_path: Path):
    """A trace round-trips: export produces tasks, the (simulated) annotated
    export imports back to a sidecar keyed by the same case_id.
    """
    trace = tmp_path / "trace.jsonl"
    _write_trace(
        trace,
        [
            {
                "event": "extractor_citation_repointed",
                "case_id": "round_trip_001",
                "field_name": "allergies",
                "outcome": "no_match",
                "value_preview": "penicillin",
            }
        ],
    )
    queue_path = tmp_path / "queue.json"
    labeling_queue_export.main([
        "--trace", str(trace),
        "--output", str(queue_path),
        "--top-disagreement", "1",
        "--top-hard-modality", "0",
    ])
    tasks = json.loads(queue_path.read_text())
    # Simulate an annotator completing the task.
    tasks[0]["annotations"] = [
        {
            "id": 99,
            "result": [
                {
                    "type": "rectanglelabels",
                    "value": {
                        "x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0,
                        "rectanglelabels": ["value"],
                    },
                }
            ],
        }
    ]
    completed = tmp_path / "completed.json"
    completed.write_text(json.dumps(tasks))

    out_dir = tmp_path / "annotated"
    labeling_queue_import.main([
        "--export", str(completed),
        "--output-dir", str(out_dir),
    ])
    sidecars = list(out_dir.glob("*.gt.json"))
    assert len(sidecars) == 1
    sidecar = json.loads(sidecars[0].read_text())
    assert sidecar["fields"][0]["name"] == "allergies"
    assert sidecar["fields"][0]["bbox"] == {"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0}
