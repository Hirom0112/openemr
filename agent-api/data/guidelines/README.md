# Clinical Copilot — Guidelines Corpus

Curated, citable snippets used by the clinical copilot retriever to attach
guideline support to extracted patient findings.

## What's covered

| Bucket | Source | Year | Entries |
| --- | --- | --- | --- |
| `diabetes_t2dm` | ADA Standards of Care in Diabetes | 2025 | 8 |
| `lipids_ascvd` | AHA/ACC Multisociety Cholesterol Guideline | 2018 | 6 |
| `nafld_masld` | AASLD Practice Guidance on MASLD/NAFLD | 2023 | 6 |
| `ckd_kdigo` | KDIGO Clinical Practice Guideline for CKD | 2024 | 6 |
| `afib_anticoag` | ACC/AHA/ACCP/HRS AFib Guideline + AGA IDA Guideline | 2023 / 2020 | 6 |
| `alcohol_screening` | USPSTF Unhealthy Alcohol Use (incl. AUDIT-C) | 2018 | 5 |
| `chest_pain` | AHA/ACC Chest Pain Evaluation Guideline | 2021 | 6 |

Total: **43 entries** across 7 buckets.

All `source_url` fields point to public, citable journal/society pages
(`diabetesjournals.org`, `ahajournals.org`, `jacc.org`, `aasld.org`,
`kdigo.org`, `gastrojournal.org`, `uspreventiveservicestaskforce.org`).
Snippets are paraphrased verbatim where possible; recommendations are
preserved unaltered. Each entry that paraphrases is flagged inline in the
`snippet` field.

## Demo patients matched

- **Reyes** — uncontrolled T2DM (HbA1c 9.2%), HTN, obesity → `diabetes_t2dm`,
  `lipids_ascvd`, `alcohol_screening`.
- **Kowalski** — RUQ pain, transaminitis, eGFR 62, K 3.3, hypertriglyceridemia,
  alcohol use → `nafld_masld`, `ckd_kdigo`, `alcohol_screening`,
  `lipids_ascvd`.
- **Chen** — T2DM, HTN, mixed dyslipidemia, exertional chest tightness, FHx
  premature MI → `diabetes_t2dm`, `lipids_ascvd`, `chest_pain`.
- **Whitaker** — AFib on apixaban, BPH, hyperlipidemia, new normocytic anemia
  → `afib_anticoag` (incl. AGA IDA workup).

## Schema

Each entry:

```json
{
  "id": "stable-id",
  "condition_tags": ["lowercase-hyphen-tags"],
  "title": "short title",
  "snippet": "verbatim or paraphrased recommendation",
  "source_org": "issuing organization",
  "source_doc": "document title",
  "source_section": "section number/name",
  "source_url": "https://...",
  "applies_when": "patient-context guard",
  "demo_relevance": "which demo patient(s) this fires for"
}
```

`condition_tags` are matched (case-insensitive, hyphen-joined) against
extracted patient fields by the retriever. Existing tags include:
`t2dm`, `hba1c`, `glycemic-target`, `metformin`, `glp1`, `sglt2`, `ascvd`,
`ldl`, `statin`, `primary-prevention`, `htn`, `blood-pressure`, `bp-target`,
`ckd`, `egfr`, `acr`, `nephropathy-screen`, `retinopathy-screen`,
`nafld`, `masld`, `fib-4`, `transaminitis`, `ruq-pain`, `metabolic-syndrome`,
`alcohol-use`, `audit-c`, `metALD`, `weight-loss`,
`ckd-staging`, `cga-classification`, `chronicity`, `nephrology-referral`,
`raas`, `monitoring`,
`afib`, `cha2ds2-vasc`, `has-bled`, `doac`, `apixaban`, `anticoagulation`,
`stroke-risk`, `stroke-risk-modifiers`, `bleeding-risk`,
`anemia-on-anticoag`, `occult-gi-bleed`, `ida`, `ferritin`, `endoscopy`,
`screening`, `uspstf-b`, `screening-tool`, `interpretation`,
`brief-intervention`, `behavioral-counseling`, `aud`, `referral`,
`stable-angina`, `chest-pain`, `pretest-probability`, `ccta`,
`stress-imaging`, `ischemia`, `diagnostic-testing`, `low-risk`, `deferral`,
`shared-decision-making`, `evaluation`,
`fhx-premature-mi`, `ascvd-risk`, `ascvd-risk-enhancers`,
`severe-hypercholesterolemia`, `intermediate-risk`,
`ascvd-secondary-prevention`, `high-intensity`, `obesity`,
`first-line`, `intensification`, `individualization`, `older-adult`,
`hf`, `ckd-stage-2`, `t2dm-screening`.

## How to add more

1. Pick the right bucket file (or create a new one and add it to `index.json
   .buckets`).
2. Append an entry to the bucket's `entries` array using the schema above.
3. Add a matching mini-entry to `index.json .entries` (id, bucket,
   condition_tags, title, source_org).
4. Re-run any retriever unit tests; both files are loaded at startup.
5. Source must be citable (URL must resolve); if a guideline is paywalled,
   use the open press summary or PMC mirror and note "Paraphrased" in the
   snippet.

## Notes / known caveats

- ADA 2026 Standards of Care exists; we deliberately use the **2025** edition
  to match what is published as the current Standards of Care across most
  retrieval contexts and to keep section numbering stable.
- `kdigo-2024-bp` references KDIGO 2021 BP guidance carried into the 2024 CKD
  guideline; flagged as paraphrased.
- AFib bucket cross-cuts the AGA 2020 IDA guideline because the demo's
  Whitaker case (AFib + new anemia) requires the GI workup recommendation in
  the same retrieval slice.
