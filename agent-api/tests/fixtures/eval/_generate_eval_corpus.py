"""Deterministic generator for the W2 eval-corpus PDFs.

Synthetic data only. All names, MRNs, DOBs are fabricated — see
``tests/test_w2_eval_no_real_phi.py`` for the maintained whitelist.

Produces the new fixtures referenced by ``tests/fixtures/w2_eval_cases.py``;
the two pre-existing fixtures (``lab_osh_lactate.pdf`` and
``intake_admission.pdf``) are NOT regenerated, only re-pointed.

Run: ``python3 agent-api/tests/fixtures/eval/_generate_eval_corpus.py``
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# Pin a stable creation epoch so PDFs are byte-deterministic across runs.
os.environ.setdefault("SOURCE_DATE_EPOCH", "1700000000")

EVAL_DIR = Path(__file__).parent
FIXTURES_DIR = EVAL_DIR.parent  # agent-api/tests/fixtures

# Pre-existing fixtures we just point at — do NOT regenerate.
EXISTING_LAB_OSH_LACTATE = FIXTURES_DIR / "lab_osh_lactate.pdf"
EXISTING_INTAKE_ADMISSION = FIXTURES_DIR / "intake_admission.pdf"

# Real-shaped clinical documents (synthetic identities, safe to commit). Used
# by 8 eval cases to exercise the pipeline against realistic layouts and a
# raster-PNG OCR path. NOT regenerated — files live on disk under
# ``real-examples/`` and are committed to the repo.
REAL_EXAMPLES_DIR = EVAL_DIR / "real-examples"
_REAL_FIXTURES: dict[str, str] = {
    "chen_lab_lipid":      "p01-chen-lipid-panel.pdf",
    "whitaker_lab_cbc":    "p02-whitaker-cbc.pdf",
    "reyes_lab_hba1c_png": "p03-reyes-hba1c.png",
    "kowalski_lab_cmp":    "p04-kowalski-cmp.pdf",
    "chen_intake_typed":   "p01-chen-intake-typed.pdf",
    "whitaker_intake":     "p02-whitaker-intake.pdf",
    "reyes_intake_png":    "p03-reyes-intake.png",
    "kowalski_intake_png": "p04-kowalski-intake.png",
}


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def _new_canvas(out_path: Path, title: str) -> canvas.Canvas:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_path), pagesize=LETTER)
    c.setTitle(title)
    c.setAuthor("synthetic-fixture")
    c.setSubject("synthetic")
    c.setCreator("agent-api eval-corpus generator")
    return c


def _row(
    c: canvas.Canvas,
    y: float,
    test: str,
    value: str,
    unit: str,
    ref: str,
    flag: str,
) -> None:
    c.setFont("Helvetica", 10)
    c.drawString(1.0 * inch, y, test)
    c.drawString(2.7 * inch, y, value)
    c.drawString(3.5 * inch, y, unit)
    c.drawString(4.6 * inch, y, ref)
    c.drawString(6.4 * inch, y, flag)


# ---------------------------------------------------------------------------
# Lab fixtures
# ---------------------------------------------------------------------------


def _gen_lab_clean_2(out_path: Path) -> Path:
    """CBC + BMP, no critical flags. Synthetic patient: Jane Doe."""
    c = _new_canvas(out_path, "Lab Report - Jane Doe (synthetic)")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "CLINICAL LABORATORY REPORT")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "Riverside Medical Center - Lab Services")
    c.drawString(1 * inch, 9.85 * inch, "200 Riverside Way, Anytown, ST 00000")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.4 * inch, "Patient Information")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.15 * inch, "Name: Jane Doe")
    c.drawString(1 * inch, 8.95 * inch, "DOB: 1978-07-22")
    c.drawString(1 * inch, 8.75 * inch, "MRN: 100231")
    c.drawString(1 * inch, 8.55 * inch, "Sex: Female")
    c.drawString(1 * inch, 8.35 * inch, "Collected: 2026-04-29 09:10")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(1 * inch, 7.9 * inch, "CBC + BMP")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(1.0 * inch, 7.6 * inch, "Test")
    c.drawString(2.7 * inch, 7.6 * inch, "Value")
    c.drawString(3.5 * inch, 7.6 * inch, "Unit")
    c.drawString(4.6 * inch, 7.6 * inch, "Reference")
    c.drawString(6.4 * inch, 7.6 * inch, "Flag")
    c.line(1.0 * inch, 7.5 * inch, 7.5 * inch, 7.5 * inch)

    _row(c, 7.20 * inch, "WBC",      "6.8",  "K/uL",   "4.0 - 11.0", "")
    _row(c, 6.85 * inch, "Hgb",      "13.4", "g/dL",   "12.0 - 15.5", "")
    _row(c, 6.50 * inch, "Plt",      "238",  "K/uL",   "150 - 400",  "")
    _row(c, 6.15 * inch, "Sodium",   "140",  "mEq/L",  "136 - 145",  "")
    _row(c, 5.80 * inch, "Potassium","4.1",  "mEq/L",  "3.5 - 5.0",  "")
    _row(c, 5.45 * inch, "Cr",       "0.9",  "mg/dL",  "0.6 - 1.3",  "")
    _row(c, 5.10 * inch, "Glucose",  "92",   "mg/dL",  "70 - 110",   "")

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 1")
    c.showPage()
    c.save()
    return out_path


def _gen_lab_clean_3(out_path: Path) -> Path:
    """Lipid panel, all in range. Synthetic patient: Carlos Reyes."""
    c = _new_canvas(out_path, "Lab Report - Carlos Reyes (synthetic)")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "LIPID PANEL")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "Northside Outpatient Lab")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.4 * inch, "Patient Information")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.15 * inch, "Name: Carlos Reyes")
    c.drawString(1 * inch, 8.95 * inch, "DOB: 1955-11-02")
    c.drawString(1 * inch, 8.75 * inch, "MRN: 100412")
    c.drawString(1 * inch, 8.55 * inch, "Sex: Male")
    c.drawString(1 * inch, 8.35 * inch, "Collected: 2026-04-28 07:45")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1.0 * inch, 7.6 * inch, "Test")
    c.drawString(2.7 * inch, 7.6 * inch, "Value")
    c.drawString(3.5 * inch, 7.6 * inch, "Unit")
    c.drawString(4.6 * inch, 7.6 * inch, "Reference")
    c.drawString(6.4 * inch, 7.6 * inch, "Flag")
    c.line(1.0 * inch, 7.5 * inch, 7.5 * inch, 7.5 * inch)

    _row(c, 7.2 * inch, "Total Cholesterol", "182", "mg/dL", "<200",       "")
    _row(c, 6.85 * inch, "LDL",              "104", "mg/dL", "<130",       "")
    _row(c, 6.50 * inch, "HDL",              "55",  "mg/dL", ">40",        "")
    _row(c, 6.15 * inch, "Triglycerides",    "118", "mg/dL", "<150",       "")

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 1")
    c.showPage()
    c.save()
    return out_path


def _gen_lab_critical_4(out_path: Path) -> Path:
    """Critical glucose with HH flag. Synthetic patient: Priya Natarajan."""
    c = _new_canvas(out_path, "Lab Report - Priya Natarajan (synthetic)")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "STAT GLUCOSE")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "Mercy ED Lab")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.4 * inch, "Patient Information")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.15 * inch, "Name: Priya Natarajan")
    c.drawString(1 * inch, 8.95 * inch, "DOB: 1990-01-19")
    c.drawString(1 * inch, 8.75 * inch, "MRN: 100558")
    c.drawString(1 * inch, 8.55 * inch, "Sex: Female")
    c.drawString(1 * inch, 8.35 * inch, "Collected: 2026-04-30 02:55")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(1.0 * inch, 7.6 * inch, "Test")
    c.drawString(2.7 * inch, 7.6 * inch, "Value")
    c.drawString(3.5 * inch, 7.6 * inch, "Unit")
    c.drawString(4.6 * inch, 7.6 * inch, "Reference")
    c.drawString(6.4 * inch, 7.6 * inch, "Flag")
    c.line(1.0 * inch, 7.5 * inch, 7.5 * inch, 7.5 * inch)
    _row(c, 7.2 * inch, "Glucose", "612", "mg/dL", "70 - 110", "HH")
    _row(c, 6.85 * inch, "Anion Gap", "22", "mEq/L", "4 - 12", "H")

    c.setFont("Helvetica", 9)
    c.drawString(1 * inch, 6.4 * inch, "HH = critical high. Provider notified per policy.")
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 1")
    c.showPage()
    c.save()
    return out_path


def _gen_lab_blurry(out_path: Path) -> Path:
    """Same shape as lab_clean_2 but rendered in faint gray to simulate
    low-confidence OCR. Real OCR confidence flag is asserted in the case
    metadata, not via the PDF — but the visual cue is still there."""
    c = _new_canvas(out_path, "Lab Report - Liang Park (synthetic, blurry)")
    c.setFillColor(colors.grey)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "CLINICAL LABORATORY REPORT")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "Westgate Faxed Lab Services")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.4 * inch, "Patient Information")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.15 * inch, "Name: Liang Park")
    c.drawString(1 * inch, 8.95 * inch, "DOB: 1971-09-30")
    c.drawString(1 * inch, 8.75 * inch, "MRN: 100719")
    c.drawString(1 * inch, 8.55 * inch, "Sex: Male")

    _row(c, 7.6 * inch, "WBC", "9.1", "K/uL", "4.0 - 11.0", "")
    _row(c, 7.2 * inch, "Hgb", "12.7", "g/dL", "13.0 - 17.0", "L")
    _row(c, 6.85 * inch, "Cr", "1.0", "mg/dL", "0.6 - 1.3", "")

    c.setFillColor(colors.black)
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 1 - faxed copy")
    c.showPage()
    c.save()
    return out_path


# ---------------------------------------------------------------------------
# Intake fixtures
# ---------------------------------------------------------------------------


def _intake(
    out_path: Path,
    *,
    title: str,
    name: str,
    dob: str,
    mrn: str,
    sex: str,
    chief: str,
    allergies: str,
    meds: str,
    code_status: str,
    extra_lines: tuple[str, ...] = (),
    blurry: bool = False,
) -> Path:
    c = _new_canvas(out_path, title)
    if blurry:
        c.setFillColor(colors.grey)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "ADMISSION HISTORY")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "General Hospital - Inpatient Admissions")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.4 * inch, "Patient Demographics")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.15 * inch, f"Name: {name}")
    c.drawString(1 * inch, 8.95 * inch, f"DOB: {dob}")
    c.drawString(1 * inch, 8.75 * inch, f"MRN: {mrn}")
    c.drawString(1 * inch, 8.55 * inch, f"Sex: {sex}")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 8.0 * inch, "CHIEF CONCERN")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 7.75 * inch, chief)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 7.3 * inch, "ALLERGIES")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 7.05 * inch, allergies)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 6.6 * inch, "CURRENT MEDICATIONS")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 6.35 * inch, meds)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 5.9 * inch, f"CODE STATUS: {code_status}")

    y = 5.4 * inch
    for line in extra_lines:
        c.setFont("Helvetica", 10)
        c.drawString(1 * inch, y, line)
        y -= 0.25 * inch

    if blurry:
        c.setFillColor(colors.black)
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 1 - Synthetic admission record")
    c.showPage()
    c.save()
    return out_path


def _gen_intake_dnr(out_path: Path) -> Path:
    return _intake(
        out_path,
        title="Admission - Eleanor Whitfield (synthetic)",
        name="Eleanor Whitfield",
        dob="1942-08-11",
        mrn="100923",
        sex="F",
        chief="Worsening dyspnea, advanced COPD; admitted for comfort optimization.",
        allergies="Sulfa - hives",
        meds="Tiotropium 18 mcg INH daily; Morphine 5 mg PO q4h PRN",
        code_status="DNR / DNI",
        extra_lines=(
            "Advance directive on file: yes",
            "Healthcare proxy: daughter, contact in chart",
        ),
    )


def _gen_intake_no_allergies(out_path: Path) -> Path:
    return _intake(
        out_path,
        title="Admission - Tomas Albright (synthetic)",
        name="Tomas Albright",
        dob="1985-02-26",
        mrn="100377",
        sex="M",
        chief="Right lower quadrant pain, suspected appendicitis.",
        allergies="NKDA",
        meds="None",
        code_status="Full Code",
    )


def _gen_intake_minimal(out_path: Path) -> Path:
    return _intake(
        out_path,
        title="Admission - Hannah Goldberg (synthetic)",
        name="Hannah Goldberg",
        dob="2001-12-04",
        mrn="100644",
        sex="F",
        chief="Migraine, intractable; admitted for IV therapy.",
        allergies="",
        meds="",
        code_status="",
    )


def _gen_intake_blurry(out_path: Path) -> Path:
    return _intake(
        out_path,
        title="Admission - Faxed Copy (synthetic, blurry)",
        name="Sven Halvorsen",
        dob="1968-05-17",
        mrn="100805",
        sex="M",
        chief="Acute on chronic back pain, faxed referral.",
        allergies="Codeine - nausea",
        meds="Gabapentin 300 mg TID",
        code_status="Full Code",
        blurry=True,
    )


# ---------------------------------------------------------------------------
# "Unknown" fixtures (consultant note, imaging report)
# ---------------------------------------------------------------------------


def _gen_consultant_note(out_path: Path) -> Path:
    c = _new_canvas(out_path, "Consultant Note (synthetic)")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "CARDIOLOGY CONSULT NOTE")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "Cardiology Service - Inpatient Consult")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.6 * inch, "Patient: Marcus Webb  MRN: 100847")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.1 * inch, "REASON FOR CONSULT")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 8.85 * inch, "Evaluate troponin elevation in setting of sepsis.")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 8.3 * inch, "ASSESSMENT")
    c.setFont("Helvetica", 10)
    text = c.beginText(1 * inch, 8.05 * inch)
    text.setFont("Helvetica", 10)
    for line in (
        "62-year-old man with sepsis and modest troponin rise, likely demand",
        "ischemia rather than primary ACS. No dynamic ECG changes. Continue",
        "source control and aggressive resuscitation. Will follow with serial",
        "troponins and recheck ECG in 6 hours.",
    ):
        text.textLine(line)
    c.drawText(text)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 6.5 * inch, "RECOMMENDATIONS")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 6.25 * inch, "1. Trend troponins q6h x 3.")
    c.drawString(1 * inch, 6.05 * inch, "2. Continue aspirin 81 mg daily.")
    c.drawString(1 * inch, 5.85 * inch, "3. No indication for cath at this time.")

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Dictated by: Dr. R. Castillo, Cardiology")
    c.showPage()
    c.save()
    return out_path


def _gen_imaging_report(out_path: Path) -> Path:
    c = _new_canvas(out_path, "Imaging Report (synthetic)")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "RADIOLOGY REPORT")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "Department of Radiology")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.6 * inch, "Patient: Jane Doe  MRN: 100231")
    c.drawString(1 * inch, 9.4 * inch, "Study: CT Chest with contrast")
    c.drawString(1 * inch, 9.2 * inch, "Date: 2026-04-29")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 8.7 * inch, "FINDINGS")
    c.setFont("Helvetica", 10)
    text = c.beginText(1 * inch, 8.45 * inch)
    for line in (
        "No pulmonary embolism. No focal consolidation. Mild basilar atelectasis.",
        "Heart size normal. No pericardial effusion. Mediastinum unremarkable.",
        "Visualized upper abdomen: unremarkable.",
    ):
        text.textLine(line)
    c.drawText(text)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 7.0 * inch, "IMPRESSION")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 6.75 * inch, "1. No acute cardiopulmonary process. No PE.")

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Read by: Dr. M. Kapur, Radiology")
    c.showPage()
    c.save()
    return out_path


# ---------------------------------------------------------------------------
# Edge-case fixtures
# ---------------------------------------------------------------------------


def _gen_blank(out_path: Path) -> Path:
    c = _new_canvas(out_path, "Blank (synthetic)")
    c.showPage()
    c.save()
    return out_path


def _gen_encrypted(out_path: Path) -> Path:
    """Not actually encrypted. Just contains the word 'ENCRYPTED' so the
    classifier/extractor can take a clean refusal path under tests."""
    c = _new_canvas(out_path, "Encrypted Stub (synthetic)")
    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(4.25 * inch, 5.5 * inch, "ENCRYPTED")
    c.setFont("Helvetica", 10)
    c.drawCentredString(
        4.25 * inch,
        5.0 * inch,
        "This document is password protected. Please contact the sender.",
    )
    c.showPage()
    c.save()
    return out_path


def _gen_empty_stream(out_path: Path) -> Path:
    """A single page with literally one space character — a degenerate
    'noise' PDF that will produce no text blocks worth extracting."""
    c = _new_canvas(out_path, "Empty Stream (synthetic)")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 5.5 * inch, " ")
    c.showPage()
    c.save()
    return out_path


def _gen_all_noise_scan(out_path: Path) -> Path:
    """Garbled 'OCR-noise' page — random punctuation patterns that no
    classifier should resolve."""
    c = _new_canvas(out_path, "All-Noise Scan (synthetic)")
    c.setFillColor(colors.lightgrey)
    c.setFont("Helvetica", 10)
    noise_lines = [
        "j8#@.. ::; ,, --- '' ?? @@ %% &&  ()() ////  ;;;",
        "..--..--..  qq  pp  ll  oo  ee  rr  tt  yy  uu",
        "*** !!! ??? ::: ;;; ,,, ... --- +++ === ___",
        "// // // \\\\ \\\\ \\\\  ...  ...  ...  ...  ...",
    ] * 6
    y = 9.5 * inch
    for line in noise_lines:
        c.drawString(1 * inch, y, line)
        y -= 0.2 * inch
    c.showPage()
    c.save()
    return out_path


def _gen_mixed_content(out_path: Path) -> Path:
    """Page 1: intake-form-shaped. Page 2: lab-shaped. Different patient names
    on each page so the extractor cannot collapse them."""
    c = _new_canvas(out_path, "Mixed Content (synthetic)")

    # Page 1 — intake-shaped
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "ADMISSION HISTORY")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "General Hospital")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.4 * inch, "Patient Demographics")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.15 * inch, "Name: Yara Olsson")
    c.drawString(1 * inch, 8.95 * inch, "DOB: 1980-06-09")
    c.drawString(1 * inch, 8.75 * inch, "MRN: 100488")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 8.2 * inch, "CHIEF CONCERN")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 7.95 * inch, "Productive cough x 1 week.")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 7.4 * inch, "CODE STATUS: Full Code")
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 2")
    c.showPage()

    # Page 2 — lab-shaped
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "CLINICAL LABORATORY REPORT")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.6 * inch, "Patient: Yara Olsson  MRN: 100488")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(1.0 * inch, 9.0 * inch, "Test")
    c.drawString(2.7 * inch, 9.0 * inch, "Value")
    c.drawString(3.5 * inch, 9.0 * inch, "Unit")
    c.drawString(4.6 * inch, 9.0 * inch, "Reference")
    c.drawString(6.4 * inch, 9.0 * inch, "Flag")
    c.line(1.0 * inch, 8.9 * inch, 7.5 * inch, 8.9 * inch)
    _row(c, 8.5 * inch, "WBC", "12.1", "K/uL", "4.0 - 11.0", "H")
    _row(c, 8.15 * inch, "Hgb", "13.0", "g/dL", "12.0 - 15.5", "")
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 2 of 2")
    c.showPage()
    c.save()
    return out_path


def _gen_intra_doc_conflict_lactate(out_path: Path) -> Path:
    """3 pages: page 1 says lactate 4.2, page 3 says lactate 2.4 — same
    patient, no addendum semantics. The conflict detector (§5.7) should
    surface both with a soft-warn."""
    c = _new_canvas(out_path, "Intra-doc Conflict (synthetic)")

    # Shared header drawer
    def _header(title: str) -> None:
        c.setFont("Helvetica-Bold", 14)
        c.drawString(1 * inch, 10.3 * inch, "OUTSIDE HOSPITAL LABORATORY REPORT")
        c.setFont("Helvetica", 10)
        c.drawString(1 * inch, 10.05 * inch, "Memorial Outside Hospital - Clinical Lab")
        c.setFont("Helvetica-Bold", 11)
        c.drawString(1 * inch, 9.6 * inch, "Patient: Marcus Webb  MRN: 100847  DOB: 1962-03-14")
        c.setFont("Helvetica-Bold", 12)
        c.drawString(1 * inch, 9.1 * inch, title)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(1.0 * inch, 8.7 * inch, "Test")
        c.drawString(2.7 * inch, 8.7 * inch, "Value")
        c.drawString(3.5 * inch, 8.7 * inch, "Unit")
        c.drawString(4.6 * inch, 8.7 * inch, "Reference")
        c.drawString(6.4 * inch, 8.7 * inch, "Flag")
        c.line(1.0 * inch, 8.6 * inch, 7.5 * inch, 8.6 * inch)

    # Page 1 — lactate 4.2
    _header("Initial Result")
    _row(c, 8.30 * inch, "Lactate", "4.2", "mmol/L", "0.5 - 2.2", "HH")
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 3")
    c.showPage()

    # Page 2 — interstitial / unrelated
    _header("Other Chemistry")
    _row(c, 8.30 * inch, "Sodium", "138", "mEq/L", "136 - 145", "")
    _row(c, 7.95 * inch, "Cr",     "1.2", "mg/dL", "0.6 - 1.3", "")
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 2 of 3")
    c.showPage()

    # Page 3 — lactate 2.4 (conflict)
    _header("Repeat Result")
    _row(c, 8.30 * inch, "Lactate", "2.4", "mmol/L", "0.5 - 2.2", "H")
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 3 of 3")
    c.showPage()
    c.save()
    return out_path


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_GENERATED: dict[str, tuple[str, Callable[[Path], Path]]] = {
    "lab_clean_2":               ("lab_clean_2.pdf",               _gen_lab_clean_2),
    "lab_clean_3":               ("lab_clean_3.pdf",               _gen_lab_clean_3),
    "lab_critical_4":            ("lab_critical_4.pdf",            _gen_lab_critical_4),
    "lab_blurry":                ("lab_blurry.pdf",                _gen_lab_blurry),
    "intake_dnr":                ("intake_dnr.pdf",                _gen_intake_dnr),
    "intake_no_allergies":       ("intake_no_allergies.pdf",       _gen_intake_no_allergies),
    "intake_minimal":            ("intake_minimal.pdf",            _gen_intake_minimal),
    "intake_blurry":             ("intake_blurry.pdf",             _gen_intake_blurry),
    "consultant_note":           ("consultant_note.pdf",           _gen_consultant_note),
    "imaging_report":            ("imaging_report.pdf",            _gen_imaging_report),
    "blank":                     ("blank.pdf",                     _gen_blank),
    "encrypted":                 ("encrypted.pdf",                 _gen_encrypted),
    "empty_stream":              ("empty_stream.pdf",              _gen_empty_stream),
    "all_noise_scan":            ("all_noise_scan.pdf",            _gen_all_noise_scan),
    "mixed_content":             ("mixed_content.pdf",             _gen_mixed_content),
    "intra_doc_conflict_lactate":("intra_doc_conflict_lactate.pdf",_gen_intra_doc_conflict_lactate),
}


def generate_all() -> dict[str, Path]:
    """Generate every fixture the eval set references.

    Returns a mapping from fixture key to absolute Path. Three classes of
    fixtures are NOT regenerated and must already exist on disk:

      * ``lab_osh_lactate`` / ``intake_admission`` — the original synthetic
        reportlab fixtures pre-dating this generator.
      * ``_REAL_FIXTURES`` (see top of module) — real-shaped clinical
        documents under ``real-examples/`` (synthetic identities, committed).

    The remaining ``_GENERATED`` keys are deterministically rebuilt every call.
    """
    out: dict[str, Path] = {}

    if not EXISTING_LAB_OSH_LACTATE.exists():
        raise FileNotFoundError(
            f"Pre-existing fixture missing: {EXISTING_LAB_OSH_LACTATE}. "
            "Generate it first via _generate_lab_osh_lactate.py."
        )
    if not EXISTING_INTAKE_ADMISSION.exists():
        raise FileNotFoundError(
            f"Pre-existing fixture missing: {EXISTING_INTAKE_ADMISSION}. "
            "Generate it first via _generate_intake_admission.py."
        )
    out["lab_osh_lactate"] = EXISTING_LAB_OSH_LACTATE
    out["intake_admission"] = EXISTING_INTAKE_ADMISSION

    for key, filename in _REAL_FIXTURES.items():
        path = REAL_EXAMPLES_DIR / filename
        if not path.exists():
            raise FileNotFoundError(
                f"Real-example fixture missing: {path}. These files are "
                "committed to the repo under tests/fixtures/eval/real-examples/."
            )
        out[key] = path

    for key, (filename, fn) in _GENERATED.items():
        path = EVAL_DIR / filename
        fn(path)
        out[key] = path

    # Wave 2C — merge in the synthetic_v2 corpus (typed_pdf / table_heavy /
    # photo_capture, all carrying bbox GT sidecars). The v2 generator is
    # standalone (separate module, separate output dir) but we register its
    # outputs here so the eval runner's fixture index resolves them via the
    # existing path. v2 is deterministic too, so re-running ``generate_all``
    # is idempotent.
    try:
        from tests.fixtures.eval._generate_synthetic_v2 import generate_all_v2  # type: ignore
    except Exception:
        try:
            from . import _generate_synthetic_v2  # type: ignore
            generate_all_v2 = _generate_synthetic_v2.generate_all_v2  # type: ignore[attr-defined]
        except Exception:
            generate_all_v2 = None  # type: ignore[assignment]
    if generate_all_v2 is not None:
        v2_paths = generate_all_v2()
        for key, path in v2_paths.items():
            out[key] = path
    return out


if __name__ == "__main__":
    paths = generate_all()
    for k, p in paths.items():
        size = p.stat().st_size if p.exists() else 0
        print(f"{k:32s} -> {p}  ({size} bytes)")
