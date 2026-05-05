"""Generate three synthetic guideline PDFs for the hybrid-RAG corpus.

These are FABRICATED, paraphrased pseudo-guideline texts — they are NOT
copies of any real published guideline. They exist solely to give the
indexer enough plausible content (multiple sections, multiple pages) to
exercise sparse + dense retrieval end-to-end without a network or paid
content licence.

Run:
    python3 agent-api/corpus/_generate_synthetic_corpus.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, PageBreak


_HEADING_STYLE = ParagraphStyle(
    name="GuidelineHeading",
    fontName="Helvetica-Bold",
    fontSize=14,
    leading=18,
    spaceAfter=8,
)


_BODY_STYLE = ParagraphStyle(
    name="GuidelineBody",
    fontName="Helvetica",
    fontSize=11,
    leading=15,
    spaceAfter=8,
)


# ── SSC 2021 (synthetic) ────────────────────────────────────────────────────

_SSC_SECTIONS: list[tuple[str, list[str]]] = [
    (
        "SEPSIS HOUR-1 BUNDLE",
        [
            "On suspicion of sepsis or septic shock, the synthetic hour-one bundle "
            "advises rapid measurement of serum lactate, drawing of blood cultures "
            "before initiating antibiotics when feasible, and prompt empiric broad-"
            "spectrum coverage. Crystalloid resuscitation at thirty milliliters per "
            "kilogram is suggested for hypotension or lactate at or above four "
            "millimoles per liter. Vasopressors should be initiated when mean "
            "arterial pressure remains below sixty-five millimeters of mercury "
            "despite adequate fluids.",
            "Reassessment of perfusion is recommended within the first hour, with "
            "ongoing tracking of lactate clearance, capillary refill, and urine "
            "output. The bundle is designed to compress decision latency at the "
            "front of the resuscitation, not to replace clinical judgement.",
        ],
    ),
    (
        "ANTIMICROBIAL STEWARDSHIP",
        [
            "Synthetic guidance suggests that empiric coverage should be broad at "
            "presentation and narrowed within forty-eight to seventy-two hours as "
            "culture data return. Duration of therapy is typically seven to ten days "
            "for most uncomplicated bloodstream sources, with longer courses for "
            "endocarditis, osteomyelitis, or undrained foci.",
            "Procalcitonin trends may inform discontinuation of therapy in clinically "
            "improved patients but should not be the sole driver of stop decisions.",
        ],
    ),
    (
        "VASOPRESSOR SELECTION",
        [
            "Norepinephrine is the suggested first-line vasopressor for septic shock "
            "in this synthetic guidance. Vasopressin may be added at a fixed low dose "
            "to reduce norepinephrine requirements. Epinephrine is reserved for "
            "patients with inadequate response to first-line agents or with concurrent "
            "myocardial dysfunction.",
        ],
    ),
]


# ── KDIGO AKI (synthetic) ───────────────────────────────────────────────────

_KDIGO_SECTIONS: list[tuple[str, list[str]]] = [
    (
        "KDIGO STAGE 2 AKI CRITERIA",
        [
            "In this synthetic restatement, stage two acute kidney injury is defined "
            "as a serum creatinine increase of two to three times baseline, or a "
            "urine output below 0.5 milliliters per kilogram per hour for at least "
            "twelve hours. Stage one corresponds to a creatinine rise of 1.5 to 1.9 "
            "times baseline or an absolute increase of 0.3 milligrams per deciliter "
            "within forty-eight hours.",
        ],
    ),
    (
        "AKI WORKUP AND MONITORING",
        [
            "Initial evaluation should include review of recent medications for "
            "nephrotoxic exposure, volume status assessment, and urinalysis with "
            "microscopy. Renal ultrasound is suggested when post-renal obstruction "
            "is plausible. Serum creatinine should be trended every twelve to "
            "twenty-four hours during active injury.",
            "Avoidance of further nephrotoxins, including non-steroidal anti-"
            "inflammatory agents and intravenous iodinated contrast where feasible, "
            "is suggested through the recovery window.",
        ],
    ),
    (
        "RENAL REPLACEMENT TIMING",
        [
            "Synthetic guidance does not endorse uniformly early initiation of renal "
            "replacement therapy in stage two or three acute kidney injury without a "
            "compelling indication such as refractory hyperkalemia, acidosis, "
            "uremic complications, or volume overload unresponsive to diuretics.",
        ],
    ),
]


# ── ADA Inpatient Glycemic (synthetic) ──────────────────────────────────────

_ADA_SECTIONS: list[tuple[str, list[str]]] = [
    (
        "INPATIENT INSULIN INFUSION TARGETS",
        [
            "The synthetic guidance suggests a glucose target range of one hundred "
            "forty to one hundred eighty milligrams per deciliter for most non-"
            "critically-ill hospitalized adults receiving insulin. A tighter target "
            "of one hundred ten to one hundred forty milligrams per deciliter may be "
            "appropriate for selected critically-ill patients if it can be achieved "
            "without significant hypoglycemia.",
            "Continuous insulin infusion is suggested for severe hyperglycemia, "
            "diabetic ketoacidosis, and the perioperative period in cardiac surgery, "
            "with hourly bedside glucose monitoring during titration.",
        ],
    ),
    (
        "BASAL BOLUS REGIMEN",
        [
            "For non-critically-ill inpatients, the synthetic recommendation favors "
            "a basal-bolus regimen over sliding-scale-only insulin. A typical total "
            "daily dose starts at 0.3 to 0.5 units per kilogram, divided as fifty "
            "percent basal and fifty percent prandial when oral intake is reliable. "
            "Correction insulin is added on top for pre-meal hyperglycemia.",
        ],
    ),
    (
        "HYPOGLYCEMIA MANAGEMENT",
        [
            "Treatment of glucose below seventy milligrams per deciliter is "
            "suggested with fifteen grams of fast-acting carbohydrate orally if the "
            "patient can swallow safely, or twenty-five grams of dextrose "
            "intravenously otherwise, with re-check in fifteen minutes. Insulin "
            "regimen should be reviewed and adjusted after any episode below "
            "fifty-four milligrams per deciliter.",
        ],
    ),
]


def _render_pdf(out_path: Path, sections: list[tuple[str, list[str]]]) -> None:
    """Render a multi-section synthetic PDF to ``out_path``."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        title=out_path.stem,
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54,
    )
    story: list = []
    for idx, (heading, paragraphs) in enumerate(sections):
        story.append(Paragraph(heading, _HEADING_STYLE))
        story.append(Spacer(1, 6))
        for para in paragraphs:
            story.append(Paragraph(para, _BODY_STYLE))
            story.append(Spacer(1, 6))
        # Force a page break between major sections so chunkers see real
        # page boundaries (and so each PDF spans 2+ pages).
        if idx < len(sections) - 1:
            story.append(PageBreak())
    doc.build(story)


def main() -> None:
    here = Path(__file__).resolve().parent
    _render_pdf(here / "ssc_2021_excerpt.pdf", _SSC_SECTIONS)
    _render_pdf(here / "kdigo_aki_2012_excerpt.pdf", _KDIGO_SECTIONS)
    _render_pdf(here / "ada_glycemic_excerpt.pdf", _ADA_SECTIONS)
    print(f"Wrote synthetic corpus PDFs to {here}")


if __name__ == "__main__":
    main()
