#!/usr/bin/env python3
"""
Loads synthetic patient data into OpenEMR via REST API + direct MySQL.

OpenEMR's FHIR API is read-only, so we write via:
  - REST API  (/apis/default/api/...)  for patients, encounters, allergies,
                                        conditions, medications
  - Direct MySQL                        for vitals (form_vitals) and labs
                                        (procedure_order/report/result) because
                                        the REST vitals endpoint has an authUserId
                                        bug in this OpenEMR version

Required environment variables:
  BASE_URL        e.g. https://your-railway-app.up.railway.app
  CLIENT_ID       agent-api-loader OAuth2 client ID
  CLIENT_SECRET   agent-api-loader OAuth2 client secret
  OE_USER         OpenEMR admin username  (default: admin)
  OE_PASS         OpenEMR admin password  (default: your-openemr-admin-password)
  MYSQL_HOST      TCP proxy host          (default: shuttle.proxy.rlwy.net)
  MYSQL_PORT      TCP proxy port          (default: 12805)
  MYSQL_USER      MySQL user              (default: root)
  MYSQL_PASS      MySQL password
  MYSQL_DB        database name           (default: openemr)

Usage:
  BASE_URL=https://... CLIENT_ID=... CLIENT_SECRET=... MYSQL_PASS=... python3 load.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

try:
    import pymysql
except ImportError:
    print("pymysql not installed. Run: pip3 install pymysql", file=sys.stderr)
    sys.exit(1)

BUNDLES_DIR = pathlib.Path(__file__).parent / "bundles"

SCOPE = (
    "openid api:oemr "
    "user/patient.crus user/encounter.crus user/vital.crus "
    "user/medical_problem.cruds user/medication.cruds user/allergy.cruds "
    "user/Patient.rs user/Encounter.rs user/Observation.rs "
    "user/Condition.rs user/MedicationRequest.rs "
    "user/AllergyIntolerance.rs user/DiagnosticReport.rs"
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_token(base_url: str, client_id: str, client_secret: str,
              oe_user: str, oe_pass: str) -> str:
    data = urllib.parse.urlencode({
        "grant_type": "password",
        "client_id": client_id,
        "client_secret": client_secret,
        "username": oe_user,
        "password": oe_pass,
        "user_role": "users",
        "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/oauth2/default/token", data=data, method="POST"
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read())
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"No token: {body}")
    return token


# ---------------------------------------------------------------------------
# REST API helpers
# ---------------------------------------------------------------------------

def api_post(base_url: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{base_url}/apis/default/api/{path.lstrip('/')}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"error": e.code, "body": body}


def create_patient(base_url: str, token: str, p: dict[str, Any]) -> tuple[int, str]:
    """Returns (pid, puuid)."""
    result = api_post(base_url, token, "patient", {
        "fname": p["fname"],
        "lname": p["lname"],
        "DOB": p["dob"],
        "sex": p["sex"],
        "street": p.get("street", ""),
        "city": p.get("city", "Springfield"),
        "state": p.get("state", "IL"),
        "postal_code": p.get("postal_code", "62701"),
    })
    data = result.get("data", {})
    pid = data.get("pid")
    puuid = data.get("uuid", "")
    if not pid:
        raise RuntimeError(f"Patient creation failed for {p['fname']} {p['lname']}: {result}")
    return int(pid), puuid


def create_encounter(base_url: str, token: str, puuid: str,
                     date: str, reason: str) -> tuple[int, str]:
    """Returns (eid, euuid)."""
    result = api_post(base_url, token, f"patient/{puuid}/encounter", {
        "date": date,
        "onset_date": date,
        "reason": reason,
        "facility_id": 1,
        "pc_catid": 5,
        "class_code": "IMP",
    })
    data = result.get("data", {})
    eid = data.get("eid") or data.get("encounter")
    euuid = data.get("euuid", "")
    if not eid:
        raise RuntimeError(f"Encounter creation failed: {result}")
    return int(eid), euuid


def create_allergy(base_url: str, token: str, puuid: str,
                   title: str, reaction: str = "", allergy_type: str = "allergy") -> None:
    payload: dict[str, Any] = {"title": title, "begdate": "2020-01-01"}
    if reaction:
        payload["reaction"] = reaction
    if allergy_type:
        payload["allergy_type"] = allergy_type
    api_post(base_url, token, f"patient/{puuid}/allergy", payload)


def create_condition(base_url: str, token: str, puuid: str,
                     title: str, icd: str = "") -> None:
    payload: dict[str, Any] = {"title": title, "begdate": "2026-04-27"}
    if icd:
        payload["diagnosis"] = icd
    api_post(base_url, token, f"patient/{puuid}/medical_problem", payload)


def create_medication(base_url: str, token: str, pid: int, title: str, route: str = "Oral") -> None:
    api_post(base_url, token, f"patient/{pid}/medication", {
        "title": title,
        "route": route,
        "unit": "1",
        "form": "Tablet",
    })


# ---------------------------------------------------------------------------
# MySQL helpers (vitals + labs)
# ---------------------------------------------------------------------------

def mysql_conn(cfg: dict[str, str]):
    return pymysql.connect(
        host=cfg["host"], port=int(cfg["port"]),
        user=cfg["user"], password=cfg["password"],
        database=cfg["db"], connect_timeout=10,
        charset="utf8mb4",
    )


# LOINC codes that OpenEMR's FHIR Observation vitals service maps to uuid_mapping
_VITAL_LOINC_CODES = [
    "85353-1",  # Vital signs panel
    "9279-1",   # Respiratory Rate
    "8867-4",   # Heart rate
    "2708-6",   # O2 saturation
    "59408-5",  # Pulse oximetry O2 sat
    "8310-5",   # Body Temperature
    "8327-9",   # Temperature Location
    "8302-2",   # Body Height
    "9843-4",   # Head circumference
    "29463-7",  # Body Weight
    "39156-5",  # BMI
    "85354-9",  # Blood pressure panel
    "8289-1",   # Ped head circumference
    "59576-9",  # Ped BMI
    "77606-2",  # Ped weight-for-height
]


def insert_vitals(conn, pid: int, eid: int, dt: str,
                  bps: int, bpd: int, pulse: int, respiration: int,
                  temperature: float, oxygen_saturation: float,
                  weight: float = 0, height: float = 0) -> None:
    uid = uuid.uuid4().bytes
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO form_vitals
              (uuid, date, pid, user, groupname, authorized, activity,
               bps, bpd, pulse, respiration, temperature, temp_method,
               oxygen_saturation, weight, height)
            VALUES (%s,%s,%s,'admin','Default',1,1,%s,%s,%s,%s,%s,'Oral',%s,%s,%s)
        """, (uid, dt, pid, str(bps), str(bpd), pulse, respiration,
              temperature, oxygen_saturation, weight, height))
        form_vitals_id = cur.lastrowid

        # Register in forms table so OpenEMR's FHIR layer can find these vitals
        cur.execute("""
            INSERT INTO forms
              (date, encounter, form_name, form_id, pid, user, groupname, authorized, deleted, formdir)
            VALUES (%s,%s,'Vitals',%s,%s,'admin','Default',1,0,'vitals')
        """, (dt, eid, form_vitals_id, pid))

        # Register in uuid_registry (required by OpenEMR's UuidRegistry::getRegistryForTable)
        cur.execute("""
            INSERT IGNORE INTO uuid_registry
              (uuid, table_name, table_id, table_vertical, couchdb, document_drive, mapped)
            VALUES (%s,'form_vitals','id','','',0,0)
        """, (uid,))

        # Create uuid_mapping entries — one per LOINC code — so FHIR Observation queries resolve
        for loinc in _VITAL_LOINC_CODES:
            mapping_uuid = uuid.uuid4().bytes
            resource_path = f"category=vital-signs&code={loinc}"
            cur.execute("""
                INSERT INTO uuid_mapping (uuid, resource, resource_path, `table`, target_uuid)
                VALUES (%s,'Observation',%s,'form_vitals',%s)
            """, (mapping_uuid, resource_path, uid))

    conn.commit()


def insert_lab(conn, pid: int, eid: int, ordered_dt: str, collected_dt: str,
               loinc: str, name: str, value: str, units: str,
               ref_range: str, abnormal: str, status: str = "final") -> None:
    order_uid = uuid.uuid4().bytes
    report_uid = uuid.uuid4().bytes
    result_uid = uuid.uuid4().bytes

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO procedure_order
              (uuid, provider_id, patient_id, encounter_id,
               date_ordered, date_collected, order_status, activity,
               procedure_order_type, order_intent)
            VALUES (%s,1,%s,%s,%s,%s,'complete',1,'laboratory_test','order')
        """, (order_uid, pid, eid, ordered_dt, collected_dt))
        order_id = cur.lastrowid

        # procedure_order_code required so ProcedureService JOIN resolves (procedure_order_seq must match)
        cur.execute("""
            INSERT INTO procedure_order_code
              (procedure_order_id, procedure_order_seq, procedure_code, procedure_name,
               procedure_order_title, do_not_send)
            VALUES (%s,1,%s,%s,%s,0)
        """, (order_id, loinc, name, name))

        cur.execute("""
            INSERT INTO procedure_report
              (uuid, procedure_order_id, procedure_order_seq, date_collected, date_report,
               report_status, review_status)
            VALUES (%s,%s,1,%s,%s,%s,'received')
        """, (report_uid, order_id, collected_dt, collected_dt, status))
        report_id = cur.lastrowid

        cur.execute("""
            INSERT INTO procedure_result
              (uuid, procedure_report_id, result_data_type, result_code,
               result_text, date, units, result, `range`, abnormal, result_status)
            VALUES (%s,%s,'N',%s,%s,%s,%s,%s,%s,%s,%s)
        """, (result_uid, report_id, loinc, name, collected_dt,
              units, value, ref_range, abnormal, status))
    conn.commit()


# ---------------------------------------------------------------------------
# Patient loader functions — one per scenario
# ---------------------------------------------------------------------------

# The static admit dates in the PATIENTS list span 2026-04-26..2026-04-30.
# That window is fine on the day the data was authored, but the Co-Pilot
# census query filters `form_encounter.date >= NOW() - 7 DAY`, so once the
# deploy clock advances past ANCHOR_DATE + 7 days these encounters silently
# fall out of the panel. Map each static date to a rolling date relative to
# today so the panel keeps working without re-seeding.
_ANCHOR_DATE = date(2026, 4, 30)


def rolling_admit_date(static_admit_date: str) -> str:
    """Map a static YYYY-MM-DD admit date to today - (ANCHOR - static) days.

    The most recent static date (ANCHOR) becomes today; earlier static dates
    are spread out as the same number of days earlier than today. Preserves
    the relative ordering and spacing of the original dataset.
    """
    try:
        d = datetime.strptime(static_admit_date, "%Y-%m-%d").date()
    except ValueError:
        return static_admit_date
    offset = (_ANCHOR_DATE - d).days
    if offset < 0:
        offset = 0
    return (date.today() - timedelta(days=offset)).isoformat()


def load_patient(base_url: str, token: str, conn,
                 p_def: dict[str, Any],
                 chen_user_id: int = 0) -> None:
    """Load one patient and all their clinical data."""
    name = f"{p_def['fname']} {p_def['lname']}"
    try:
        pid, puuid = create_patient(base_url, token, p_def)
        print(f"  Patient  pid={pid} puuid={puuid[:8]}...")

        # Encounter — use a rolling date so the encounter never ages out of
        # the 7-day census window.
        admit_date = rolling_admit_date(p_def.get("admit_date", "2026-04-27"))
        reason = p_def.get("admit_reason", "Inpatient admission")
        eid, _ = create_encounter(base_url, token, puuid, admit_date, reason)
        print(f"  Encounter eid={eid}")

        # Assign provider_id in form_encounter so index.php census filter works.
        # "other" patients get provider_id=0 (excluded from Chen's panel).
        # Chen patients get her real user_id so they show on her 15-patient panel.
        target_provider_id = 0 if p_def.get("provider") == "other" else chen_user_id
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE form_encounter SET provider_id = %s WHERE encounter = %s",
                (target_provider_id, eid),
            )
        conn.commit()

        # Allergies
        for alg in p_def.get("allergies", []):
            create_allergy(base_url, token, puuid,
                           alg["title"], alg.get("reaction", ""),
                           alg.get("type", "allergy"))

        # Conditions
        for cond in p_def.get("conditions", []):
            create_condition(base_url, token, puuid,
                             cond["title"], cond.get("icd", ""))

        # Medications
        for med in p_def.get("medications", []):
            create_medication(base_url, token, pid, med["title"], med.get("route", "Oral"))

        # Vitals (direct MySQL)
        for v in p_def.get("vitals", []):
            insert_vitals(conn, pid, eid, v["dt"],
                          v.get("bps", 120), v.get("bpd", 80),
                          v.get("pulse", 80), v.get("respiration", 16),
                          v.get("temperature", 37.0),
                          v.get("oxygen_saturation", 98),
                          v.get("weight", 0), v.get("height", 0))

        # Labs (direct MySQL)
        ordered_dt = f"2026-04-29 02:00:00"
        for lab in p_def.get("labs", []):
            insert_lab(conn, pid, eid,
                       ordered_dt, lab["collected_dt"],
                       lab["loinc"], lab["name"],
                       lab["value"], lab["units"],
                       lab.get("range", ""), lab.get("abnormal", "normal"),
                       lab.get("status", "final"))

        print(f"  OK — {len(p_def.get('vitals',[]))} vitals, "
              f"{len(p_def.get('labs',[]))} labs, "
              f"{len(p_def.get('allergies',[]))} allergies, "
              f"{len(p_def.get('conditions',[]))} conditions, "
              f"{len(p_def.get('medications',[]))} meds")

    except Exception as exc:
        print(f"  FAIL: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Patient definitions (18 patients, seed 42)
# ---------------------------------------------------------------------------

PATIENTS: list[dict[str, Any]] = [

    # ── S1: Marcus Webb — qSOFA=3 (URGENT P1) ─────────────────────────────
    {
        "fname": "Marcus", "lname": "Webb", "dob": "1968-03-14", "sex": "Male",
        "admit_date": "2026-04-27", "admit_reason": "Pneumonia/Sepsis — bed 501",
        "conditions": [{"title": "Community-acquired pneumonia", "icd": "J18.9"},
                       {"title": "Sepsis", "icd": "A41.9"}],
        "allergies": [{"title": "Sulfa drugs", "reaction": "Rash", "type": "allergy"}],
        "medications": [{"title": "Piperacillin-Tazobactam 3.375g IV q6h"},
                        {"title": "Norepinephrine 0.1 mcg/kg/min IV"}],
        "vitals": [
            # qSOFA: SBP<=100 (1pt), RR>=22 (1pt), AMS (GCS<15 approximated by note)
            {"dt": "2026-04-29 05:15:00", "bps": 88, "bpd": 55,
             "pulse": 118, "respiration": 26, "temperature": 38.9,
             "oxygen_saturation": 91, "weight": 82, "height": 175},
            {"dt": "2026-04-29 03:00:00", "bps": 94, "bpd": 60,
             "pulse": 110, "respiration": 24, "temperature": 38.7,
             "oxygen_saturation": 93},
        ],
        "labs": [
            {"loinc": "2518-9", "name": "Lactate", "collected_dt": "2026-04-29 04:30:00",
             "value": "4.2", "units": "mmol/L", "range": "0.5-2.2", "abnormal": "high",
             "status": "final"},
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 03:45:00",
             "value": "18.3", "units": "K/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
        ],
    },

    # ── S2: Delia Fontaine — Critical K+ 6.4 unacknowledged (URGENT P2) ──
    {
        "fname": "Delia", "lname": "Fontaine", "dob": "1952-07-22", "sex": "Female",
        "admit_date": "2026-04-28", "admit_reason": "AKI on CKD Stage 4",
        "conditions": [{"title": "Acute kidney injury", "icd": "N17.9"},
                       {"title": "Chronic kidney disease stage 4", "icd": "N18.4"}],
        "allergies": [{"title": "Ibuprofen", "reaction": "AKI", "type": "allergy"}],
        "medications": [{"title": "Furosemide 40mg IV"},
                        {"title": "Sodium bicarbonate 150mEq/L IV"}],
        "vitals": [
            {"dt": "2026-04-29 05:30:00", "bps": 152, "bpd": 94,
             "pulse": 72, "respiration": 18, "temperature": 36.8,
             "oxygen_saturation": 96},
        ],
        "labs": [
            # Critical K+ at 03:12 — no physician acknowledgment
            {"loinc": "6298-4", "name": "Potassium", "collected_dt": "2026-04-29 03:12:00",
             "value": "6.4", "units": "mEq/L", "range": "3.5-5.1", "abnormal": "critical",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 03:12:00",
             "value": "5.8", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "critical",
             "status": "final"},
        ],
    },

    # ── S3: Raymond Okafor — Blank code status (WATCH P7) ─────────────────
    {
        "fname": "Raymond", "lname": "Okafor", "dob": "1944-11-05", "sex": "Male",
        "admit_date": "2026-04-28", "admit_reason": "COPD exacerbation",
        "conditions": [{"title": "COPD exacerbation", "icd": "J44.1"}],
        "allergies": [],  # no known allergies — clean
        "medications": [{"title": "Ipratropium-Albuterol nebulizer q4h"},
                        {"title": "Methylprednisolone 125mg IV q8h"}],
        "vitals": [
            {"dt": "2026-04-29 05:00:00", "bps": 138, "bpd": 82,
             "pulse": 88, "respiration": 20, "temperature": 37.2,
             "oxygen_saturation": 90},
        ],
        "labs": [
            {"loinc": "2745-0", "name": "pH arterial", "collected_dt": "2026-04-29 04:00:00",
             "value": "7.32", "units": "", "range": "7.35-7.45", "abnormal": "low",
             "status": "final"},
        ],
        # No code_status entry — satisfies S3
    },

    # ── S4: Gloria Tran — Incomplete allergy section (WATCH) ──────────────
    {
        "provider": "other",
        "fname": "Gloria", "lname": "Tran", "dob": "1958-04-30", "sex": "Female",
        "admit_date": "2026-04-27", "admit_reason": "CHF exacerbation",
        "conditions": [{"title": "Congestive heart failure", "icd": "I50.9"}],
        # Allergy with no reaction or type — satisfies S4
        "allergies": [{"title": "Unknown substance", "reaction": "", "type": ""}],
        "medications": [{"title": "Furosemide 80mg IV bid"},
                        {"title": "Lisinopril 10mg daily"}],
        "vitals": [
            {"dt": "2026-04-29 05:45:00", "bps": 168, "bpd": 98,
             "pulse": 92, "respiration": 22, "temperature": 36.9,
             "oxygen_saturation": 92, "weight": 94, "height": 162},
        ],
        "labs": [
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 03:30:00",
             "value": "1.6", "units": "mg/dL", "range": "0.5-1.1", "abnormal": "high",
             "status": "final"},
            {"loinc": "6298-4", "name": "Potassium", "collected_dt": "2026-04-29 03:30:00",
             "value": "3.2", "units": "mEq/L", "range": "3.5-5.1", "abnormal": "low",
             "status": "final"},
        ],
    },

    # ── S5: Bernard Kowalski — Penicillin allergy + Amoxicillin (WATCH P5) ─
    {
        "provider": "other",
        "fname": "Bernard", "lname": "Kowalski", "dob": "1962-09-18", "sex": "Male",
        "admit_date": "2026-04-28", "admit_reason": "Community-acquired pneumonia",
        "conditions": [{"title": "Community-acquired pneumonia", "icd": "J18.1"}],
        "allergies": [{"title": "Penicillin", "reaction": "Anaphylaxis", "type": "allergy"}],
        # Amoxicillin added overnight — conflicts with Penicillin allergy
        "medications": [{"title": "Amoxicillin 500mg PO tid"},
                        {"title": "Azithromycin 500mg IV daily"}],
        "vitals": [
            {"dt": "2026-04-29 05:00:00", "bps": 124, "bpd": 76,
             "pulse": 84, "respiration": 18, "temperature": 37.8,
             "oxygen_saturation": 95},
        ],
        "labs": [
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 02:30:00",
             "value": "14.2", "units": "K/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
        ],
    },

    # ── S6: Ingrid Nakamura — Discharge plan + pending CT (WATCH P6) ──────
    {
        "provider": "other",
        "fname": "Ingrid", "lname": "Nakamura", "dob": "1971-02-14", "sex": "Female",
        "admit_date": "2026-04-26", "admit_reason": "PE workup — dyspnea",
        "conditions": [{"title": "Pulmonary embolism workup", "icd": "Z03.89"},
                       {"title": "Dyspnea", "icd": "R06.09"}],
        "allergies": [{"title": "Contrast dye", "reaction": "Hives", "type": "allergy"}],
        "medications": [{"title": "Heparin 5000 units SC q8h"},
                        {"title": "Enoxaparin 1mg/kg SC q12h"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 118, "bpd": 72,
             "pulse": 78, "respiration": 16, "temperature": 36.7,
             "oxygen_saturation": 97},
        ],
        "labs": [
            {"loinc": "3255-7", "name": "D-dimer", "collected_dt": "2026-04-29 01:00:00",
             "value": "3.8", "units": "ug/mL", "range": "0.0-0.5", "abnormal": "high",
             "status": "final"},
            # CT chest ordered but pending (status=registered)
            {"loinc": "24627-2", "name": "CT Chest", "collected_dt": "2026-04-29 06:00:00",
             "value": "PENDING", "units": "", "range": "", "abnormal": "unknown",
             "status": "registered"},
        ],
    },

    # ── S7: Darnell Simmons — Nephrology consult unanswered >6h (WATCH P8) ─
    {
        "fname": "Darnell", "lname": "Simmons", "dob": "1955-06-30", "sex": "Male",
        "admit_date": "2026-04-28", "admit_reason": "Hypertensive urgency / AKI",
        "conditions": [{"title": "Hypertensive urgency", "icd": "I16.0"},
                       {"title": "Acute kidney injury", "icd": "N17.9"}],
        "allergies": [{"title": "Lisinopril", "reaction": "Cough", "type": "allergy"}],
        "medications": [{"title": "Labetalol 200mg PO bid"},
                        {"title": "Amlodipine 10mg daily"}],
        "vitals": [
            {"dt": "2026-04-29 05:30:00", "bps": 188, "bpd": 112,
             "pulse": 80, "respiration": 16, "temperature": 36.6,
             "oxygen_saturation": 98},
        ],
        "labs": [
            # Nephrology consult ordered 9h ago (22:00 prior evening), unanswered
            {"loinc": "57778-7", "name": "Nephrology Consult", "collected_dt": "2026-04-28 22:00:00",
             "value": "PENDING", "units": "", "range": "", "abnormal": "unknown",
             "status": "registered"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 02:00:00",
             "value": "3.1", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "high",
             "status": "final"},
        ],
    },

    # ── S8: Yvonne Castillo — Conflicting creatinine values ───────────────
    {
        "fname": "Yvonne", "lname": "Castillo", "dob": "1967-12-03", "sex": "Female",
        "admit_date": "2026-04-28", "admit_reason": "Sepsis — urinary source",
        "conditions": [{"title": "Urosepsis", "icd": "A41.51"}],
        "allergies": [{"title": "Cephalosporins", "reaction": "Rash", "type": "allergy"}],
        "medications": [{"title": "Gentamicin 5mg/kg IV q24h"},
                        {"title": "Vancomycin 25mg/kg IV q12h"}],
        "vitals": [
            {"dt": "2026-04-29 04:45:00", "bps": 102, "bpd": 62,
             "pulse": 106, "respiration": 20, "temperature": 38.4,
             "oxygen_saturation": 95},
        ],
        "labs": [
            # Conflicting creatinine — nurse POC says 1.2, lab says 2.1 same day
            {"loinc": "2160-0", "name": "Creatinine (POC-Nurse)", "collected_dt": "2026-04-29 03:00:00",
             "value": "1.2", "units": "mg/dL", "range": "0.5-1.1", "abnormal": "high",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine (Lab)", "collected_dt": "2026-04-29 03:00:00",
             "value": "2.1", "units": "mg/dL", "range": "0.5-1.1", "abnormal": "critical",
             "status": "final"},
        ],
    },

    # ── pt-009: Stable — UTI ───────────────────────────────────────────────
    {
        "provider": "other",
        "fname": "Patricia", "lname": "Nguyen", "dob": "1975-08-19", "sex": "Female",
        "admit_date": "2026-04-28", "admit_reason": "Uncomplicated UTI",
        "conditions": [{"title": "Urinary tract infection", "icd": "N39.0"}],
        "allergies": [{"title": "Trimethoprim", "reaction": "Rash", "type": "allergy"}],
        "medications": [{"title": "Nitrofurantoin 100mg PO bid"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 118, "bpd": 74,
             "pulse": 76, "respiration": 14, "temperature": 37.1,
             "oxygen_saturation": 99},
        ],
        "labs": [
            {"loinc": "5778-6", "name": "Urinalysis WBC", "collected_dt": "2026-04-28 18:00:00",
             "value": ">50", "units": "/hpf", "range": "0-5", "abnormal": "high",
             "status": "final"},
        ],
    },

    # ── pt-010: Stable — Cellulitis ───────────────────────────────────────
    {
        "provider": "other",
        "fname": "Gerald", "lname": "Hoffman", "dob": "1980-01-25", "sex": "Male",
        "admit_date": "2026-04-28", "admit_reason": "Left leg cellulitis",
        "conditions": [{"title": "Cellulitis, left lower leg", "icd": "L03.116"}],
        "allergies": [],
        "medications": [{"title": "Cefazolin 1g IV q8h"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 128, "bpd": 80,
             "pulse": 82, "respiration": 15, "temperature": 37.6,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-28 19:00:00",
             "value": "12.4", "units": "K/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
        ],
    },

    # ── pt-011: Stable — GI Bleed ─────────────────────────────────────────
    {
        "provider": "other",
        "fname": "Rosemary", "lname": "Delgado", "dob": "1950-05-12", "sex": "Female",
        "admit_date": "2026-04-27", "admit_reason": "Upper GI bleed",
        "conditions": [{"title": "Acute upper GI hemorrhage", "icd": "K92.0"}],
        "allergies": [{"title": "Aspirin", "reaction": "GI bleed", "type": "allergy"}],
        "medications": [{"title": "Pantoprazole 40mg IV bid"},
                        {"title": "Octreotide 50mcg/hr IV"}],
        "vitals": [
            {"dt": "2026-04-29 05:30:00", "bps": 112, "bpd": 68,
             "pulse": 94, "respiration": 16, "temperature": 36.8,
             "oxygen_saturation": 97},
        ],
        "labs": [
            {"loinc": "718-7", "name": "Hemoglobin", "collected_dt": "2026-04-29 02:00:00",
             "value": "7.2", "units": "g/dL", "range": "12.0-16.0", "abnormal": "low",
             "status": "final"},
        ],
    },

    # ── pt-012: Stable — Stroke ───────────────────────────────────────────
    {
        "fname": "Walter", "lname": "Osei", "dob": "1948-03-07", "sex": "Male",
        "admit_date": "2026-04-27", "admit_reason": "Ischemic stroke — left MCA",
        "conditions": [{"title": "Cerebral infarction, left MCA", "icd": "I63.512"}],
        "allergies": [{"title": "Warfarin", "reaction": "Bleeding", "type": "allergy"}],
        "medications": [{"title": "Aspirin 325mg daily"},
                        {"title": "Atorvastatin 80mg daily"}],
        "vitals": [
            {"dt": "2026-04-29 05:00:00", "bps": 148, "bpd": 88,
             "pulse": 72, "respiration": 16, "temperature": 36.9,
             "oxygen_saturation": 97},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 03:00:00",
             "value": "142", "units": "mg/dL", "range": "70-100", "abnormal": "high",
             "status": "final"},
        ],
    },

    # ── pt-013: Stable — DKA ──────────────────────────────────────────────
    {
        "fname": "Amelia", "lname": "Burke", "dob": "1992-10-28", "sex": "Female",
        "admit_date": "2026-04-28", "admit_reason": "Diabetic ketoacidosis",
        "conditions": [{"title": "Type 1 DKA", "icd": "E10.10"}],
        "allergies": [],
        "medications": [{"title": "Regular insulin infusion 0.1 units/kg/hr"},
                        {"title": "Normal saline 1L/hr IV"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 110, "bpd": 66,
             "pulse": 96, "respiration": 22, "temperature": 36.5,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 05:00:00",
             "value": "210", "units": "mg/dL", "range": "70-100", "abnormal": "high",
             "status": "final"},
            {"loinc": "6298-4", "name": "Potassium", "collected_dt": "2026-04-29 05:00:00",
             "value": "4.1", "units": "mEq/L", "range": "3.5-5.1", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-014: Stable — Pancreatitis ─────────────────────────────────────
    {
        "provider": "other",
        "fname": "Jerome", "lname": "Whitfield", "dob": "1969-07-16", "sex": "Male",
        "admit_date": "2026-04-28", "admit_reason": "Acute pancreatitis",
        "conditions": [{"title": "Acute pancreatitis", "icd": "K85.90"}],
        "allergies": [{"title": "Codeine", "reaction": "Nausea", "type": "allergy"}],
        "medications": [{"title": "Morphine 2mg IV q4h PRN"},
                        {"title": "Ondansetron 4mg IV q6h PRN"}],
        "vitals": [
            {"dt": "2026-04-29 05:45:00", "bps": 122, "bpd": 76,
             "pulse": 88, "respiration": 16, "temperature": 37.3,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "1798-8", "name": "Lipase", "collected_dt": "2026-04-28 20:00:00",
             "value": "1842", "units": "U/L", "range": "10-140", "abnormal": "high",
             "status": "final"},
        ],
    },

    # ── pt-015: Stable — CHF (compensated) ───────────────────────────────
    {
        "provider": "other",
        "fname": "Lillian", "lname": "Archer", "dob": "1943-04-02", "sex": "Female",
        "admit_date": "2026-04-27", "admit_reason": "CHF — volume overload",
        "conditions": [{"title": "Congestive heart failure", "icd": "I50.32"}],
        "allergies": [{"title": "Spironolactone", "reaction": "Hyperkalemia", "type": "allergy"}],
        "medications": [{"title": "Furosemide 40mg IV bid"},
                        {"title": "Carvedilol 12.5mg PO bid"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 132, "bpd": 80,
             "pulse": 78, "respiration": 18, "temperature": 36.8,
             "oxygen_saturation": 94, "weight": 88, "height": 158},
        ],
        "labs": [
            {"loinc": "33762-6", "name": "BNP", "collected_dt": "2026-04-29 01:00:00",
             "value": "820", "units": "pg/mL", "range": "0-100", "abnormal": "high",
             "status": "final"},
        ],
    },

    # ── pt-016: Stable — Post-op Hip ─────────────────────────────────────
    {
        "fname": "Douglas", "lname": "Pearce", "dob": "1941-11-19", "sex": "Male",
        "admit_date": "2026-04-27", "admit_reason": "Post-op right hip arthroplasty",
        "conditions": [{"title": "Post-op right total hip arthroplasty", "icd": "Z96.641"}],
        "allergies": [{"title": "Latex", "reaction": "Urticaria", "type": "allergy"}],
        "medications": [{"title": "Oxycodone 5mg PO q4h PRN"},
                        {"title": "Enoxaparin 40mg SC daily"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 126, "bpd": 78,
             "pulse": 74, "respiration": 14, "temperature": 36.7,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "718-7", "name": "Hemoglobin", "collected_dt": "2026-04-29 04:00:00",
             "value": "9.8", "units": "g/dL", "range": "13.5-17.5", "abnormal": "low",
             "status": "final"},
        ],
    },

    # ── pt-017: Stable — Atrial Fibrillation ─────────────────────────────
    {
        "provider": "other",
        "fname": "Sandra", "lname": "Morrow", "dob": "1956-09-14", "sex": "Female",
        "admit_date": "2026-04-28", "admit_reason": "New-onset atrial fibrillation",
        "conditions": [{"title": "Atrial fibrillation, new onset", "icd": "I48.0"}],
        "allergies": [],
        "medications": [{"title": "Diltiazem 30mg PO qid"},
                        {"title": "Apixaban 5mg PO bid"}],
        "vitals": [
            {"dt": "2026-04-29 05:30:00", "bps": 138, "bpd": 86,
             "pulse": 112, "respiration": 16, "temperature": 36.8,
             "oxygen_saturation": 97},
        ],
        "labs": [
            {"loinc": "6299-2", "name": "Thyroid TSH", "collected_dt": "2026-04-29 02:00:00",
             "value": "0.08", "units": "mIU/L", "range": "0.4-4.0", "abnormal": "low",
             "status": "final"},
        ],
    },

    # ── S10 / pt-018: Thomas Greer — Out-of-census (prov-other) ──────────
    {
        "provider": "other",
        "fname": "Thomas", "lname": "Greer", "dob": "1983-05-22", "sex": "Male",
        "admit_date": "2026-04-29", "admit_reason": "Chest pain — rule out ACS",
        "conditions": [{"title": "Chest pain, unspecified", "icd": "R07.9"}],
        "allergies": [{"title": "Metoprolol", "reaction": "Bronchospasm", "type": "allergy"}],
        "medications": [{"title": "Aspirin 325mg PO"},
                        {"title": "Nitroglycerin 0.4mg SL PRN"}],
        "vitals": [
            {"dt": "2026-04-29 06:30:00", "bps": 142, "bpd": 88,
             "pulse": 96, "respiration": 16, "temperature": 36.6,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "49563-0", "name": "Troponin I", "collected_dt": "2026-04-29 05:00:00",
             "value": "0.04", "units": "ng/mL", "range": "0.0-0.04", "abnormal": "borderline",
             "status": "final"},
        ],
    },

    # ── pt-019: Linda Okonkwo — minor fall, code-status documented, no active dx (P10 Routine) ──────────────
    {
        "fname": "Linda", "lname": "Okonkwo", "dob": "1966-08-30", "sex": "Female",
        "admit_date": "2026-04-30", "admit_reason": "Observation after minor fall — no fracture, no active diagnosis",
        # No active conditions — pure observation stay so the rules engine reaches level 10 (Routine).
        "conditions": [],
        "allergies": [],
        "medications": [{"title": "Acetaminophen 650mg PO q6h PRN"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 124, "bpd": 76,
             "pulse": 72, "respiration": 15, "temperature": 37.0,
             "oxygen_saturation": 97},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-28 20:00:00",
             "value": "98", "units": "mg/dL", "range": "70-100", "abnormal": "normal",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-28 20:00:00",
             "value": "0.9", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
            # Resuscitation status documented — clears the blank-code-status flag so this patient can land at P10.
            {"loinc": "81638-3", "name": "Resuscitation status", "collected_dt": "2026-04-29 06:00:00",
             "value": "Full Code", "units": "", "range": "", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-020: Robert Finch — Pre-procedure obs, no active dx, code status undocumented (P9) ────
    {
        "fname": "Robert", "lname": "Finch", "dob": "1959-03-17", "sex": "Male",
        "admit_date": "2026-04-29", "admit_reason": "Pre-procedure observation — elective colonoscopy prep",
        # No active conditions and no code-status observation — rules engine drops to level 9 (Blank Code Status).
        "conditions": [],
        "allergies": [],
        "medications": [{"title": "Polyethylene glycol 3350 solution PO"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 118, "bpd": 74,
             "pulse": 68, "respiration": 14, "temperature": 36.8,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 04:00:00",
             "value": "94", "units": "mg/dL", "range": "70-100", "abnormal": "normal",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 04:00:00",
             "value": "0.8", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-021: Priya Anand — Suspected sepsis (airborne precautions) ─────
    {
        "fname": "Priya", "lname": "Anand", "dob": "1975-04-12", "sex": "Female",
        "admit_date": "2026-04-29", "admit_reason": "Suspected sepsis — source under investigation",
        "conditions": [{"title": "Septicemia, unspecified organism", "icd": "A41.9"}],
        "allergies": [],
        "medications": [{"title": "Vancomycin 1g IV q12h"},
                        {"title": "Piperacillin-tazobactam 3.375g IV q6h"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 96, "bpd": 62,
             "pulse": 108, "respiration": 24, "temperature": 38.6,
             "oxygen_saturation": 94},
        ],
        "labs": [
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-29 04:00:00",
             "value": "14.2", "units": "10*3/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
            {"loinc": "2518-9", "name": "Lactate", "collected_dt": "2026-04-29 04:00:00",
             "value": "1.8", "units": "mmol/L", "range": "0.5-2.0", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-022: James Whitfield — Acute delirium with documented GCS<15 (P5 Mental Status Alert) ──────────────
    {
        "fname": "James", "lname": "Whitfield", "dob": "1942-10-05", "sex": "Male",
        "admit_date": "2026-04-28", "admit_reason": "Acute delirium — hyperactive type",
        "conditions": [{"title": "Delirium due to known physiological condition", "icd": "F05"}],
        "allergies": [],
        "medications": [{"title": "Haloperidol 0.5mg IV PRN agitation"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 128, "bpd": 74,
             "pulse": 88, "respiration": 17, "temperature": 37.3,
             "oxygen_saturation": 96},
        ],
        "labs": [
            {"loinc": "2823-3", "name": "Potassium", "collected_dt": "2026-04-29 03:00:00",
             "value": "4.0", "units": "mmol/L", "range": "3.5-5.0", "abnormal": "normal",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 03:00:00",
             "value": "1.1", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
            # GCS Total 12 — emitted via the lab path so it lands as a FHIR Observation with LOINC 9269-2.
            # The triage extractor reads any observation by LOINC, so this trips mental_status_alert and lands the patient at P5.
            {"loinc": "9269-2", "name": "Glasgow Coma Scale Total", "collected_dt": "2026-04-29 06:00:00",
             "value": "12", "units": "{score}", "range": "13-15", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-023: Keisha Balogun — Sickle cell vaso-occlusive crisis with pain 9/10 (P6 Severe Pain) ────────
    {
        "fname": "Keisha", "lname": "Balogun", "dob": "1988-07-19", "sex": "Female",
        "admit_date": "2026-04-28", "admit_reason": "Sickle cell disease with acute vaso-occlusive crisis",
        "conditions": [{"title": "Sickle-cell disease with crisis", "icd": "D57.00"}],
        "allergies": [],
        "medications": [{"title": "Morphine 4mg IV q3h PRN pain"},
                        {"title": "Ketorolac 15mg IV q6h"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 118, "bpd": 72,
             "pulse": 98, "respiration": 18, "temperature": 37.5,
             "oxygen_saturation": 96},
        ],
        "labs": [
            {"loinc": "718-7", "name": "Hemoglobin", "collected_dt": "2026-04-29 02:00:00",
             "value": "8.2", "units": "g/dL", "range": "12.0-16.0", "abnormal": "low",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 02:00:00",
             "value": "0.8", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
            # Pain score 9/10 — emitted via the lab path. Triage extractor scans any observation by LOINC,
            # so this trips pain_score_high and the patient lands at P6 (ahead of the P7 abnormal-lab tier).
            {"loinc": "72514-3", "name": "Pain severity 0-10", "collected_dt": "2026-04-29 06:00:00",
             "value": "9", "units": "{score}", "range": "0-3", "abnormal": "normal",
             "status": "final"},
        ],
    },

    # ── pt-024: Alejandro Cruz — Intra-abdominal sepsis, admitted overnight (P1) ──
    {
        "fname": "Alejandro", "lname": "Cruz", "dob": "1971-05-20", "sex": "Male",
        "admit_date": "2026-04-30", "admit_reason": "Intra-abdominal sepsis — secondary peritonitis, bed 528",
        "conditions": [{"title": "Sepsis due to intra-abdominal infection", "icd": "A41.9"},
                       {"title": "Secondary peritonitis", "icd": "K65.1"}],
        "allergies": [],
        "medications": [{"title": "Meropenem 1g IV q8h"},
                        {"title": "Metronidazole 500mg IV q8h"},
                        {"title": "Norepinephrine 0.05 mcg/kg/min IV"}],
        "vitals": [
            {"dt": "2026-04-30 04:15:00", "bps": 94, "bpd": 58,
             "pulse": 114, "respiration": 26, "temperature": 38.7,
             "oxygen_saturation": 93},
        ],
        "labs": [
            {"loinc": "2518-9", "name": "Lactate", "collected_dt": "2026-04-30 03:00:00",
             "value": "5.1", "units": "mmol/L", "range": "0.5-2.2", "abnormal": "high",
             "status": "final"},
            {"loinc": "6690-2", "name": "WBC", "collected_dt": "2026-04-30 03:00:00",
             "value": "24.6", "units": "K/uL", "range": "4.5-11.0", "abnormal": "high",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-30 03:00:00",
             "value": "2.1", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "high",
             "status": "final"},
        ],
    },

    # ── pt-025: Marisol Vega — Stable hypertension follow-up (P8 Active Condition Stable) ──
    # Existing P8 holders (Linda, Robert, James) moved to other tiers, so this patient
    # carries the "active condition, vitals/labs unremarkable" tier on Chen's panel.
    {
        "fname": "Marisol", "lname": "Vega", "dob": "1964-02-09", "sex": "Female",
        "admit_date": "2026-04-29", "admit_reason": "Stable essential hypertension — observation",
        "conditions": [{"title": "Essential hypertension", "icd": "I10"}],
        "allergies": [],
        "medications": [{"title": "Amlodipine 5mg PO daily"}],
        "vitals": [
            {"dt": "2026-04-29 06:00:00", "bps": 134, "bpd": 82,
             "pulse": 76, "respiration": 16, "temperature": 36.9,
             "oxygen_saturation": 98},
        ],
        "labs": [
            {"loinc": "2345-7", "name": "Glucose", "collected_dt": "2026-04-29 04:00:00",
             "value": "92", "units": "mg/dL", "range": "70-100", "abnormal": "normal",
             "status": "final"},
            {"loinc": "2160-0", "name": "Creatinine", "collected_dt": "2026-04-29 04:00:00",
             "value": "0.9", "units": "mg/dL", "range": "0.6-1.2", "abnormal": "normal",
             "status": "final"},
        ],
    },
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Fixed seed keeps any future randomized fields deterministic across runs.
    random.seed(42)

    base_url = os.environ.get("BASE_URL", "").rstrip("/")
    client_id = os.environ.get("CLIENT_ID", "")
    client_secret = os.environ.get("CLIENT_SECRET", "")
    oe_user = os.environ.get("OE_USER", "admin")
    oe_pass = os.environ.get("OE_PASS", "your-openemr-admin-password")
    mysql_host = os.environ.get("MYSQL_HOST", "shuttle.proxy.rlwy.net")
    mysql_port = int(os.environ.get("MYSQL_PORT", "12805"))
    mysql_user = os.environ.get("MYSQL_USER", "root")
    mysql_pass = os.environ.get("MYSQL_PASS", "")
    mysql_db = os.environ.get("MYSQL_DB", "openemr")

    missing = [k for k, v in {
        "BASE_URL": base_url, "CLIENT_ID": client_id,
        "CLIENT_SECRET": client_secret, "MYSQL_PASS": mysql_pass,
    }.items() if not v]
    if missing:
        print(f"Missing: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    print(f"Authenticating to {base_url} ...")
    token = get_token(base_url, client_id, client_secret, oe_user, oe_pass)
    print("Token obtained.")

    print("Connecting to MySQL ...")
    conn = mysql_conn({
        "host": mysql_host, "port": mysql_port,
        "user": mysql_user, "password": mysql_pass, "db": mysql_db,
    })
    print("MySQL connected.\n")

    # Look up the integer user_id for the authenticated user (Sara Chen).
    # This is written into form_encounter.provider_id so index.php's census
    # filter returns her panel and no others.
    # Provider lookup is distinct from the OAuth principal: the seeder may auth
    # as admin (broad API privileges) while still attributing encounters to the
    # clinician whose panel the demo expects (Sara Chen). PROVIDER_USER lets the
    # caller override this; default to "sara" with a fallback to the auth user.
    provider_username = os.environ.get("PROVIDER_USER", "sara")
    chen_user_id = 0
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s LIMIT 1", (provider_username,))
        row = cur.fetchone()
        if not row and provider_username != oe_user:
            cur.execute("SELECT id FROM users WHERE username = %s LIMIT 1", (oe_user,))
            row = cur.fetchone()
        if row:
            chen_user_id = int(row[0])
    print(f"Provider user_id for '{provider_username}': {chen_user_id}\n")

    ok = 0
    total = len(PATIENTS)
    for i, p_def in enumerate(PATIENTS, 1):
        print(f"[{i:02d}/{total}] {p_def['fname']} {p_def['lname']} ...")
        try:
            load_patient(base_url, token, conn, p_def, chen_user_id=chen_user_id)
            ok += 1
        except Exception as exc:
            print(f"  FAIL: {exc}", file=sys.stderr)
        time.sleep(0.3)  # avoid rate-limiting

    conn.close()
    print(f"\nDone: {ok}/{total} patients loaded.")
    if ok < total:
        print("Re-run load.py to retry failed patients.", file=sys.stderr)


if __name__ == "__main__":
    main()
