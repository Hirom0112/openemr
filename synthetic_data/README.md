# Synthetic Data — Clinical Co-Pilot

18 synthetic FHIR R4 patient bundles for the Clinical Co-Pilot pilot.
Fixed seed `42` — fully reproducible, no real patient data.

## Quick start

```bash
# 1. Generate bundles (no dependencies beyond Python 3.11+)
python3 synthetic_data/generate.py

# 2. Load into Railway OpenEMR instance
BASE_URL=https://your-app.up.railway.app \
CLIENT_ID=your-client-id \
CLIENT_SECRET=your-client-secret \
python3 synthetic_data/load.py
```

## Patient roster

| ID     | Name               | Bed | Scenario                                  | Priority    |
|--------|--------------------|-----|-------------------------------------------|-------------|
| pt-001 | Marcus Webb        | 501 | S1 — qSOFA=3 (sepsis/pneumonia)           | URGENT P1   |
| pt-002 | Delia Fontaine     | 512 | S2 — Critical K+ 6.4 unack'd at 03:12    | URGENT P2   |
| pt-003 | Raymond Okafor     | 507 | S3 — Blank code status (COPD)             | WATCH P7    |
| pt-004 | Gloria Tran        | 514 | S4 — Incomplete allergy section (CHF)     | WATCH       |
| pt-005 | Bernard Kowalski   | 503 | S5 — PCN allergy + overnight Amoxicillin  | WATCH P5    |
| pt-006 | Ingrid Nakamura    | 518 | S6 — Discharge plan + chest CT pending    | WATCH P6    |
| pt-007 | Darnell Simmons    | 522 | S7 — Nephrology consult unanswered >6 h   | WATCH P8    |
| pt-008 | Yvonne Castillo    | 509 | S8 — Conflicting creatinine (1.2 vs 2.1)  | Conflict    |
| pt-009 | Elena Morales      | 504 | Stable — UTI                              | STABLE      |
| pt-010 | Rajiv Patel        | 506 | Stable — Cellulitis                       | STABLE      |
| pt-011 | Karl Bergstrom     | 508 | Stable — GI bleed / PUD                  | STABLE      |
| pt-012 | Miriam Johnson     | 510 | Stable — Ischemic stroke                  | STABLE      |
| pt-013 | Carlos Reyes       | 511 | Stable — DKA                              | STABLE      |
| pt-014 | Abena Osei         | 513 | Stable — Pancreatitis                     | STABLE      |
| pt-015 | Dorothy Williams   | 515 | Stable — CHF diuresis                     | STABLE      |
| pt-016 | Wei Huang          | 516 | Stable — Post-op day 1                    | STABLE      |
| pt-017 | Sean Murphy        | 517 | Stable — Alcohol withdrawal               | STABLE      |
| pt-018 | Thomas Greer       | 520 | S10 — Out-of-census (prov-other)          | Cross-cov.  |

## Scenarios map

| Scenario | Coverage |
|----------|---------|
| S1 qSOFA ≥ 2 | pt-001 Marcus Webb |
| S2 Critical unacknowledged lab | pt-002 Delia Fontaine — K+ 6.4 @ 03:12 |
| S3 Blank code status | pt-003 Raymond Okafor |
| S4 Incomplete allergy section | pt-004 Gloria Tran |
| S5 Allergy-medication conflict | pt-005 Bernard Kowalski — PCN + Amoxicillin |
| S6 Discharge plan + pending result | pt-006 Ingrid Nakamura — chest CT registered |
| S7 Consult unanswered > 6 h | pt-007 Darnell Simmons — Nephrology |
| S8 Conflicting values same field | pt-008 Yvonne Castillo — creatinine 1.2 vs 2.1 |
| S9 Census > 16 patients | Satisfied by 18-patient total |
| S10 Out-of-census patient | pt-018 Thomas Greer — prov-other, not prov-chen |

## Spot checks after loading

```bash
# 16+ patients visible
GET /apis/default/fhir/Patient?_count=20

# Marcus Webb qSOFA vitals
GET /apis/default/fhir/Observation?patient=pt-001&code=8480-6,9279-1,9269-2

# Delia Fontaine critical K+
GET /apis/default/fhir/Observation?patient=pt-002&code=6298-4

# pt-018 has different provider (cross-coverage test)
GET /apis/default/fhir/Encounter?patient=pt-018
```
