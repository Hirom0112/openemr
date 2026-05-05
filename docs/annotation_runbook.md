# Annotation Runbook — Wave 2E Bbox GT Pipeline

This runbook is for a non-engineer annotator. By the end of it you will:

1. Install Label Studio locally (one command).
2. Receive an annotation queue exported by the eval team.
3. Draw bounding boxes around the cited values in roughly 20 documents.
4. Export your work and hand it back to the team.

You do not need to write any code. You do need a Mac or Linux laptop, a
terminal, and ~1 hour for the first batch.

---

## 0. Prerequisites

- Python 3.10+ on your laptop. Check with `python3 --version`.
- ~500 MB of disk space.
- A copy of the eval team's `repoint_trace.jsonl` artifact and a folder
  of the source documents (PDF / PNG). The team will share these via
  the secure document drop, never email.

---

## 1. Install Label Studio

In a terminal:

```bash
pip install --user label-studio
label-studio start
```

Label Studio opens a browser tab at `http://localhost:8080`. Create an
account with any email — it is local, no data leaves your machine.

If `pip` complains about an unmanaged environment, use a venv:

```bash
python3 -m venv ~/ls-venv
source ~/ls-venv/bin/activate
pip install label-studio
label-studio start
```

---

## 2. Get your queue

The eval team will hand you two files:

- `label_studio_tasks.json` — your queue of ~20 documents.
- `documents/` — a folder containing the source PDFs / PNGs.

**Generation note for the team (not the annotator):** to produce
`label_studio_tasks.json` from a recent CI run's artifact:

```bash
cd agent-api
python3 -m scripts.labeling_queue_export \
    --trace ../artifacts/repoint_trace.jsonl \
    --output ../queue/label_studio_tasks.json \
    --top-disagreement 10 \
    --top-hard-modality 10
```

The script reads `repoint_trace.jsonl` (uploaded as a CI artifact by the
`copilot-eval` workflow) and emits exactly N tasks. No PHI in the
queue: only a 32-character `value_preview` carries any document text,
matching the hash-equivalent preview the extractor logs.

---

## 3. Public datasets — license stance

We do **not** bundle FUNSD, CORD, or DocVQA in this repo. Their licenses
permit research use but restrict redistribution. The exporter ships the
shape; the team downloads the data on-demand to a CI cache that is never
committed back. If you need extra seed examples for practice, ask the
team to point you at the documents drop — do not download FUNSD yourself.

---

## 4. Set up the Label Studio project

1. In Label Studio click **Create Project**.
2. Name it `wave-2e-bbox-gt` (any name is fine).
3. Under **Data Import**, drag `label_studio_tasks.json` onto the page.
4. Under **Labeling Setup**, choose **Object Detection with Bounding
   Boxes**. Set the single label name to `value`.
5. Save.

Label Studio will show you a list of ~20 tasks. Each carries a
`value_preview` (the first 32 characters of the value the extractor
thought it found) and a `field_name` (e.g. `current_medications`).

---

## 5. Annotate

For each task:

1. Open the document image (Label Studio renders the PDF or PNG).
2. Find the **value** named in `value_preview`. The
   `extractor_anchor_text_preview` field hints at which header / label
   sits next to it.
3. Draw a tight bounding box around **just** the value text — not the
   header, not the entire row.
4. Click **Submit**.

QA sanity rules — fail any task that violates:

- The bounding box must lie entirely inside the page bounds (Label
  Studio enforces this; if you see a red outline, redraw).
- The text inside the box should match the `value_preview` (allowing
  for the 32-character truncation).
- Skip a task only if the value is **not visible** in the document.
  Use the **Skip** button; do not submit an empty box.

---

## 6. Export your work

1. From the project page click **Export**.
2. Choose format **JSON**.
3. Save the file as `label_studio_export.json`.

Hand `label_studio_export.json` back to the eval team via the same
secure drop you used to receive the queue.

---

## 7. Team-side import (for the engineer)

Once the export is back:

```bash
cd agent-api
python3 -m scripts.labeling_queue_import \
    --export ../queue/label_studio_export.json \
    --output-dir tests/fixtures/annotated
```

Each completed task becomes one `tests/fixtures/annotated/<case_id>.gt.json`
sidecar with the same shape the existing `citation_iou` /
`citation_pixel_distance` rubrics already consume. The next CI run picks
them up automatically — no rubric or wiring changes needed.

---

## 8. What happens next

The next eval run will score the extractor's bboxes against your
ground truth. Results land in `eval_results.md` under the `citation_iou`
and `citation_pixel_distance_detail` rows, and the per-modality
breakdown at the bottom shows whether the pass rate held for the
modality you annotated.

If a regression appears, the team triages — annotators are not on the
hook for fixing model output. Your job is the ground truth, full stop.
