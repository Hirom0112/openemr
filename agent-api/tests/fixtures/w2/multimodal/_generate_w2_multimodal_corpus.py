"""Phase 9 Slice 9.9 — W2 multimodal corpus generator.

Parallel to ``tests/fixtures/eval/_generate_eval_corpus.py``. Derives 20
fixtures from 15 anchor files in ``tests/fixtures/w2/multimodal/{hl7v2,
docx,xlsx,tiff}/``. Anchors are checked into git; derivatives are
generated on-demand (and re-generated idempotently in CI).

Anchors (15):
  - 14 HL7 v2 messages (p01..p07 × {ADT-A08, ORU-R01})
  - 7 DOCX referrals (p01..p07)
  - 3 XLSX workbooks (p01-chen, p02-whitaker, p03-reyes) — generated here
    if missing (no checked-in XLSX anchors yet; ``_seed_xlsx_anchors`` builds
    them from constants below)
  - 2 multi-page TIFF fax packets (p01, p04)

Derivatives (20):
  HL7 (4)   — wrong-patient (PID-3 mutation), blank-OBX, multi-patient packet,
              dup-OBX intra-doc conflict
  XLSX (5)  — sparse Labs_Trend, wrong-mrn Patient sheet, dup-lab,
              no-allergies (sheet absent), blank workbook (headers only)
  DOCX (2)  — wrong-name, corrupt document.xml
  TIFF (5)  — single-page lab, single-page intake, mode-1 1-bit, corrupt-IFD,
              wrong-cover, 4-page iterator (5 derivatives + 1 expansion)

This module is import-safe: ``generate_all()`` returns a dict mapping
fixture_key → absolute Path. Calling it is idempotent — re-running will
re-emit derivative files but anchors are never overwritten.

Run standalone for CI bootstrap:
    python3 -m tests.fixtures.w2.multimodal._generate_w2_multimodal_corpus
"""

from __future__ import annotations

import io
import os
import shutil
import zipfile
from pathlib import Path
from typing import Dict


_HERE = Path(__file__).resolve().parent
_HL7_DIR = _HERE / "hl7v2"
_DOCX_DIR = _HERE / "docx"
_XLSX_DIR = _HERE / "xlsx"
_TIFF_DIR = _HERE / "tiff"


# ---------------------------------------------------------------------------
# XLSX anchor seed data (3 workbooks).
# Schema: each workbook has Patient, Labs_Trend, Allergies sheets.
# Names/MRNs/DOBs are SYNTHETIC and align with the patient identities in
# tests/fixtures/w2_eval_cases.py.
# ---------------------------------------------------------------------------


_XLSX_SEEDS: Dict[str, dict] = {
    "p01-chen-workbook": {
        "patient": [
            ["MRN", "Family", "Given", "DOB", "Gender"],
            ["100481", "Chen", "Margaret", "1967-08-14", "female"],
        ],
        "labs": [
            ["Date", "Test", "Value", "Unit", "Loinc"],
            ["2026-04-01", "LDL", "142", "mg/dL", "13457-7"],
            ["2026-04-01", "HDL", "48", "mg/dL", "2085-9"],
            ["2026-04-01", "Total Cholesterol", "210", "mg/dL", "2093-3"],
        ],
        "allergies": [
            ["Substance", "Reaction", "Severity"],
            ["penicillin", "rash", "moderate"],
        ],
    },
    "p02-whitaker-workbook": {
        "patient": [
            ["MRN", "Family", "Given", "DOB", "Gender"],
            ["100492", "Whitaker", "James", "1958-11-03", "male"],
        ],
        "labs": [
            ["Date", "Test", "Value", "Unit", "Loinc"],
            ["2026-04-02", "WBC", "8.4", "10^3/uL", "6690-2"],
            ["2026-04-02", "Hgb", "13.1", "g/dL", "718-7"],
            ["2026-04-02", "Plt", "234", "10^3/uL", "777-3"],
        ],
        "allergies": [
            ["Substance", "Reaction", "Severity"],
            ["NKDA", "", ""],
        ],
    },
    "p03-reyes-workbook": {
        "patient": [
            ["MRN", "Family", "Given", "DOB", "Gender"],
            ["100503", "Reyes", "Luis", "1972-05-18", "male"],
        ],
        "labs": [
            ["Date", "Test", "Value", "Unit", "Loinc"],
            ["2026-04-03", "HbA1c", "7.8", "%", "4548-4"],
            ["2026-04-03", "Glucose", "168", "mg/dL", "2345-7"],
        ],
        "allergies": [
            ["Substance", "Reaction", "Severity"],
            ["sulfa", "hives", "mild"],
        ],
    },
}


def _seed_xlsx_anchors() -> None:
    """Create the 3 XLSX anchor workbooks if missing. Idempotent."""
    _XLSX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from openpyxl import Workbook  # type: ignore
    except ImportError:  # pragma: no cover — environment guard
        return
    for stem, sheets in _XLSX_SEEDS.items():
        target = _XLSX_DIR / f"{stem}.xlsx"
        if target.exists():
            continue
        wb = Workbook()
        wb.remove(wb.active)  # type: ignore[arg-type]
        for sheet_name, rows in (
            ("Patient", sheets["patient"]),
            ("Labs_Trend", sheets["labs"]),
            ("Allergies", sheets["allergies"]),
        ):
            ws = wb.create_sheet(sheet_name)
            for row in rows:
                ws.append(row)
        wb.save(target)


# ---------------------------------------------------------------------------
# Derivative generators
# ---------------------------------------------------------------------------


def _hl7_derivatives() -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    if not _HL7_DIR.exists():
        return out
    # 1. Wrong-patient PID-3 mutation on chen ORU.
    src = _HL7_DIR / "p01-chen-oru-r01.hl7"
    if src.exists():
        text = src.read_text(encoding="utf-8", errors="replace")
        # PID-3 carries MRN; mutate by appending an X — non-existent MRN.
        mutated = text.replace("|100481^", "|999999^").replace("|100481|", "|999999|")
        if mutated == text:
            # Best-effort: just append a sentinel to the first PID line.
            mutated = text.replace("PID|", "PID|X-WRONG-PID|", 1)
        target = _HL7_DIR / "p01-chen-oru-r01.wrong-patient.hl7"
        target.write_text(mutated, encoding="utf-8")
        out["p01-chen-oru-r01.wrong-patient"] = target
    # 2. Blank OBX-5 on patel ORU.
    src = _HL7_DIR / "p05-patel-oru-r01.hl7"
    if src.exists():
        text = src.read_text(encoding="utf-8", errors="replace")
        # Replace OBX-5 (5th field after OBX|) with empty. Naive segment
        # rewrite — sufficient for fixture purposes.
        lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("OBX|"):
                parts = line.split("|")
                if len(parts) > 5:
                    parts[5] = ""
                lines.append("|".join(parts))
            else:
                lines.append(line)
        target = _HL7_DIR / "p05-patel-oru-r01.blank-obx.hl7"
        target.write_text("\n".join(lines), encoding="utf-8")
        out["p05-patel-oru-r01.blank-obx"] = target
    # 3. Multi-patient packet — concatenate two ORUs (johnson + nguyen).
    a = _HL7_DIR / "p06-johnson-oru-r01.hl7"
    b = _HL7_DIR / "p07-nguyen-oru-r01.hl7"
    if a.exists() and b.exists():
        target = _HL7_DIR / "p06-johnson-oru-r01.multi-patient.hl7"
        target.write_text(
            a.read_text(encoding="utf-8") + "\n" + b.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        out["p06-johnson-oru-r01.multi-patient"] = target
    # 4. Dup-OBX intra-doc conflict on nguyen.
    src = _HL7_DIR / "p07-nguyen-oru-r01.hl7"
    if src.exists():
        text = src.read_text(encoding="utf-8", errors="replace")
        # Find first OBX line and duplicate with a different value.
        lines = text.splitlines()
        new_lines: list[str] = []
        injected = False
        for line in lines:
            new_lines.append(line)
            if not injected and line.startswith("OBX|"):
                parts = line.split("|")
                if len(parts) > 5:
                    # Append a 'Z' to the value to make it conflict.
                    parts[5] = (parts[5] or "0") + "Z"
                new_lines.append("|".join(parts))
                injected = True
        target = _HL7_DIR / "p07-nguyen-oru-r01.dup-obx.hl7"
        target.write_text("\n".join(new_lines), encoding="utf-8")
        out["p07-nguyen-oru-r01.dup-obx"] = target
    return out


def _xlsx_derivatives() -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    try:
        from openpyxl import Workbook, load_workbook  # type: ignore
    except ImportError:  # pragma: no cover
        return out
    chen = _XLSX_DIR / "p01-chen-workbook.xlsx"
    whitaker = _XLSX_DIR / "p02-whitaker-workbook.xlsx"
    reyes = _XLSX_DIR / "p03-reyes-workbook.xlsx"

    if chen.exists():
        # 1. Sparse Labs_Trend — keep header + first row blanked except date.
        wb = load_workbook(chen)
        if "Labs_Trend" in wb.sheetnames:
            ws = wb["Labs_Trend"]
            for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
                for cell in row[1:]:  # keep date column
                    cell.value = None
        target = _XLSX_DIR / "p01-chen-workbook.sparse-labs.xlsx"
        wb.save(target)
        out["p01-chen-workbook.sparse-labs"] = target

        # 2. No-allergies (sheet removed).
        wb = load_workbook(chen)
        if "Allergies" in wb.sheetnames:
            del wb["Allergies"]
        target = _XLSX_DIR / "p01-chen-workbook.no-allergies.xlsx"
        wb.save(target)
        out["p01-chen-workbook.no-allergies"] = target

        # 3. Blank workbook — only headers, no data rows.
        wb = load_workbook(chen)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            # Delete rows 2..end (keep header).
            if ws.max_row > 1:
                ws.delete_rows(2, ws.max_row - 1)
        target = _XLSX_DIR / "p01-chen-workbook.blank.xlsx"
        wb.save(target)
        out["p01-chen-workbook.blank"] = target

    if whitaker.exists():
        # 4. Wrong MRN on Patient sheet.
        wb = load_workbook(whitaker)
        if "Patient" in wb.sheetnames:
            ws = wb["Patient"]
            # Header row 1, data row 2, MRN column = 1.
            cell = ws.cell(row=2, column=1)
            cell.value = "999999"
        target = _XLSX_DIR / "p02-whitaker-workbook.wrong-mrn.xlsx"
        wb.save(target)
        out["p02-whitaker-workbook.wrong-mrn"] = target

    if reyes.exists():
        # 5. Dup-lab conflict — same loinc, two rows with different values.
        wb = load_workbook(reyes)
        if "Labs_Trend" in wb.sheetnames:
            ws = wb["Labs_Trend"]
            ws.append(["2026-04-03", "HbA1c", "9.2", "%", "4548-4"])
        target = _XLSX_DIR / "p03-reyes-workbook.dup-lab.xlsx"
        wb.save(target)
        out["p03-reyes-workbook.dup-lab"] = target

    return out


def _docx_derivatives() -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    if not _DOCX_DIR.exists():
        return out
    # 1. Wrong-name on nguyen referral — rewrite document.xml replacing the
    #    nguyen surname with a non-matching name. DOCX is a zip; we open and
    #    rewrite document.xml in-place.
    src = _DOCX_DIR / "p07-nguyen-referral.docx"
    if src.exists():
        target = _DOCX_DIR / "p07-nguyen-referral.wrong-name.docx"
        with zipfile.ZipFile(src, "r") as z_in:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z_out:
                for item in z_in.namelist():
                    data = z_in.read(item)
                    if item.endswith("document.xml"):
                        # Replace any occurrence of the patient family name
                        # with a non-matching token. Best-effort byte rewrite.
                        data = (
                            data.replace(b"Nguyen", b"Smithwick")
                            .replace(b"NGUYEN", b"SMITHWICK")
                            .replace(b"nguyen", b"smithwick")
                        )
                    z_out.writestr(item, data)
            target.write_bytes(buf.getvalue())
        out["p07-nguyen-referral.wrong-name"] = target
    # 2. Corrupt document.xml on chen referral.
    src = _DOCX_DIR / "p01-chen-referral.docx"
    if src.exists():
        target = _DOCX_DIR / "p01-chen-referral.corrupt-xml.docx"
        with zipfile.ZipFile(src, "r") as z_in:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z_out:
                for item in z_in.namelist():
                    data = z_in.read(item)
                    if item.endswith("document.xml"):
                        # Truncate halfway through to break the XML.
                        data = data[: max(1, len(data) // 2)]
                    z_out.writestr(item, data)
            target.write_bytes(buf.getvalue())
        out["p01-chen-referral.corrupt-xml"] = target
    return out


def _tiff_derivatives() -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    if not _TIFF_DIR.exists():
        return out
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError:  # pragma: no cover
        return out

    chen = _TIFF_DIR / "p01-chen-fax-packet.tiff"
    kowalski = _TIFF_DIR / "p04-kowalski-fax-packet.tiff"

    def _save_pages(pages: list, target: Path) -> None:
        if not pages:
            return
        first, rest = pages[0], pages[1:]
        first.save(target, save_all=True, append_images=rest, format="TIFF")

    def _load_pages(path: Path) -> list:
        out_pages: list = []
        with Image.open(path) as im:
            try:
                idx = 0
                while True:
                    im.seek(idx)
                    out_pages.append(im.copy())
                    idx += 1
            except EOFError:
                pass
        return out_pages

    if chen.exists():
        pages = _load_pages(chen)
        if pages:
            # 1. Single-page lab-only — first page, copied alone.
            target = _TIFF_DIR / "p01-chen-fax-packet.lab-only.tiff"
            pages[0].save(target, format="TIFF")
            out["p01-chen-fax-packet.lab-only"] = target

            # 2. Mode-1 1-bit derivative — convert all pages.
            mode1_pages = [p.convert("1") for p in pages]
            target = _TIFF_DIR / "p01-chen-fax-packet.mode1.tiff"
            _save_pages(mode1_pages, target)
            out["p01-chen-fax-packet.mode1"] = target

            # 3. Wrong-cover — generate a fresh first page with a non-matching
            #    name banner, then append original pages 1..N.
            cover = Image.new("L", pages[0].size, color=255)
            draw = ImageDraw.Draw(cover)
            draw.text((20, 20), "FAX COVER: Patient Smithwick MRN 999999", fill=0)
            target = _TIFF_DIR / "p01-chen-fax-packet.wrong-cover.tiff"
            _save_pages([cover] + pages[1:], target)
            out["p01-chen-fax-packet.wrong-cover"] = target

    if kowalski.exists():
        pages = _load_pages(kowalski)
        if pages:
            # 4. Single-page intake-only — last page (intakes are typically
            #    cover-stage in fax packets; pick page index min(len-1, 1)).
            idx = min(len(pages) - 1, 1)
            target = _TIFF_DIR / "p04-kowalski-fax-packet.intake-only.tiff"
            pages[idx].save(target, format="TIFF")
            out["p04-kowalski-fax-packet.intake-only"] = target

            # 5. Corrupt-IFD — write a tiff with a deliberately truncated IFD.
            #    We simulate by writing the file then truncating to ~30% of bytes.
            tmp = _TIFF_DIR / "_tmp_corrupt_source.tiff"
            _save_pages(pages, tmp)
            data = tmp.read_bytes()
            target = _TIFF_DIR / "p04-kowalski-fax-packet.corrupt-ifd.tiff"
            target.write_bytes(data[: max(64, len(data) // 3)])
            tmp.unlink(missing_ok=True)
            out["p04-kowalski-fax-packet.corrupt-ifd"] = target

            # 6. 4-page derivative — duplicate pages cyclically up to 4.
            four = (pages * 4)[:4]
            target = _TIFF_DIR / "p04-kowalski-fax-packet.4page.tiff"
            _save_pages(four, target)
            out["p04-kowalski-fax-packet.4page"] = target

    return out


def generate_all() -> Dict[str, Path]:
    """Idempotently emit anchors (XLSX) + 20 derivatives. Returns the full
    fixture_key → Path map for every anchor and derivative on disk.
    """
    _seed_xlsx_anchors()

    fixtures: Dict[str, Path] = {}
    for d, suffix in (
        (_HL7_DIR, ".hl7"),
        (_DOCX_DIR, ".docx"),
        (_XLSX_DIR, ".xlsx"),
        (_TIFF_DIR, ".tiff"),
    ):
        if not d.exists():
            continue
        for p in sorted(d.glob(f"*{suffix}")):
            fixtures[p.stem] = p

    fixtures.update(_hl7_derivatives())
    fixtures.update(_xlsx_derivatives())
    fixtures.update(_docx_derivatives())
    fixtures.update(_tiff_derivatives())
    return fixtures


if __name__ == "__main__":
    out = generate_all()
    print(f"Generated {len(out)} fixtures across hl7v2/docx/xlsx/tiff")
    for k in sorted(out):
        print(f"  {k} -> {out[k].relative_to(_HERE)}")
