#!/usr/bin/env python3
"""
Wipes all synthetic patient data from the OpenEMR MySQL database.
Deletes from all affected tables in dependency order.

Required env vars: MYSQL_PASS (and optionally MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_DB)
"""
import os
import sys

try:
    import pymysql
except ImportError:
    print("pymysql not installed. Run: pip3 install pymysql", file=sys.stderr)
    sys.exit(1)

host = os.environ.get("MYSQL_HOST", "shuttle.proxy.rlwy.net")
port = int(os.environ.get("MYSQL_PORT", "12805"))
user = os.environ.get("MYSQL_USER", "root")
password = os.environ.get("MYSQL_PASS", "")
db = os.environ.get("MYSQL_DB", "openemr")

if not password:
    print("MYSQL_PASS is required", file=sys.stderr)
    sys.exit(1)

conn = pymysql.connect(host=host, port=port, user=user, password=password,
                       database=db, connect_timeout=10, charset="utf8mb4")

TABLES_IN_ORDER = [
    # Labs
    "procedure_result",
    "procedure_report",
    "procedure_order",
    # Vitals (form registry first, then data)
    "forms",
    "form_vitals",
    # Clinical data linked to patient
    "lists",           # allergies + conditions
    "prescriptions",   # medications
    # Encounters
    "form_encounter",
    "encounter_reason_codes",
    # Patient core
    "patient_data",
    "openemr_postcalendar_events",
]

with conn.cursor() as cur:
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    for table in TABLES_IN_ORDER:
        try:
            cur.execute(f"DELETE FROM `{table}`")
            print(f"  Cleared {table}: {cur.rowcount} rows")
        except Exception as e:
            print(f"  SKIP {table}: {e}")

    # Clear uuid_registry and uuid_mapping for form_vitals and patient_data only
    try:
        cur.execute("DELETE FROM uuid_mapping WHERE `table` IN ('form_vitals', 'patient_data')")
        print(f"  Cleared uuid_mapping (form_vitals + patient_data): {cur.rowcount} rows")
    except Exception as e:
        print(f"  SKIP uuid_mapping: {e}")
    try:
        cur.execute("DELETE FROM uuid_registry WHERE table_name IN ('form_vitals', 'patient_data', 'form_encounter', 'lists')")
        print(f"  Cleared uuid_registry (synthetic tables): {cur.rowcount} rows")
    except Exception as e:
        print(f"  SKIP uuid_registry: {e}")

    cur.execute("SET FOREIGN_KEY_CHECKS=1")
conn.commit()
conn.close()
print("\nWipe complete.")
