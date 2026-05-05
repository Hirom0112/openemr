"""Smoke-subset definition for push-time CI.

Selects a deterministic, representative 10-case subset from the full 124-case
``CASES`` list. The selection rule is:

  - 2 × typed_pdf     (consultant_note fixture — text-layer PDF, prose)
  - 2 × intake_form   (structured intake / triage form, text-layer)
  - 1 × scanned_pdf   (raster PNG, OCR-required)
  - 1 × table_heavy   (dense lab tabular data)
  - 1 × multi_column  (imaging report, multi-column layout)
  - 1 × synthetic     (programmatically generated, ground-truth available)
  - 1 × photo_capture (phone-photographed document, bbox_gt bucket)
  - 1 × bbox_gt       (synthetic_v2 fixture with GT bbox sidecar)

Total: 10 cases. IDs are hard-coded so every run is byte-identical and the
cost is predictably bounded (~$1 API spend vs ~$15-30 for the full suite).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Hard-coded smoke case IDs — DO NOT change without updating the test.
# Selection rule documented in the module docstring above.
# ---------------------------------------------------------------------------

SMOKE_CASE_IDS: tuple[str, ...] = (
    # typed_pdf (2) — consultant_note fixture → document_modality=typed_pdf
    "unknown_nominal_001_consultant_note_no_hint",
    "unknown_nominal_002_consultant_note_unknown_hint",
    # intake_form (2) — structured intake / triage form
    "intake_nominal_001_full_code",
    "intake_nominal_004_dnr_no_hint",
    # scanned_pdf (1) — raster PNG HbA1c report, OCR-required
    "lab_nominal_011_lipid_repeat",
    # table_heavy (1) — dense OSH lactate lab tabular data
    "lab_nominal_001_osh_lactate",
    # multi_column (1) — imaging report, multi-column layout
    "unknown_nominal_003_imaging_no_hint",
    # synthetic (1) — programmatically generated lab report, ground-truth available
    "lab_nominal_004_cbc_bmp_no_hint",
    # photo_capture (1) — phone-photographed intake form with bbox GT sidecar
    "bbox_gt_photo_001",
    # bbox_gt (1) — synthetic_v2 typed-PDF fixture with field-level GT bbox
    "bbox_gt_typed_001",
)
