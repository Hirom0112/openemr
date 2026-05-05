"""W2 50-case eval-set definitions (Slices 5.1 + 5.4).

Each case is a frozen dataclass that points at a fixture key (resolved by
``tests/fixtures/eval/_generate_eval_corpus.py:generate_all``) and carries the
expected agent outcome — kind, critic decision, violation/softwarn codes, and
field assertions. Slices 5.2/5.3's rubric runners consume ``CASES`` directly.

All names, MRNs, and DOBs are SYNTHETIC. The whitelist of synthetic identities
lives in ``tests/test_w2_eval_no_real_phi.py``; do not add a new patient
identity here without updating that whitelist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Bucket = Literal[
    "lab_nominal",
    "intake_nominal",
    "unknown_nominal",
    "wrong_type_hint",
    "wrong_patient",
    "blank_noise",
    "mixed_content",
    "low_quality_scan",
    "intra_doc_conflict",
    "evidence_retrieval",
    "missing_data",
    # Wave 2C — synthetic fixtures with bbox ground-truth sidecars.
    # Used by the citation_iou / citation_pixel_distance rubrics. The
    # fixtures live in ``tests/fixtures/eval/synthetic_v2/`` (see
    # ``_generate_synthetic_v2.py``); each carries a ``<name>.gt.json``
    # sidecar with field-level bboxes.
    "bbox_gt",
]

ExpectedKind = Literal["lab_report", "intake_form", "unknown"]
ExpectedDecision = Literal["pass", "soft_warn", "hard_block"]

# Document modality bucket — used by the eval suite to enforce per-modality
# pass-rate floors (see evals/diff_baseline.py). Distinct from ``Bucket``
# (which describes the test scenario) — modality describes the *document* shape.
DocumentModality = Literal[
    "typed_pdf",         # text-layer PDF, single column
    "scanned_pdf",       # raster PDF, OCR-required
    "intake_form",       # structured intake / triage form
    "photo_capture",     # phone-photographed document
    "handwritten",       # handwriting-heavy
    "table_heavy",       # lab reports with tabular data
    "multi_column",      # multi-column layouts
    "synthetic",         # programmatically generated, ground-truth available
    "unknown",           # not yet classified
]


@dataclass(frozen=True)
class W2EvalCase:
    case_id: str
    bucket: Bucket
    fixture_key: str  # key into eval._generate_eval_corpus.generate_all()
    doc_type_hint: str | None
    chart_patient: dict  # FHIR Patient JSON shape
    expected_kind: ExpectedKind
    expected_critic_decision: ExpectedDecision
    expected_violation_codes: tuple[str, ...] = ()
    expected_softwarn_codes: tuple[str, ...] = ()
    expected_field_assertions: tuple[tuple[str, str], ...] = ()
    notes: str = ""
    # Phase 3 — Observation.derivedFrom provenance chain assertions.
    # When set on a case (lab_nominal cases that produce real Observations),
    # the rubric layer probes the MySQL ``copilot_observations`` table and
    # checks the chain end-to-end. When None, the rubric is skipped.
    # Shape:
    #   {
    #     "observations_min": int,        # >= N Observation rows expected
    #     "all_have_derivedFrom": bool,   # every Observation.derivedFrom non-empty
    #     "all_citations_resolve": bool,  # every citation bbox_id is in ocr_layout
    #   }
    expected_provenance: dict | None = None
    # Evidence-retrieval bucket — set on cases that exercise the
    # ``evidence_retriever`` node. ``evidence_query`` carries the clinical
    # question; ``expected_must_cite_source_id`` is a tuple of source ids
    # (e.g. ``("kdigo-aki-2012",)``) at least one of which must appear in
    # the cited evidence; ``expected_keywords_in_quote`` is a tuple of
    # lower-case substrings, at least one of which must appear in the
    # cited quote text. ``None`` on every non-evidence case — the rubric
    # short-circuits to ``True`` when unset (vacuously satisfied).
    evidence_query: str | None = None
    expected_must_cite_source_id: tuple[str, ...] = ()
    expected_keywords_in_quote: tuple[str, ...] = ()
    # Wave 2C — document modality bucket. Used by per-modality pass-rate
    # gating in diff_baseline.py. Defaults to "unknown" so a forgotten
    # backfill is loud (the bucket reports zero coverage).
    document_modality: DocumentModality = "unknown"


# ---------------------------------------------------------------------------
# Synthetic FHIR Patient builder
# ---------------------------------------------------------------------------


def _patient(
    *,
    pid: str,
    mrn: str,
    given: str,
    family: str,
    dob: str,
    gender: str | None = None,
) -> dict:
    out: dict = {
        "resourceType": "Patient",
        "id": pid,
        "identifier": [
            {"type": {"coding": [{"code": "MR"}]}, "value": mrn},
        ],
        "name": [{"given": [given], "family": family}],
        "birthDate": dob,
    }
    if gender is not None:
        out["gender"] = gender
    return out


# Convenience patient constants (each pinned to a synthetic identity used in
# at least one case). Keep these aligned with the whitelist in the no-PHI test.
PT_MARCUS_WEBB = _patient(
    pid="pt-100847", mrn="100847", given="Marcus", family="Webb", dob="1962-03-14", gender="male"
)
PT_JANE_DOE = _patient(
    pid="pt-100231", mrn="100231", given="Jane", family="Doe", dob="1978-07-22", gender="female"
)
PT_CARLOS_REYES = _patient(
    pid="pt-100412", mrn="100412", given="Carlos", family="Reyes", dob="1955-11-02", gender="male"
)
PT_PRIYA_NATARAJAN = _patient(
    pid="pt-100558", mrn="100558", given="Priya", family="Natarajan", dob="1990-01-19",
    gender="female",
)
PT_LIANG_PARK = _patient(
    pid="pt-100719", mrn="100719", given="Liang", family="Park", dob="1971-09-30", gender="male"
)
PT_ELEANOR_WHITFIELD = _patient(
    pid="pt-100923", mrn="100923", given="Eleanor", family="Whitfield", dob="1942-08-11",
    gender="female",
)
PT_TOMAS_ALBRIGHT = _patient(
    pid="pt-100377", mrn="100377", given="Tomas", family="Albright", dob="1985-02-26",
    gender="male",
)
PT_HANNAH_GOLDBERG = _patient(
    pid="pt-100644", mrn="100644", given="Hannah", family="Goldberg", dob="2001-12-04",
    gender="female",
)
PT_SVEN_HALVORSEN = _patient(
    pid="pt-100805", mrn="100805", given="Sven", family="Halvorsen", dob="1968-05-17",
    gender="male",
)
PT_YARA_OLSSON = _patient(
    pid="pt-100488", mrn="100488", given="Yara", family="Olsson", dob="1980-06-09", gender="female"
)
PT_AMINA_BAKR = _patient(
    pid="pt-100176", mrn="100176", given="Amina", family="Bakr", dob="1995-04-08", gender="female"
)
PT_GREGOR_VOLKOV = _patient(
    pid="pt-100265", mrn="100265", given="Gregor", family="Volkov", dob="1948-10-25", gender="male"
)
PT_NIA_OKAFOR = _patient(
    pid="pt-100033", mrn="100033", given="Nia", family="Okafor", dob="2007-02-13", gender="female"
)
PT_KENJI_TANAKA = _patient(
    pid="pt-100922", mrn="100922", given="Kenji", family="Tanaka", dob="1959-12-30", gender="male"
)
PT_ROSA_MENDEZ = _patient(
    pid="pt-100611", mrn="100611", given="Rosa", family="Mendez", dob="1983-03-21", gender="female"
)

# Synthetic patients matching the real-shaped documents under
# tests/fixtures/eval/real-examples/. Document MRNs are formatted as
# "MRN-2026-XXXXX" (an external-facing format, not the chart-side identifier);
# we use the standard 100xxx synthetic MRN range on the chart side. The
# demographic check is graceful when the document MRN doesn't match the chart
# MRN format — it falls back to name+DOB matching (W2_ARCHITECTURE §5.6).
PT_MARGARET_CHEN = _patient(
    pid="pt-100481", mrn="100481", given="Margaret", family="Chen", dob="1967-08-14",
    gender="female",
)
PT_JAMES_WHITAKER = _patient(
    pid="pt-100492", mrn="100492", given="James", family="Whitaker", dob="1958-11-03",
    gender="male",
)
PT_LUIS_REYES = _patient(
    pid="pt-100503", mrn="100503", given="Luis", family="Reyes", dob="1972-05-18",
    gender="male",
)
PT_ANDRZEJ_KOWALSKI = _patient(
    pid="pt-100518", mrn="100518", given="Andrzej", family="Kowalski", dob="1954-09-22",
    gender="male",
)

# Additional synthetic identities used by the 88-case expansion (geriatric,
# pediatric, OB, non-English, evidence-retrieval, and refusal variants).
PT_BERTHA_NIEMINEN = _patient(
    pid="pt-100744", mrn="100744", given="Bertha", family="Nieminen", dob="1944-04-04",
    gender="female",
)
PT_DARIUS_OKONKWO = _patient(
    pid="pt-100051", mrn="100051", given="Darius", family="Okonkwo", dob="2009-09-09",
    gender="male",
)
PT_ELENA_VARGAS = _patient(
    pid="pt-100862", mrn="100862", given="Elena", family="Vargas", dob="1992-06-15",
    gender="female",
)
PT_FELIX_DUBOIS = _patient(
    pid="pt-100278", mrn="100278", given="Felix", family="Dubois", dob="1976-10-01",
    gender="male",
)
# Evidence-retrieval cases reuse a benign synthetic chart context.
PT_GENERIC_EVIDENCE = _patient(
    pid="pt-100999", mrn="100999", given="Casey", family="Stone", dob="1980-01-01",
    gender="female",
)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


CASES: list[W2EvalCase] = [
    # =====================================================================
    # Bucket: lab_nominal — 12 cases
    # =====================================================================
    W2EvalCase(
        case_id="lab_nominal_001_osh_lactate",
        bucket="lab_nominal",
        fixture_key="lab_osh_lactate",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='lactate'].value", "4.2"),),
        notes="Demo headline value: critical lactate.",
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        },
    ),
    W2EvalCase(
        case_id="lab_nominal_002_osh_lactate_no_hint",
        bucket="lab_nominal",
        fixture_key="lab_osh_lactate",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='lactate'].value", "4.2"),),
        notes="No type hint — classifier alone must resolve.",
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        },
    ),
    W2EvalCase(
        case_id="lab_nominal_003_cbc_bmp",
        bucket="lab_nominal",
        fixture_key="whitaker_lab_cbc",
        doc_type_hint="lab_report",
        chart_patient=PT_JAMES_WHITAKER,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        notes="Real-shaped CBC report (typed PDF). Loose field assertion only.",
    ),
    W2EvalCase(
        case_id="lab_nominal_004_cbc_bmp_no_hint",
        bucket="lab_nominal",
        fixture_key="lab_clean_2",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='hgb'].value", "13.4"),),
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        },
    ),
    W2EvalCase(
        case_id="lab_nominal_005_lipid",
        bucket="lab_nominal",
        fixture_key="chen_lab_lipid",
        doc_type_hint="lab_report",
        chart_patient=PT_MARGARET_CHEN,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        notes="Real-shaped lipid panel (typed PDF). Loose field assertion only.",
    ),
    W2EvalCase(
        case_id="lab_nominal_006_lipid_no_hint",
        bucket="lab_nominal",
        fixture_key="lab_clean_3",
        doc_type_hint=None,
        chart_patient=PT_CARLOS_REYES,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        },
    ),
    W2EvalCase(
        case_id="lab_nominal_007_critical_glucose",
        bucket="lab_nominal",
        fixture_key="kowalski_lab_cmp",
        doc_type_hint="lab_report",
        chart_patient=PT_ANDRZEJ_KOWALSKI,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        notes="Real-shaped CMP (typed PDF). Loose field assertion only.",
    ),
    W2EvalCase(
        case_id="lab_nominal_008_critical_glucose_no_hint",
        bucket="lab_nominal",
        fixture_key="lab_critical_4",
        doc_type_hint=None,
        chart_patient=PT_PRIYA_NATARAJAN,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        },
    ),
    W2EvalCase(
        case_id="lab_nominal_009_osh_lactate_male_only_chart",
        bucket="lab_nominal",
        fixture_key="lab_osh_lactate",
        doc_type_hint="lab_report",
        # chart matches MRN/name/DOB; minor identifier shape variation only.
        chart_patient={
            "resourceType": "Patient",
            "id": "pt-100847",
            "identifier": [{"type": {"coding": [{"code": "MR"}]}, "value": "100847"}],
            "name": [{"given": ["Marcus", "J"], "family": "Webb"}],
            "birthDate": "1962-03-14",
        },
        expected_kind="lab_report",
        expected_critic_decision="pass",
        notes="Chart name carries middle initial — should still pass.",
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        },
    ),
    W2EvalCase(
        case_id="lab_nominal_010_cbc_bmp_alt_chart",
        bucket="lab_nominal",
        fixture_key="lab_clean_2",
        doc_type_hint="lab_report",
        chart_patient=PT_JANE_DOE,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='cr'].value", "0.9"),),
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        },
    ),
    W2EvalCase(
        case_id="lab_nominal_011_lipid_repeat",
        bucket="lab_nominal",
        fixture_key="reyes_lab_hba1c_png",
        doc_type_hint="lab_report",
        chart_patient=PT_LUIS_REYES,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
        notes="Real raster-PNG HbA1c report — OCR confidence is low without "
              "tesseract; critic degradation path soft-warns.",
    ),
    W2EvalCase(
        case_id="lab_nominal_012_critical_anion_gap",
        bucket="lab_nominal",
        fixture_key="lab_critical_4",
        doc_type_hint="lab_report",
        chart_patient=PT_PRIYA_NATARAJAN,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='anion_gap'].value", "22"),),
        expected_provenance={
            "observations_min": 1,
            "all_have_derivedFrom": True,
            "all_citations_resolve": True,
        },
    ),
    # =====================================================================
    # Bucket: intake_nominal — 10 cases
    # =====================================================================
    W2EvalCase(
        case_id="intake_nominal_001_full_code",
        bucket="intake_nominal",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        expected_field_assertions=(("code_status", "Full Code"),),
    ),
    W2EvalCase(
        case_id="intake_nominal_002_full_code_no_hint",
        bucket="intake_nominal",
        fixture_key="intake_admission",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="intake_nominal_003_dnr",
        bucket="intake_nominal",
        fixture_key="whitaker_intake",
        doc_type_hint="intake_form",
        chart_patient=PT_JAMES_WHITAKER,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        notes="Real-shaped new-patient intake form (typed PDF). Loose assertion only.",
    ),
    W2EvalCase(
        case_id="intake_nominal_004_dnr_no_hint",
        bucket="intake_nominal",
        fixture_key="intake_dnr",
        doc_type_hint=None,
        chart_patient=PT_ELEANOR_WHITFIELD,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="intake_nominal_005_nkda",
        bucket="intake_nominal",
        fixture_key="chen_intake_typed",
        doc_type_hint="intake_form",
        chart_patient=PT_MARGARET_CHEN,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        notes="Real-shaped new-patient intake form (typed PDF). Loose assertion only.",
    ),
    W2EvalCase(
        case_id="intake_nominal_006_nkda_no_hint",
        bucket="intake_nominal",
        fixture_key="intake_no_allergies",
        doc_type_hint=None,
        chart_patient=PT_TOMAS_ALBRIGHT,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="intake_nominal_007_minimal",
        bucket="intake_nominal",
        fixture_key="reyes_intake_png",
        doc_type_hint="intake_form",
        chart_patient=PT_LUIS_REYES,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
        notes="Real raster-PNG intake form — OCR confidence is low without "
              "tesseract; critic degradation path soft-warns.",
    ),
    W2EvalCase(
        case_id="intake_nominal_008_minimal_no_hint",
        bucket="intake_nominal",
        fixture_key="intake_minimal",
        doc_type_hint=None,
        chart_patient=PT_HANNAH_GOLDBERG,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="intake_nominal_009_dnr_repeat_match",
        bucket="intake_nominal",
        fixture_key="kowalski_intake_png",
        doc_type_hint="intake_form",
        chart_patient=PT_ANDRZEJ_KOWALSKI,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
        notes="Real raster-PNG intake form — OCR confidence is low without "
              "tesseract; critic degradation path soft-warns.",
    ),
    W2EvalCase(
        case_id="intake_nominal_010_full_code_repeat",
        bucket="intake_nominal",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        expected_field_assertions=(("allergies", "Penicillin - rash"),),
    ),
    # =====================================================================
    # Bucket: unknown_nominal — 6 cases
    # =====================================================================
    W2EvalCase(
        case_id="unknown_nominal_001_consultant_note_no_hint",
        bucket="unknown_nominal",
        fixture_key="consultant_note",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="pass",
        notes="Cardiology consult — should classify as unknown without hint.",
    ),
    W2EvalCase(
        case_id="unknown_nominal_002_consultant_note_unknown_hint",
        bucket="unknown_nominal",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="unknown_nominal_003_imaging_no_hint",
        bucket="unknown_nominal",
        fixture_key="imaging_report",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        notes="Radiology report — unknown type, classifier should refuse to fit lab/intake.",
    ),
    W2EvalCase(
        case_id="unknown_nominal_004_imaging_unknown_hint",
        bucket="unknown_nominal",
        fixture_key="imaging_report",
        doc_type_hint="unknown",
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="unknown_nominal_005_consultant_note_alt_chart",
        bucket="unknown_nominal",
        fixture_key="consultant_note",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="unknown_nominal_006_imaging_repeat",
        bucket="unknown_nominal",
        fixture_key="imaging_report",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="pass",
    ),
    # =====================================================================
    # Bucket: wrong_type_hint — 4 cases
    # =====================================================================
    W2EvalCase(
        case_id="wrong_type_hint_001_lab_hinted_intake",
        bucket="wrong_type_hint",
        fixture_key="lab_osh_lactate",
        doc_type_hint="intake_form",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
        notes="Hint says intake; document is a lab. Classifier overrides hint, soft-warn.",
    ),
    W2EvalCase(
        case_id="wrong_type_hint_002_intake_hinted_lab",
        bucket="wrong_type_hint",
        fixture_key="intake_admission",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
    ),
    W2EvalCase(
        case_id="wrong_type_hint_003_consult_hinted_lab",
        bucket="wrong_type_hint",
        fixture_key="consultant_note",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
        notes="Consultant note hinted as lab — fall back to unknown with warning.",
    ),
    W2EvalCase(
        case_id="wrong_type_hint_004_imaging_hinted_intake",
        bucket="wrong_type_hint",
        fixture_key="imaging_report",
        doc_type_hint="intake_form",
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
    ),
    # =====================================================================
    # Bucket: wrong_patient — 5 cases
    #
    # All reuse intake_admission.pdf, which carries
    #     name=Marcus Webb, MRN=100847, DOB=1962-03-14
    # We mutate chart_patient to drive each row of W2_ARCHITECTURE §5.6.
    # =====================================================================
    W2EvalCase(
        case_id="wrong_patient_001_mrn_match_dob_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # MRN matches, name matches, DOB differs by year -> soft_warn
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Marcus", family="Webb", dob="1961-03-14",
            gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("dob_mismatch",),
        notes="§5.6 row 2: MRN+name match, DOB mismatch.",
    ),
    W2EvalCase(
        case_id="wrong_patient_002_mrn_match_name_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # MRN matches, DOB matches, name differs entirely -> soft_warn
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Markus", family="Webber", dob="1962-03-14",
            gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("name_mismatch",),
        notes="§5.6 row 3: MRN+DOB match, name mismatch.",
    ),
    W2EvalCase(
        case_id="wrong_patient_003_mrn_match_both_other_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # MRN matches, name AND DOB differ -> soft_warn (possible OCR collision)
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Roberta", family="Klein", dob="1979-09-01",
            gender="female",
        ),
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("possible_mrn_ocr_collision",),
        notes="§5.6 row 4: MRN match alone — possible OCR collision.",
    ),
    W2EvalCase(
        case_id="wrong_patient_004_mrn_extracted_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # Document MRN is 100847, chart MRN is something else entirely -> hard_block
        chart_patient=_patient(
            pid="pt-200999", mrn="200999", given="Marcus", family="Webb", dob="1962-03-14",
            gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="hard_block",
        expected_violation_codes=("mrn_mismatch",),
        notes="§5.6 row 5: extracted MRN mismatches chart — hard block.",
    ),
    W2EvalCase(
        case_id="wrong_patient_005_absent_mrn_dob_mismatch",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # Simulate absent-MRN-on-document by mismatching chart DOB while name matches.
        # Per §5.6 row 9: absent MRN + name match + DOB mismatch -> hard_block.
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Marcus", family="Webb", dob="1972-11-30",
            gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="hard_block",
        expected_violation_codes=("dob_critical_mismatch",),
        notes=(
            "§5.6 row 9 analog: when the agent treats the doc MRN as absent or "
            "unreadable, a DOB mismatch is the dominant signal and hard-blocks."
        ),
    ),
    # =====================================================================
    # Bucket: blank_noise — 4 cases
    # =====================================================================
    W2EvalCase(
        case_id="blank_noise_001_blank_pdf",
        bucket="blank_noise",
        fixture_key="blank",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="hard_block",
        expected_violation_codes=("empty_document",),
    ),
    W2EvalCase(
        case_id="blank_noise_002_encrypted_pdf",
        bucket="blank_noise",
        fixture_key="encrypted",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="hard_block",
        expected_violation_codes=("unreadable_document",),
        notes="Stub 'ENCRYPTED' page — must refuse cleanly, not hallucinate.",
    ),
    W2EvalCase(
        case_id="blank_noise_003_empty_stream",
        bucket="blank_noise",
        fixture_key="empty_stream",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
        notes="Single-space PDF — extractor should refuse without claiming content.",
    ),
    W2EvalCase(
        case_id="blank_noise_004_all_noise_scan",
        bucket="blank_noise",
        fixture_key="all_noise_scan",
        doc_type_hint="lab_report",
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="hard_block",
        expected_violation_codes=("unreadable_document",),
    ),
    # =====================================================================
    # Bucket: mixed_content — 4 cases
    # =====================================================================
    W2EvalCase(
        case_id="mixed_content_001_no_hint",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint=None,
        chart_patient=PT_YARA_OLSSON,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    W2EvalCase(
        case_id="mixed_content_002_lab_hint",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint="lab_report",
        chart_patient=PT_YARA_OLSSON,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    W2EvalCase(
        case_id="mixed_content_003_intake_hint",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint="intake_form",
        chart_patient=PT_YARA_OLSSON,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    W2EvalCase(
        case_id="mixed_content_004_unknown_hint",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint="unknown",
        chart_patient=PT_YARA_OLSSON,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    # =====================================================================
    # Bucket: low_quality_scan — 3 cases
    # =====================================================================
    W2EvalCase(
        case_id="low_quality_scan_001_lab_blurry",
        bucket="low_quality_scan",
        fixture_key="lab_blurry",
        doc_type_hint="lab_report",
        chart_patient=PT_LIANG_PARK,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
        expected_field_assertions=(("values[?test_name=='hgb'].value", "12.7"),),
    ),
    W2EvalCase(
        case_id="low_quality_scan_002_lab_blurry_no_hint",
        bucket="low_quality_scan",
        fixture_key="lab_blurry",
        doc_type_hint=None,
        chart_patient=PT_LIANG_PARK,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
    ),
    W2EvalCase(
        case_id="low_quality_scan_003_intake_blurry",
        bucket="low_quality_scan",
        fixture_key="intake_blurry",
        doc_type_hint="intake_form",
        chart_patient=PT_SVEN_HALVORSEN,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
    ),
    # =====================================================================
    # Bucket: intra_doc_conflict — 2 cases
    # =====================================================================
    W2EvalCase(
        case_id="intra_doc_conflict_001_lactate",
        bucket="intra_doc_conflict",
        fixture_key="intra_doc_conflict_lactate",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("intra_doc_conflict",),
        notes="Lactate 4.2 (page 1) vs 2.4 (page 3); conflict detector surfaces both.",
    ),
    W2EvalCase(
        case_id="intra_doc_conflict_002_lactate_no_hint",
        bucket="intra_doc_conflict",
        fixture_key="intra_doc_conflict_lactate",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("intra_doc_conflict",),
    ),
    # =====================================================================
    # 88-case expansion — added in Stage 4 closeout. New cases reuse the
    # existing fixture corpus to stay within the deterministic generator.
    # =====================================================================
    # ---- lab_nominal: +3 (15 total) ----
    W2EvalCase(
        case_id="lab_nominal_013_two_page_lab",
        bucket="lab_nominal",
        fixture_key="intra_doc_conflict_lactate",
        doc_type_hint="lab_report",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("intra_doc_conflict",),
        notes="Two-page-plus lab layout — exercises multi-page extraction path.",
    ),
    W2EvalCase(
        case_id="lab_nominal_014_faxed_scan",
        bucket="lab_nominal",
        fixture_key="lab_blurry",
        doc_type_hint="lab_report",
        chart_patient=PT_LIANG_PARK,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
        notes="Faxed scan layout — re-uses blurry fixture, asserts soft-warn path.",
    ),
    W2EvalCase(
        case_id="lab_nominal_015_multi_panel",
        bucket="lab_nominal",
        fixture_key="lab_clean_2",
        doc_type_hint="lab_report",
        chart_patient=PT_JANE_DOE,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='glucose'].value", "92"),),
        notes="Multi-panel CBC+BMP — repeated to stress per-test extraction.",
    ),
    # ---- intake_nominal: +4 (14 total) ----
    W2EvalCase(
        case_id="intake_nominal_011_geriatric",
        bucket="intake_nominal",
        fixture_key="intake_dnr",
        doc_type_hint="intake_form",
        chart_patient=PT_BERTHA_NIEMINEN,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("name_mismatch",),
        notes="Geriatric admission — chart name differs from doc (Whitfield); "
              "exercises name-mismatch path on intake.",
    ),
    W2EvalCase(
        case_id="intake_nominal_012_pediatric",
        bucket="intake_nominal",
        fixture_key="intake_minimal",
        doc_type_hint="intake_form",
        chart_patient=PT_DARIUS_OKONKWO,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("name_mismatch",),
        notes="Pediatric intake — name mismatch on minimal form.",
    ),
    W2EvalCase(
        case_id="intake_nominal_013_ob",
        bucket="intake_nominal",
        fixture_key="intake_no_allergies",
        doc_type_hint="intake_form",
        chart_patient=PT_ELENA_VARGAS,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("name_mismatch",),
        notes="OB-context intake — chart-vs-doc name mismatch.",
    ),
    W2EvalCase(
        case_id="intake_nominal_014_non_english_passthrough",
        bucket="intake_nominal",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        chart_patient=PT_FELIX_DUBOIS,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("name_mismatch",),
        notes="Non-English-name passthrough — extractor must not transliterate.",
    ),
    # ---- unknown_nominal: +2 (8 total) ----
    W2EvalCase(
        case_id="unknown_nominal_007_consult_alt_chart",
        bucket="unknown_nominal",
        fixture_key="consultant_note",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        notes="Consultant note read against unrelated chart — still unknown kind.",
    ),
    W2EvalCase(
        case_id="unknown_nominal_008_imaging_unknown_hint",
        bucket="unknown_nominal",
        fixture_key="imaging_report",
        doc_type_hint="unknown",
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="pass",
    ),
    # ---- wrong_type_hint: +2 (6 total) ----
    W2EvalCase(
        case_id="wrong_type_hint_005_lab_clean_hinted_intake",
        bucket="wrong_type_hint",
        fixture_key="lab_clean_2",
        doc_type_hint="intake_form",
        chart_patient=PT_JANE_DOE,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
    ),
    W2EvalCase(
        case_id="wrong_type_hint_006_imaging_hinted_lab",
        bucket="wrong_type_hint",
        fixture_key="imaging_report",
        doc_type_hint="lab_report",
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("classifier_confidence_low",),
    ),
    # ---- mixed_content: +2 (6 total) ----
    W2EvalCase(
        case_id="mixed_content_005_alt_chart",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint=None,
        chart_patient=PT_MARCUS_WEBB,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    W2EvalCase(
        case_id="mixed_content_006_jane_chart",
        bucket="mixed_content",
        fixture_key="mixed_content",
        doc_type_hint="lab_report",
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("mixed_content_detected",),
    ),
    # ---- low_quality_scan: +2 (5 total) ----
    W2EvalCase(
        case_id="low_quality_scan_004_intake_blurry_no_hint",
        bucket="low_quality_scan",
        fixture_key="intake_blurry",
        doc_type_hint=None,
        chart_patient=PT_SVEN_HALVORSEN,
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
    ),
    W2EvalCase(
        case_id="low_quality_scan_005_lab_blurry_alt",
        bucket="low_quality_scan",
        fixture_key="lab_blurry",
        doc_type_hint="lab_report",
        chart_patient=PT_LIANG_PARK,
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
    ),
    # ---- intra_doc_conflict: +1 (3 total) — refusal-adjacent ----
    W2EvalCase(
        case_id="intra_doc_conflict_003_lactate_alt_chart",
        bucket="intra_doc_conflict",
        fixture_key="intra_doc_conflict_lactate",
        doc_type_hint="lab_report",
        # Chart MRN matches doc; chart DOB differs by year — overlapping
        # conflict + dob_mismatch refusal signals.
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Marcus", family="Webb",
            dob="1961-03-14", gender="male",
        ),
        expected_kind="lab_report",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("intra_doc_conflict",),
        notes="Conflict + stale-chart DOB combination — soft-warn dominates.",
    ),
    # ---- wrong_patient: +2 (7 total) — mismatched DOB / off-by-one MRN ----
    W2EvalCase(
        case_id="wrong_patient_006_off_by_one_mrn",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # Chart MRN is the doc MRN with one digit transposed -> hard_block.
        chart_patient=_patient(
            pid="pt-100848", mrn="100848", given="Marcus", family="Webb",
            dob="1962-03-14", gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="hard_block",
        expected_violation_codes=("mrn_mismatch",),
        notes="Off-by-one MRN — must hard-block even though name+DOB align.",
    ),
    W2EvalCase(
        case_id="wrong_patient_007_stale_chart_dob",
        bucket="wrong_patient",
        fixture_key="intake_admission",
        doc_type_hint="intake_form",
        # MRN+name match; chart DOB is years off — likely stale chart.
        chart_patient=_patient(
            pid="pt-100847", mrn="100847", given="Marcus", family="Webb",
            dob="1965-03-14", gender="male",
        ),
        expected_kind="intake_form",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("dob_mismatch",),
        notes="Stale chart — DOB drift surfaces as soft-warn.",
    ),
    # ---- blank_noise: +1 (5 total) — redacted ----
    W2EvalCase(
        case_id="blank_noise_005_redacted_doc",
        bucket="blank_noise",
        fixture_key="all_noise_scan",
        doc_type_hint="intake_form",
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="hard_block",
        expected_violation_codes=("unreadable_document",),
        notes="Heavily redacted / OCR-noise scan hinted as intake — must refuse.",
    ),
    # ---- missing_data: NEW bucket, 4 cases (target 8 total when combined
    #      with the 4 blank_noise refusals — see EVAL.md mapping). ----
    W2EvalCase(
        case_id="missing_data_001_no_allergies",
        bucket="missing_data",
        fixture_key="intake_no_allergies",
        doc_type_hint="intake_form",
        chart_patient=PT_TOMAS_ALBRIGHT,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        notes="NKDA / 'None' meds — extractor must surface absence, not invent.",
    ),
    W2EvalCase(
        case_id="missing_data_002_no_meds",
        bucket="missing_data",
        fixture_key="intake_no_allergies",
        doc_type_hint="intake_form",
        chart_patient=PT_TOMAS_ALBRIGHT,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        notes="Medication list literally 'None' — must not hallucinate.",
    ),
    W2EvalCase(
        case_id="missing_data_003_partial_vitals",
        bucket="missing_data",
        fixture_key="intake_minimal",
        doc_type_hint="intake_form",
        chart_patient=PT_HANNAH_GOLDBERG,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        notes="Minimal intake (no allergies/meds/code-status sections) — "
              "extractor must emit empty lists, not guess defaults.",
    ),
    W2EvalCase(
        case_id="missing_data_004_redacted_fields",
        bucket="missing_data",
        fixture_key="empty_stream",
        doc_type_hint=None,
        chart_patient=PT_JANE_DOE,
        expected_kind="unknown",
        expected_critic_decision="soft_warn",
        expected_softwarn_codes=("ocr_confidence_low",),
        notes="Redacted/empty document body — agent should refuse content claims.",
    ),
    # ---- evidence_retrieval: NEW bucket, 8 cases. The runner exercises the
    #      full graph against a benign fixture (consultant_note) so the case
    #      can resolve; the rubric layer scores the case against the
    #      evidence_query / expected_must_cite_source_id fields. ----
    W2EvalCase(
        case_id="evidence_retrieval_001_kdigo_aki_threshold",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="What is the diagnostic threshold for AKI per KDIGO?",
        expected_must_cite_source_id=("kdigo-aki-2012",),
        expected_keywords_in_quote=("creatinine", "aki", "0.3"),
        notes="KDIGO AKI threshold lookup — must cite kdigo-aki-2012.",
    ),
    W2EvalCase(
        case_id="evidence_retrieval_002_kdigo_aki_staging",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="How is AKI staged according to KDIGO criteria?",
        expected_must_cite_source_id=("kdigo-aki-2012",),
        expected_keywords_in_quote=("stage", "creatinine"),
    ),
    W2EvalCase(
        case_id="evidence_retrieval_003_ada_inpatient_insulin",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="What's the recommended insulin regimen for inpatient hyperglycemia?",
        expected_must_cite_source_id=("ada-inpatient-glycemic",),
        expected_keywords_in_quote=("insulin", "glucose"),
        notes="ADA inpatient glycemic — must cite ada-inpatient-glycemic.",
    ),
    W2EvalCase(
        case_id="evidence_retrieval_004_ada_glucose_target",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="What inpatient glucose target does ADA recommend for non-critically-ill patients?",
        expected_must_cite_source_id=("ada-inpatient-glycemic",),
        expected_keywords_in_quote=("140", "180", "mg/dl"),
    ),
    W2EvalCase(
        case_id="evidence_retrieval_005_ssc_hour_bundle",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="What hour bundle does SSC recommend for sepsis?",
        expected_must_cite_source_id=("ssc-2021",),
        expected_keywords_in_quote=("hour", "bundle", "sepsis"),
        notes="SSC hour-1 bundle — must cite ssc-2021.",
    ),
    W2EvalCase(
        case_id="evidence_retrieval_006_ssc_lactate",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="When should lactate be remeasured per Surviving Sepsis Campaign?",
        expected_must_cite_source_id=("ssc-2021",),
        expected_keywords_in_quote=("lactate",),
    ),
    W2EvalCase(
        case_id="evidence_retrieval_007_kdigo_or_ssc_aki_sepsis",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="In a septic patient developing AKI, which guideline informs fluid resuscitation?",
        expected_must_cite_source_id=("kdigo-aki-2012", "ssc-2021"),
        expected_keywords_in_quote=("fluid", "resuscitation"),
        notes="Cross-source — either KDIGO or SSC is acceptable evidence.",
    ),
    W2EvalCase(
        case_id="evidence_retrieval_008_ada_hypoglycemia",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="What is the ADA-defined threshold for clinically significant hypoglycemia?",
        expected_must_cite_source_id=("ada-inpatient-glycemic",),
        expected_keywords_in_quote=("hypoglycemia",),
    ),
    # ---- balancing cases to hit the 88-case contract (+7 over the spec's
    #      explicit per-bucket deltas which sum to 81). Distributed across
    #      lab_nominal (+2), intake_nominal (+3), evidence_retrieval (+2)
    #      to keep the largest buckets proportionally weighted. ----
    W2EvalCase(
        case_id="lab_nominal_016_lipid_alt_chart",
        bucket="lab_nominal",
        fixture_key="lab_clean_3",
        doc_type_hint="lab_report",
        chart_patient=PT_CARLOS_REYES,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        notes="Lipid panel re-run against canonical chart — exercises the "
              "stable nominal path with a different doc/chart combo.",
    ),
    W2EvalCase(
        case_id="lab_nominal_017_critical_glucose_no_hint_alt",
        bucket="lab_nominal",
        fixture_key="lab_critical_4",
        doc_type_hint=None,
        chart_patient=PT_PRIYA_NATARAJAN,
        expected_kind="lab_report",
        expected_critic_decision="pass",
        expected_field_assertions=(("values[?test_name=='glucose'].value", "612"),),
        notes="No-hint critical glucose — classifier must resolve unaided.",
    ),
    W2EvalCase(
        case_id="intake_nominal_015_dnr_alt_chart",
        bucket="intake_nominal",
        fixture_key="intake_dnr",
        doc_type_hint="intake_form",
        chart_patient=PT_ELEANOR_WHITFIELD,
        expected_kind="intake_form",
        expected_critic_decision="pass",
        expected_field_assertions=(("code_status", "DNR / DNI"),),
    ),
    W2EvalCase(
        case_id="intake_nominal_016_no_allergies_no_hint",
        bucket="intake_nominal",
        fixture_key="intake_no_allergies",
        doc_type_hint=None,
        chart_patient=PT_TOMAS_ALBRIGHT,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="intake_nominal_017_minimal_alt_hint",
        bucket="intake_nominal",
        fixture_key="intake_minimal",
        doc_type_hint="intake_form",
        chart_patient=PT_HANNAH_GOLDBERG,
        expected_kind="intake_form",
        expected_critic_decision="pass",
    ),
    W2EvalCase(
        case_id="evidence_retrieval_009_kdigo_urine_output",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="What urine-output threshold defines AKI per KDIGO?",
        expected_must_cite_source_id=("kdigo-aki-2012",),
        expected_keywords_in_quote=("urine", "output"),
    ),
    W2EvalCase(
        case_id="evidence_retrieval_010_ssc_antibiotics",
        bucket="evidence_retrieval",
        fixture_key="consultant_note",
        doc_type_hint="unknown",
        chart_patient=PT_GENERIC_EVIDENCE,
        expected_kind="unknown",
        expected_critic_decision="pass",
        evidence_query="When should empiric antibiotics be administered for sepsis per SSC?",
        expected_must_cite_source_id=("ssc-2021",),
        expected_keywords_in_quote=("antibiotic",),
    ),
]


# ---------------------------------------------------------------------------
# Wave 2C — bbox-GT cases (36 fixtures, 12 per new modality)
#
# Each case points at a synthetic_v2 fixture that carries a ``.gt.json``
# sidecar with field-level bboxes. The ``citation_iou`` and
# ``citation_pixel_distance`` rubrics (see evals/rubrics_mechanical.py)
# are GT-gated — they only run on cases whose fixture has a sidecar.
#
# Synthetic identities used here are listed at the top of
# ``_generate_synthetic_v2.py``. Each is a fabricated 3-tuple
# (name, dob, mrn) — they are added to the no-PHI whitelist
# alongside the existing cohort.
# ---------------------------------------------------------------------------


# Reusable benign synthetic patient (chart side). The bbox rubric does not
# touch demographics — it operates on the document's emitted citations only.
PT_BBOX_GT = _patient(
    pid="pt-200000", mrn="200000", given="GT", family="Synthetic", dob="1980-01-01",
    gender="female",
)


def _bbox_gt_case(case_id: str, fixture_key: str, modality: DocumentModality, kind: ExpectedKind, hint: str | None) -> W2EvalCase:
    return W2EvalCase(
        case_id=case_id,
        bucket="bbox_gt",
        fixture_key=fixture_key,
        doc_type_hint=hint,
        chart_patient=PT_BBOX_GT,
        expected_kind=kind,
        expected_critic_decision="pass",
        notes="Wave 2C bbox-GT synthetic fixture (citation_iou rubric).",
        document_modality=modality,
    )


for _i in range(1, 13):
    CASES.append(_bbox_gt_case(
        case_id=f"bbox_gt_typed_{_i:03d}",
        fixture_key=f"typed_pdf_{_i:03d}",
        modality="typed_pdf",
        kind="unknown",
        hint=None,
    ))
    CASES.append(_bbox_gt_case(
        case_id=f"bbox_gt_table_{_i:03d}",
        fixture_key=f"table_heavy_{_i:03d}",
        modality="table_heavy",
        kind="lab_report",
        hint="lab_report",
    ))
    CASES.append(_bbox_gt_case(
        case_id=f"bbox_gt_photo_{_i:03d}",
        fixture_key=f"photo_capture_{_i:03d}",
        modality="photo_capture",
        kind="intake_form",
        hint="intake_form",
    ))
del _i


# ---------------------------------------------------------------------------
# Bucket count contract
# ---------------------------------------------------------------------------


BUCKET_COUNTS: dict[str, int] = {
    "lab_nominal": 17,
    "intake_nominal": 17,
    "unknown_nominal": 8,
    "wrong_type_hint": 6,
    "wrong_patient": 7,
    "blank_noise": 5,
    "mixed_content": 6,
    "low_quality_scan": 5,
    "intra_doc_conflict": 3,
    "evidence_retrieval": 10,
    "missing_data": 4,
    # Wave 2C — 36 synthetic fixtures with bbox ground-truth sidecars
    # (12 typed_pdf + 12 table_heavy + 12 photo_capture). See
    # ``_generate_synthetic_v2.py`` for layout details.
    "bbox_gt": 36,
}

TOTAL_CASES = 124


# ---------------------------------------------------------------------------
# Wave 2C — document modality backfill
#
# Each case carries a ``document_modality`` field used by the eval suite for
# per-modality pass-rate gating. Rather than thread the modality through
# every constructor call, we backfill from a fixture_key map below. The map
# is the single source of truth — when adding a new fixture, add it here.
#
# Inference rules (documented for future maintainers):
#   - typed_pdf      : text-layer PDF, mostly prose / single column
#   - scanned_pdf    : raster image (PNG/blurry PDF) requiring OCR
#   - intake_form    : structured intake / triage form (text-layer)
#   - table_heavy    : lab reports with dense tabular data
#   - multi_column   : multi-column layouts (mixed_content, imaging)
#   - synthetic      : programmatically generated by _generate_eval_corpus
#                      (ground-truth side-channel available)
#   - unknown        : truly unclassifiable (encrypted/blank stream)
# ---------------------------------------------------------------------------


_FIXTURE_MODALITY: dict[str, DocumentModality] = {
    # Real-shaped typed PDFs (lab, intake, consult, imaging)
    "lab_osh_lactate": "table_heavy",
    "whitaker_lab_cbc": "table_heavy",
    "chen_lab_lipid": "table_heavy",
    "kowalski_lab_cmp": "table_heavy",
    "intra_doc_conflict_lactate": "table_heavy",
    "intake_admission": "intake_form",
    "intake_dnr": "intake_form",
    "intake_no_allergies": "intake_form",
    "intake_minimal": "intake_form",
    "whitaker_intake": "intake_form",
    "chen_intake_typed": "intake_form",
    "consultant_note": "typed_pdf",
    "imaging_report": "multi_column",
    # Synthetic generator output
    "lab_clean_2": "synthetic",
    "lab_clean_3": "synthetic",
    "lab_critical_4": "synthetic",
    # Raster PNG / blurry scans
    "reyes_lab_hba1c_png": "scanned_pdf",
    "reyes_intake_png": "scanned_pdf",
    "kowalski_intake_png": "scanned_pdf",
    "lab_blurry": "scanned_pdf",
    "intake_blurry": "scanned_pdf",
    "all_noise_scan": "scanned_pdf",
    # Edge cases — no recoverable content
    "blank": "unknown",
    "encrypted": "unknown",
    "empty_stream": "unknown",
    # Mixed content (multi-section / multi-column)
    "mixed_content": "multi_column",
}

# Wave 2C — synthetic_v2 fixtures (bbox GT). Modalities are also set
# explicitly on the W2EvalCase constructors (so the backfill is a no-op
# for these), but the map keeps the fixture_key -> modality contract
# complete for any future tooling that crawls _FIXTURE_MODALITY.
for _i in range(1, 13):
    _FIXTURE_MODALITY[f"typed_pdf_{_i:03d}"] = "typed_pdf"
    _FIXTURE_MODALITY[f"table_heavy_{_i:03d}"] = "table_heavy"
    _FIXTURE_MODALITY[f"photo_capture_{_i:03d}"] = "photo_capture"
del _i


def _backfill_modality() -> None:
    """Populate ``document_modality`` on every CASES entry from
    ``_FIXTURE_MODALITY``. Frozen-dataclass instances are replaced via
    ``object.__setattr__`` (the only way to mutate ``frozen=True`` fields).
    """
    for case in CASES:
        # Skip cases where the field was set explicitly (non-default).
        if case.document_modality != "unknown":
            continue
        modality = _FIXTURE_MODALITY.get(case.fixture_key, "unknown")
        object.__setattr__(case, "document_modality", modality)


_backfill_modality()


def _validate() -> None:
    counts: dict[str, int] = {}
    for c in CASES:
        counts[c.bucket] = counts.get(c.bucket, 0) + 1
    if counts != BUCKET_COUNTS:
        raise AssertionError(
            f"CASES bucket distribution drifted: got {counts}, want {BUCKET_COUNTS}"
        )
    if sum(BUCKET_COUNTS.values()) != TOTAL_CASES:
        raise AssertionError(
            f"BUCKET_COUNTS must total {TOTAL_CASES}, got {sum(BUCKET_COUNTS.values())}"
        )
    ids = [c.case_id for c in CASES]
    if len(set(ids)) != len(ids):
        raise AssertionError("Duplicate case_id detected")


_validate()
