"""Wave 2C — synthetic eval corpus *with* bbox ground-truth sidecars.

This generator complements ``_generate_eval_corpus.py`` (which produces
PDFs but no GT) by adding three modalities of fixtures, each with a
``<name>.gt.json`` sidecar carrying field-level bounding boxes:

  1. ``typed_pdf``     — ReportLab single-column prose PDFs. Pre-render
                         field positions are captured directly from the
                         drawing calls (origin = bottom-left, points).
  2. ``table_heavy``   — ReportLab tabular lab-report PDFs. Cell bboxes
                         are computed from the ``Table`` flowable's
                         column widths + row heights after layout.
  3. ``photo_capture`` — PIL-rendered HTML-ish text on a paper-tone
                         canvas, then projected through a deterministic
                         perspective warp to mimic a phone-photographed
                         document. The pre-warp axis-aligned bboxes AND
                         the inverse-warp matrix are both stored so the
                         scoring rubric can de-warp before comparison.

The first call to :func:`generate_all_v2` produces the entire corpus
deterministically — re-running yields identical bytes (PDFs and PNGs
alike). PDF determinism comes from ReportLab + ``SOURCE_DATE_EPOCH``;
PIL output is deterministic by construction (no JPEG/zlib randomness for
PNG with fixed input pixels).

Run:
    python3 agent-api/tests/fixtures/eval/_generate_synthetic_v2.py

Output schema (sidecar JSON, per fixture):

    {
      "fixture": "typed_pdf_001.pdf",
      "modality": "typed_pdf",
      "image_size": {"w": 612, "h": 792},     # points (PDF) or pixels (image)
      "fields": [
        {
          "name": "patient_name",
          "value": "Jane Doe",
          "page": 1,
          "bbox": {"x": 72, "y": 720, "w": 120, "h": 12},
          "rotation_deg": 0
        },
        ...
      ],
      "warp": null   # photo_capture only — see PhotoCaptureGT
    }

For ``photo_capture`` fixtures the ``warp`` key carries:

    {
      "matrix": [[a, b, c], [d, e, f], [g, h, i]],   # 3x3 forward
      "inverse": [[...]],                              # 3x3 inverse
      "pre_warp_size": {"w": ..., "h": ...}
    }

Coordinates in ``fields[*].bbox`` are in the *post*-warp image frame for
photo fixtures (the frame the model sees). Use the ``inverse`` matrix to
de-warp before any precise comparison; use the matrix directly to
project pre-warp axis-aligned rectangles into the warped frame.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# Pin a stable creation epoch so PDFs are byte-deterministic across runs.
os.environ.setdefault("SOURCE_DATE_EPOCH", "1700000000")

EVAL_DIR = Path(__file__).parent
OUT_DIR = EVAL_DIR / "synthetic_v2"

# Letter @ 72 dpi
PAGE_W_PT = 612
PAGE_H_PT = 792


# ---------------------------------------------------------------------------
# GT models (lightweight — kept as plain dicts to dump verbatim to JSON)
# ---------------------------------------------------------------------------


@dataclass
class GTField:
    name: str
    value: str
    page: int
    x: float
    y: float
    w: float
    h: float
    rotation_deg: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "page": self.page,
            "bbox": {
                "x": round(self.x, 3),
                "y": round(self.y, 3),
                "w": round(self.w, 3),
                "h": round(self.h, 3),
            },
            "rotation_deg": self.rotation_deg,
        }


def _write_sidecar(
    fixture_path: Path,
    *,
    modality: str,
    image_w: int,
    image_h: int,
    fields: list[GTField],
    warp: dict[str, Any] | None = None,
) -> Path:
    sidecar = fixture_path.with_suffix(fixture_path.suffix + ".gt.json")
    payload: dict[str, Any] = {
        "fixture": fixture_path.name,
        "modality": modality,
        "image_size": {"w": image_w, "h": image_h},
        "fields": [f.to_dict() for f in fields],
        "warp": warp,
    }
    sidecar.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return sidecar


# ---------------------------------------------------------------------------
# typed_pdf — ReportLab text on a single-column page
# ---------------------------------------------------------------------------


def _string_width(text: str, font: str, size: float) -> float:
    """Approximate string width in points using ReportLab's stringWidth."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    return float(stringWidth(text, font, size))


def _draw_typed_field(
    c: canvas.Canvas,
    *,
    x_in: float,
    y_in: float,
    label: str,
    value: str,
    field_name: str,
    fields_out: list[GTField],
    page: int = 1,
    font: str = "Helvetica",
    font_size: float = 10.0,
) -> None:
    """Draw a 'Label: value' line and record the value's bbox in points.

    PDF coordinate origin is bottom-left; we convert to image-pixel-style
    top-left for the GT (consistent with photo_capture sidecars). image_size
    in the sidecar is page width/height in points, so consumers should
    interpret bbox y from the *top*.
    """
    c.setFont(font, font_size)
    line = f"{label} {value}" if label else value
    c.drawString(x_in * inch, y_in * inch, line)

    label_width = _string_width(f"{label} ", font, font_size) if label else 0.0
    value_width = _string_width(value, font, font_size)
    # Rough line ascent (font_size * 0.75 is a fair-enough cap height proxy).
    ascent = font_size * 0.75
    descent = font_size * 0.18
    x_pt = x_in * inch + label_width
    # Convert PDF baseline-y to top-down y for the sidecar:
    y_baseline_pt = y_in * inch
    y_top_pt = PAGE_H_PT - (y_baseline_pt + ascent)
    h_pt = ascent + descent
    fields_out.append(
        GTField(
            name=field_name,
            value=value,
            page=page,
            x=x_pt,
            y=y_top_pt,
            w=value_width,
            h=h_pt,
        )
    )


def _typed_pdf_template(
    out_path: Path,
    *,
    title: str,
    patient_name: str,
    dob: str,
    mrn: str,
    chief: str,
    note_lines: tuple[str, ...],
) -> tuple[Path, list[GTField]]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_path), pagesize=LETTER)
    c.setTitle(title)
    c.setAuthor("synthetic-fixture-v2")
    c.setSubject("synthetic")
    c.setCreator("agent-api eval-corpus generator v2")

    fields: list[GTField] = []

    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, title)

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 9.6 * inch, "PATIENT")

    _draw_typed_field(
        c, x_in=1.0, y_in=9.30, label="Name:", value=patient_name,
        field_name="patient_name", fields_out=fields,
    )
    _draw_typed_field(
        c, x_in=1.0, y_in=9.10, label="DOB:", value=dob,
        field_name="dob", fields_out=fields,
    )
    _draw_typed_field(
        c, x_in=1.0, y_in=8.90, label="MRN:", value=mrn,
        field_name="mrn", fields_out=fields,
    )

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 8.45 * inch, "CHIEF CONCERN")
    _draw_typed_field(
        c, x_in=1.0, y_in=8.20, label="", value=chief,
        field_name="chief_concern", fields_out=fields,
    )

    c.setFont("Helvetica-Bold", 11)
    c.drawString(1 * inch, 7.7 * inch, "NOTE")
    y = 7.45
    for i, line in enumerate(note_lines):
        _draw_typed_field(
            c, x_in=1.0, y_in=y, label="", value=line,
            field_name=f"note_line_{i + 1}", fields_out=fields,
        )
        y -= 0.22

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 1 (synthetic v2)")
    c.showPage()
    c.save()
    return out_path, fields


# ---------------------------------------------------------------------------
# table_heavy — ReportLab Table flowable; per-cell bboxes
# ---------------------------------------------------------------------------


def _table_heavy_template(
    out_path: Path,
    *,
    title: str,
    patient_name: str,
    mrn: str,
    rows: tuple[tuple[str, str, str, str, str], ...],
) -> tuple[Path, list[GTField]]:
    """Single-page table fixture. Records bbox per data cell.

    Layout uses fixed column widths + a fixed row height for determinism
    and clean GT math. The header is drawn with the same column widths so
    its bboxes are also recorded.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_path), pagesize=LETTER)
    c.setTitle(title)
    c.setAuthor("synthetic-fixture-v2")
    c.setSubject("synthetic")
    c.setCreator("agent-api eval-corpus generator v2")

    fields: list[GTField] = []

    # Header
    c.setFont("Helvetica-Bold", 14)
    c.drawString(1 * inch, 10.3 * inch, title)

    c.setFont("Helvetica-Bold", 11)
    _draw_typed_field(
        c, x_in=1.0, y_in=9.6, label="Patient:", value=patient_name,
        field_name="patient_name", fields_out=fields, font="Helvetica-Bold",
    )
    _draw_typed_field(
        c, x_in=4.5, y_in=9.6, label="MRN:", value=mrn,
        field_name="mrn", fields_out=fields, font="Helvetica-Bold",
    )

    # Table region
    table_x = 1.0 * inch
    table_top_in = 9.0
    row_h_in = 0.25
    col_widths_in = (1.7, 0.8, 0.7, 1.1, 0.5)  # test, value, unit, ref, flag
    col_x_in: list[float] = []
    cur = 1.0
    for w in col_widths_in:
        col_x_in.append(cur)
        cur += w
    headers = ("Test", "Value", "Unit", "Reference", "Flag")
    field_for_header = ("test_name", "value", "unit", "reference", "flag")

    # Draw header row
    c.setFont("Helvetica-Bold", 10)
    y_in = table_top_in
    for col_idx, hdr in enumerate(headers):
        c.drawString(col_x_in[col_idx] * inch, y_in * inch, hdr)
    c.line(table_x, (y_in - 0.05) * inch, 7.5 * inch, (y_in - 0.05) * inch)

    # Draw data rows
    c.setFont("Helvetica", 10)
    for r_idx, row in enumerate(rows):
        y_in -= row_h_in
        row_label = row[0].lower().replace(" ", "_")
        for col_idx, cell_value in enumerate(row):
            c.drawString(col_x_in[col_idx] * inch, y_in * inch, cell_value)
            if not cell_value:
                # Skip empty cells (e.g. Flag column on a normal row) — a
                # zero-length value would fail the non-degenerate-bbox
                # invariant and isn't useful as GT.
                continue
            cell_w_pt = col_widths_in[col_idx] * inch
            ascent = 10.0 * 0.75
            descent = 10.0 * 0.18
            x_pt = col_x_in[col_idx] * inch
            y_top_pt = PAGE_H_PT - (y_in * inch + ascent)
            field_name = f"row_{r_idx + 1}_{field_for_header[col_idx]}_{row_label}"
            fields.append(
                GTField(
                    name=field_name,
                    value=cell_value,
                    page=1,
                    x=x_pt,
                    y=y_top_pt,
                    w=cell_w_pt,
                    h=ascent + descent,
                )
            )

    c.setFont("Helvetica-Oblique", 9)
    c.drawString(1 * inch, 0.7 * inch, "Page 1 of 1 (synthetic v2 table)")
    c.showPage()
    c.save()
    return out_path, fields


# ---------------------------------------------------------------------------
# photo_capture — PIL render + perspective warp (deterministic)
# ---------------------------------------------------------------------------


def _try_load_font(size: int) -> ImageFont.ImageFont:
    """Try a small set of fonts likely available on macOS / Linux CI."""
    candidates = (
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    )
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _solve_perspective(src: list[tuple[float, float]],
                       dst: list[tuple[float, float]]) -> list[list[float]]:
    """Solve the 8-DOF perspective transform mapping src -> dst.

    Returns a 3x3 forward matrix M such that for src point p,
    dst = M * [p, 1]^T (then divide by w). Pure Python — uses numpy
    for the linear solve so we don't depend on cv2.
    """
    import numpy as np

    A: list[list[float]] = []
    b: list[float] = []
    for (sx, sy), (dx, dy) in zip(src, dst):
        A.append([sx, sy, 1, 0, 0, 0, -dx * sx, -dx * sy])
        b.append(dx)
        A.append([0, 0, 0, sx, sy, 1, -dy * sx, -dy * sy])
        b.append(dy)
    A_np = np.array(A, dtype=np.float64)
    b_np = np.array(b, dtype=np.float64)
    h = np.linalg.solve(A_np, b_np)
    M = [
        [h[0], h[1], h[2]],
        [h[3], h[4], h[5]],
        [h[6], h[7], 1.0],
    ]
    return M


def _invert_3x3(M: list[list[float]]) -> list[list[float]]:
    import numpy as np

    return np.linalg.inv(np.array(M, dtype=np.float64)).tolist()


def _project_point(M: list[list[float]], x: float, y: float) -> tuple[float, float]:
    a, b, c = M[0]
    d, e, f = M[1]
    g, h, i = M[2]
    w = g * x + h * y + i
    return (a * x + b * y + c) / w, (d * x + e * y + f) / w


def _project_aabb(
    M: list[list[float]], x: float, y: float, w: float, h: float
) -> tuple[float, float, float, float]:
    """Project an axis-aligned rect under M and return its bounding aabb."""
    corners = [
        _project_point(M, x, y),
        _project_point(M, x + w, y),
        _project_point(M, x + w, y + h),
        _project_point(M, x, y + h),
    ]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _photo_capture_template(
    out_path: Path,
    *,
    seed: int,
    patient_name: str,
    dob: str,
    mrn: str,
    chief: str,
    notes: tuple[str, ...],
) -> tuple[Path, list[GTField], dict[str, Any]]:
    """Render an intake-form-shaped page as PNG and apply a perspective warp.

    Determinism: corner offsets are derived from ``seed`` via a fixed
    LCG (no use of ``random.random``). Same seed -> identical bytes.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-warp ("flat scan") frame
    pre_w, pre_h = 1240, 1754  # ~A4 @ 150dpi-ish
    bg = Image.new("RGB", (pre_w, pre_h), color=(252, 250, 244))  # paper tone
    draw = ImageDraw.Draw(bg)
    title_font = _try_load_font(36)
    label_font = _try_load_font(24)
    body_font = _try_load_font(20)

    pre_fields: list[GTField] = []

    def _put(text: str, x: int, y: int, font: ImageFont.ImageFont, name: str) -> None:
        draw.text((x, y), text, fill=(20, 20, 30), font=font)
        # bbox via text-bbox
        tb = draw.textbbox((x, y), text, font=font)
        pre_fields.append(
            GTField(
                name=name,
                value=text,
                page=1,
                x=float(tb[0]),
                y=float(tb[1]),
                w=float(tb[2] - tb[0]),
                h=float(tb[3] - tb[1]),
            )
        )

    margin = 100
    draw.text((margin, 80), "ADMISSION INTAKE FORM", fill=(0, 0, 80), font=title_font)
    _put(f"Patient: {patient_name}", margin, 180, label_font, "patient_name")
    _put(f"DOB: {dob}", margin, 220, label_font, "dob")
    _put(f"MRN: {mrn}", margin, 260, label_font, "mrn")
    _put("CHIEF CONCERN", margin, 330, label_font, "chief_label")
    _put(chief, margin, 365, body_font, "chief_concern")
    _put("NOTES", margin, 440, label_font, "notes_label")
    y = 475
    for i, line in enumerate(notes):
        _put(line, margin, y, body_font, f"note_{i + 1}")
        y += 35

    # Determine warp corner offsets via a tiny deterministic LCG.
    state = seed * 1103515245 + 12345
    def _next_int(lo: int, hi: int) -> int:
        nonlocal state
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        span = hi - lo + 1
        return lo + (state % span)

    # 4 source corners (axis-aligned full pre-warp rect)
    src = [(0.0, 0.0), (float(pre_w), 0.0), (float(pre_w), float(pre_h)), (0.0, float(pre_h))]
    # Destination corners: small perturbation per corner (5–35 px each axis)
    # so the document looks photographed at a slight angle.
    dst = [
        (float(_next_int(15, 60)),         float(_next_int(20, 80))),
        (pre_w - float(_next_int(15, 60)), float(_next_int(10, 70))),
        (pre_w - float(_next_int(20, 70)), pre_h - float(_next_int(20, 90))),
        (float(_next_int(20, 70)),         pre_h - float(_next_int(15, 80))),
    ]
    M = _solve_perspective(src, dst)
    M_inv = _invert_3x3(M)

    # PIL transform expects the *inverse* mapping (output -> input).
    # The 8-tuple is (a, b, c, d, e, f, g, h) for x'=(ax+by+c)/(gx+hy+1).
    inv = M_inv
    coeffs = (
        inv[0][0] / inv[2][2], inv[0][1] / inv[2][2], inv[0][2] / inv[2][2],
        inv[1][0] / inv[2][2], inv[1][1] / inv[2][2], inv[1][2] / inv[2][2],
        inv[2][0] / inv[2][2], inv[2][1] / inv[2][2],
    )
    warped = bg.transform(
        (pre_w, pre_h),
        Image.PERSPECTIVE,
        coeffs,
        resample=Image.BILINEAR,
        fillcolor=(20, 20, 25),  # dark "background" around the warped page
    )

    # Project pre-warp field bboxes through M to get post-warp aabbs.
    post_fields: list[GTField] = []
    for f in pre_fields:
        nx, ny, nw, nh = _project_aabb(M, f.x, f.y, f.w, f.h)
        post_fields.append(
            GTField(
                name=f.name,
                value=f.value,
                page=1,
                x=nx,
                y=ny,
                w=nw,
                h=nh,
                rotation_deg=0.0,
            )
        )

    warped.save(out_path, format="PNG", optimize=True)

    warp_meta: dict[str, Any] = {
        "matrix": M,
        "inverse": M_inv,
        "pre_warp_size": {"w": pre_w, "h": pre_h},
        "pre_warp_fields": [f.to_dict() for f in pre_fields],
    }
    return out_path, post_fields, warp_meta


# ---------------------------------------------------------------------------
# Corpus catalog — 12 derivatives × 3 modalities
# ---------------------------------------------------------------------------


# Synthetic identities (kept aligned with the no-PHI whitelist; see the
# corresponding test file. These names are entirely fabricated — same
# convention as _generate_eval_corpus.py).
_PEOPLE = [
    ("Iris Tanaka",      "1965-04-12", "200101"),
    ("Mateo Cruz",       "1972-09-30", "200202"),
    ("Aaliyah Brooks",   "1988-01-05", "200303"),
    ("Soren Lindgren",   "1955-07-19", "200404"),
    ("Priya Subramanian","1990-12-22", "200505"),
    ("Nikolai Petrov",   "1948-02-08", "200606"),
    ("Zara Hassan",      "2005-06-14", "200707"),
    ("Diego Vargas",     "1981-03-27", "200808"),
    ("Mei-Lin Park",     "1969-11-11", "200909"),
    ("Jonas Eriksen",    "1976-08-02", "201010"),
    ("Sofia Romero",     "1958-10-25", "201111"),
    ("Henrik Falk",      "1993-05-17", "201212"),
]

_TYPED_NOTES = (
    "Patient reports onset of symptoms three days prior to admission.",
    "Vital signs stable at presentation; awaiting labs.",
    "Plan: serial reassessment, supportive care.",
)

_TABLE_ROW_BANK = (
    ("Sodium",    "139", "mEq/L", "136 - 145", ""),
    ("Potassium", "4.2", "mEq/L", "3.5 - 5.0", ""),
    ("Glucose",   "104", "mg/dL", "70 - 110",  ""),
    ("Hgb",       "13.6","g/dL",  "12.0 - 15.5",""),
    ("WBC",       "7.4", "K/uL",  "4.0 - 11.0",""),
    ("Cr",        "1.0", "mg/dL", "0.6 - 1.3",  ""),
)


def _build_typed_pdfs() -> list[tuple[str, Callable[[Path], tuple[Path, list[GTField]]]]]:
    """12 typed_pdf derivatives — varied chief concerns + note text."""
    out = []
    for i, (name, dob, mrn) in enumerate(_PEOPLE):
        idx = i + 1
        chiefs = (
            "Sore throat with low-grade fever.",
            "Acute lower back pain following lifting.",
            "Worsening cough productive of yellow sputum.",
            "Right knee swelling after fall.",
            "Headache with photophobia.",
            "Palpitations and lightheadedness.",
            "Right flank pain, possible renal colic.",
            "Generalized fatigue x two weeks.",
            "Left-sided chest discomfort, exertional.",
            "Nausea and emesis x 24 hours.",
            "Worsening shortness of breath.",
            "Bilateral leg edema, new onset.",
        )
        chief = chiefs[i % len(chiefs)]
        case_id = f"typed_pdf_{idx:03d}"

        def _gen(out_path: Path, *, name=name, dob=dob, mrn=mrn, chief=chief, case_id=case_id) -> tuple[Path, list[GTField]]:
            return _typed_pdf_template(
                out_path,
                title=f"Synthetic Note {case_id}",
                patient_name=name,
                dob=dob,
                mrn=mrn,
                chief=chief,
                note_lines=_TYPED_NOTES,
            )

        out.append((case_id, _gen))
    return out


def _build_table_heavy() -> list[tuple[str, Callable[[Path], tuple[Path, list[GTField]]]]]:
    out = []
    for i, (name, dob, mrn) in enumerate(_PEOPLE):
        idx = i + 1
        # Rotate the row bank to vary content but keep determinism.
        rows = tuple(_TABLE_ROW_BANK[(i + j) % len(_TABLE_ROW_BANK)] for j in range(4))
        case_id = f"table_heavy_{idx:03d}"

        def _gen(out_path: Path, *, name=name, dob=dob, mrn=mrn, rows=rows, case_id=case_id) -> tuple[Path, list[GTField]]:
            return _table_heavy_template(
                out_path,
                title=f"Synthetic Lab Panel {case_id}",
                patient_name=name,
                mrn=mrn,
                rows=rows,
            )

        out.append((case_id, _gen))
    return out


def _build_photo_capture() -> list[tuple[str, Callable[[Path], tuple[Path, list[GTField], dict[str, Any]]]]]:
    out = []
    chiefs = (
        "Sore throat with low-grade fever.",
        "Acute lower back pain following lifting.",
        "Worsening cough productive of yellow sputum.",
        "Right knee swelling after fall.",
        "Headache with photophobia.",
        "Palpitations and lightheadedness.",
        "Right flank pain, possible renal colic.",
        "Generalized fatigue x two weeks.",
        "Left-sided chest discomfort, exertional.",
        "Nausea and emesis x 24 hours.",
        "Worsening shortness of breath.",
        "Bilateral leg edema, new onset.",
    )
    notes = (
        "Allergies: NKDA",
        "Code Status: Full Code",
        "Plan: admit for observation",
    )
    for i, (name, dob, mrn) in enumerate(_PEOPLE):
        idx = i + 1
        case_id = f"photo_capture_{idx:03d}"
        seed = 1000 + idx

        def _gen(
            out_path: Path,
            *,
            name=name, dob=dob, mrn=mrn,
            chief=chiefs[i % len(chiefs)],
            seed=seed,
        ) -> tuple[Path, list[GTField], dict[str, Any]]:
            return _photo_capture_template(
                out_path,
                seed=seed,
                patient_name=name,
                dob=dob,
                mrn=mrn,
                chief=chief,
                notes=notes,
            )

        out.append((case_id, _gen))
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_all_v2() -> dict[str, Path]:
    """Generate every Wave 2C synthetic fixture + sidecar.

    Returns ``{fixture_key: path}`` for downstream registration with the
    eval-suite fixture index. The fixture key matches the case_id stem
    (e.g. ``typed_pdf_001``); the on-disk filename has the appropriate
    extension (``.pdf`` for typed/table, ``.png`` for photo_capture).
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}

    for case_id, fn in _build_typed_pdfs():
        path = OUT_DIR / f"{case_id}.pdf"
        _, fields = fn(path)
        _write_sidecar(
            path, modality="typed_pdf",
            image_w=PAGE_W_PT, image_h=PAGE_H_PT,
            fields=fields, warp=None,
        )
        out[case_id] = path

    for case_id, fn in _build_table_heavy():
        path = OUT_DIR / f"{case_id}.pdf"
        _, fields = fn(path)
        _write_sidecar(
            path, modality="table_heavy",
            image_w=PAGE_W_PT, image_h=PAGE_H_PT,
            fields=fields, warp=None,
        )
        out[case_id] = path

    for case_id, fn in _build_photo_capture():
        path = OUT_DIR / f"{case_id}.png"
        _, fields, warp = fn(path)
        # All photo fixtures use the same pre-warp size (constant in the
        # template); the sidecar consumer reads this from warp.pre_warp_size.
        pre = warp["pre_warp_size"]
        _write_sidecar(
            path, modality="photo_capture",
            image_w=pre["w"], image_h=pre["h"],
            fields=fields, warp=warp,
        )
        out[case_id] = path

    return out


def all_fixture_keys() -> list[str]:
    """List the fixture keys this generator produces (no I/O)."""
    keys: list[str] = []
    for i in range(1, len(_PEOPLE) + 1):
        keys.append(f"typed_pdf_{i:03d}")
        keys.append(f"table_heavy_{i:03d}")
        keys.append(f"photo_capture_{i:03d}")
    return keys


if __name__ == "__main__":
    paths = generate_all_v2()
    for k, p in sorted(paths.items()):
        size = p.stat().st_size if p.exists() else 0
        sidecar = p.with_suffix(p.suffix + ".gt.json")
        s_size = sidecar.stat().st_size if sidecar.exists() else 0
        print(f"{k:24s} -> {p.name} ({size} bytes) + sidecar ({s_size} bytes)")
    print(f"\nTotal: {len(paths)} fixtures")
