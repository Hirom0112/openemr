"""Deterministic generator for the synthetic admission/intake fixture PDF.

Synthetic data only. Names/MRNs/DOBs are fabricated.
Run: python agent-api/tests/fixtures/_generate_intake_admission.py
"""

from __future__ import annotations

import os
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "intake_admission.pdf"


def _draw_header(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, "ADMISSION HISTORY")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 10.05 * inch, "General Hospital - Inpatient Admissions")
    c.drawString(1 * inch, 9.85 * inch, "5678 Care Lane, Anytown, ST 00000")


def _draw_demographics(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.4 * inch, "Patient Demographics")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 9.15 * inch, "Name: Marcus Webb")
    c.drawString(1 * inch, 8.95 * inch, "DOB: 1962-03-14")
    c.drawString(1 * inch, 8.75 * inch, "MRN: 100847")
    c.drawString(1 * inch, 8.55 * inch, "Sex: M")
    c.drawString(1 * inch, 8.35 * inch, "Address: 12 Elm Street, Anytown, ST")


def _draw_chief_concern(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 7.95 * inch, "CHIEF CONCERN")
    c.setFont("Helvetica", 10)
    c.drawString(
        1 * inch,
        7.7 * inch,
        "Shortness of breath and fever for 2 days; presented to ED via EMS.",
    )


def _draw_allergies(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 7.25 * inch, "ALLERGIES")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 7.0 * inch, "Penicillin - rash")


def _draw_medications(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 6.55 * inch, "CURRENT MEDICATIONS")
    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, 6.3 * inch, "Lisinopril 10 mg PO daily")


def _draw_code_status(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 5.85 * inch, "CODE STATUS: Full Code")


def _draw_footer(c: canvas.Canvas) -> None:
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 1 - Synthetic admission record")


def generate(out_path: Path = OUT) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_path), pagesize=LETTER)
    c.setTitle("Admission History - Marcus Webb (synthetic)")
    c.setAuthor("synthetic-fixture")
    c.setSubject("synthetic")
    c.setCreator("agent-api fixture generator")
    os.environ.setdefault("SOURCE_DATE_EPOCH", "1700000000")
    _draw_header(c)
    _draw_demographics(c)
    _draw_chief_concern(c)
    _draw_allergies(c)
    _draw_medications(c)
    _draw_code_status(c)
    _draw_footer(c)
    c.showPage()
    c.save()
    return out_path


if __name__ == "__main__":
    p = generate()
    print(f"wrote {p} ({p.stat().st_size} bytes)")
