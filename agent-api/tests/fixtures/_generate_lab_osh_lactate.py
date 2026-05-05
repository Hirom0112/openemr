"""Deterministic generator for the OSH lactate lab fixture PDF.

Synthetic data only. Names/MRNs/DOBs are fabricated.
Run: python agent-api/tests/fixtures/_generate_lab_osh_lactate.py
"""

from __future__ import annotations

import os
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "lab_osh_lactate.pdf"


def _draw_header(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "OUTSIDE HOSPITAL LABORATORY REPORT")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "Memorial Outside Hospital - Clinical Lab")
    c.drawString(1 * inch, 9.85 * inch, "1234 Hospital Way, Anytown, ST 00000")


def _draw_demographics(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.4 * inch, "Patient Information")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.15 * inch, "Name: Marcus Webb")
    c.drawString(1 * inch, 8.95 * inch, "DOB: 1962-03-14")
    c.drawString(1 * inch, 8.75 * inch, "MRN: 100847")
    c.drawString(1 * inch, 8.55 * inch, "Sex: Male")
    c.drawString(1 * inch, 8.35 * inch, "Collected: 2026-04-30 06:14")
    c.drawString(1 * inch, 8.15 * inch, "Accession: A-2026-0099847")


def _draw_page1_summary(c: canvas.Canvas) -> None:
    _draw_header(c)
    _draw_demographics(c)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 7.6 * inch, "Summary")
    c.setFont("Helvetica", 10)
    c.drawString(
        1 * inch,
        7.35 * inch,
        "Selected results follow on page 2. Critical values flagged with HH/LL.",
    )
    c.drawString(1 * inch, 7.15 * inch, "Ordering provider: Dr. Renee Castillo, MD")
    c.drawString(1 * inch, 6.95 * inch, "Specimen: Whole blood, venous")
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 2")


def _draw_results_row(
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


def _draw_page2_results(c: canvas.Canvas) -> None:
    _draw_header(c)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.6 * inch, "Patient: Marcus Webb  MRN: 100847  DOB: 1962-03-14")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(1 * inch, 9.1 * inch, "Selected Chemistry / Hematology")

    # column headers
    c.setFont("Helvetica-Bold", 10)
    c.drawString(1.0 * inch, 8.7 * inch, "Test")
    c.drawString(2.7 * inch, 8.7 * inch, "Value")
    c.drawString(3.5 * inch, 8.7 * inch, "Unit")
    c.drawString(4.6 * inch, 8.7 * inch, "Reference")
    c.drawString(6.4 * inch, 8.7 * inch, "Flag")
    c.line(1.0 * inch, 8.6 * inch, 7.5 * inch, 8.6 * inch)

    # data rows — ample vertical spacing so PyMuPDF emits each as its own block
    _draw_results_row(c, 8.30 * inch, "Lactate",  "4.2",   "mmol/L", "0.5 - 2.2",   "HH")
    _draw_results_row(c, 7.90 * inch, "WBC",      "14.3",  "K/uL",   "4.0 - 11.0",  "H")
    _draw_results_row(c, 7.50 * inch, "Cr",       "1.4",   "mg/dL",  "0.6 - 1.3",   "H")
    _draw_results_row(c, 7.10 * inch, "Sodium",   "138",   "mEq/L",  "136 - 145",   "")

    c.setFont("Helvetica", 9)
    c.drawString(1 * inch, 6.5 * inch, "HH = critical high, H = high, LL = critical low, L = low.")
    c.drawString(1 * inch, 6.3 * inch, "Reference ranges per institutional adult norms.")
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 2 of 2")


def generate(out_path: Path = OUT) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Deterministic PDF: pin metadata so byte output is stable run-to-run.
    c = canvas.Canvas(str(out_path), pagesize=LETTER)
    c.setTitle("OSH Lab Report - Marcus Webb (synthetic)")
    c.setAuthor("synthetic-fixture")
    c.setSubject("synthetic")
    c.setCreator("agent-api fixture generator")
    # Pin the creation date so the PDF is byte-deterministic.
    os.environ.setdefault("SOURCE_DATE_EPOCH", "1700000000")
    _draw_page1_summary(c)
    c.showPage()
    _draw_page2_results(c)
    c.showPage()
    c.save()
    return out_path


if __name__ == "__main__":
    p = generate()
    print(f"wrote {p} ({p.stat().st_size} bytes)")
