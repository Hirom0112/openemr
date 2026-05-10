#!/usr/bin/env python3
"""Seed a generic Patient Photograph for every patient lacking one.

Why: OpenEMR's `controller.php?document&retrieve&context=patient_picture`
calls `getPatientPictureDocumentId($pid)`. When the patient has no photograph
that returns null/0, then `new Document(null)` plus `$d->get_url()` throws
and the UI shows "An error has occurred." Seeding a placeholder document for
every pid eliminates the 500.

Idempotent: re-running skips any pid already linked to a Patient Photograph.

Usage:
  Local (default — talks to docker mariadb via `docker exec`):
      python3 scripts/seed_patient_photographs.py

  Railway / direct DB connection (set env vars, then run with --mode direct):
      DB_HOST=... DB_PORT=3306 DB_USER=... DB_PASS=... DB_NAME=openemr \
      DOCS_ROOT=/var/www/localhost/htdocs/openemr/sites/default/documents \
      python3 scripts/seed_patient_photographs.py --mode direct

  One-liner for Railway (executed inside the openemr container):
      railway run --service openemr -- python3 scripts/seed_patient_photographs.py --mode direct

Note: writes file bytes via `docker cp` (local mode) or directly to
`DOCS_ROOT` (direct mode). Inserts `documents` + `categories_to_documents`
rows. Files are stored unencrypted (encrypted=0) so OpenEMR's retrieve path
serves them without needing the site's encryption key.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ASSET_PATH = Path(__file__).resolve().parent / "assets" / "patient_silhouette.jpg"
PHOTO_CATEGORY_NAME = "Patient Photograph"
DEFAULT_DOCKER_DB = "development-easy-mysql-1"
DEFAULT_DOCKER_APP = "development-easy-openemr-1"
DEFAULT_DOCS_ROOT_IN_CONTAINER = (
    "/var/www/localhost/htdocs/openemr/sites/default/documents"
)


def run(cmd: list[str], *, input_bytes: bytes | None = None, check: bool = True) -> str:
    res = subprocess.run(
        cmd, input=input_bytes, capture_output=True, check=False
    )
    if check and res.returncode != 0:
        sys.stderr.write(res.stderr.decode("utf-8", "replace"))
        raise SystemExit(f"command failed: {' '.join(cmd)}")
    return res.stdout.decode("utf-8", "replace")


# ---------- DB drivers ----------------------------------------------------


class DockerMariaDB:
    """Run SQL via `docker exec ... mariadb -uroot -proot openemr`."""

    def __init__(self, container: str) -> None:
        self.container = container

    def query(self, sql: str) -> list[list[str]]:
        out = run([
            "docker", "exec", self.container,
            "mariadb", "-uroot", "-proot", "openemr",
            "--batch", "--skip-column-names", "-e", sql,
        ])
        return [line.split("\t") for line in out.strip().splitlines() if line]

    def execute(self, sql: str) -> None:
        run([
            "docker", "exec", "-i", self.container,
            "mariadb", "-uroot", "-proot", "openemr", "-e", sql,
        ])


class DirectMariaDB:
    """Direct connection via PyMySQL — for Railway / non-docker runs."""

    def __init__(self) -> None:
        try:
            import pymysql  # type: ignore
        except ImportError as e:
            raise SystemExit(
                "direct mode requires PyMySQL (pip install pymysql)"
            ) from e

        self.conn = pymysql.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASS"],
            database=os.environ.get("DB_NAME", "openemr"),
            autocommit=True,
        )

    def query(self, sql: str) -> list[list[str]]:
        with self.conn.cursor() as c:
            c.execute(sql)
            rows = c.fetchall()
        return [[("" if v is None else str(v)) for v in row] for row in rows]

    def execute(self, sql: str) -> None:
        with self.conn.cursor() as c:
            c.execute(sql)


# ---------- File placement -------------------------------------------------


class DockerFs:
    def __init__(self, container: str, docs_root: str) -> None:
        self.container = container
        self.docs_root = docs_root

    def write(self, pid: int, fname: str, data: bytes) -> str:
        target_dir = f"{self.docs_root}/{pid}"
        run(["docker", "exec", self.container, "mkdir", "-p", target_dir])
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tf:
            tf.write(data)
            host_tmp = tf.name
        try:
            run([
                "docker", "cp", host_tmp,
                f"{self.container}:{target_dir}/{fname}",
            ])
            run([
                "docker", "exec", self.container, "chown",
                "apache:apache", f"{target_dir}/{fname}",
            ])
        finally:
            os.unlink(host_tmp)
        return f"{target_dir}/{fname}"


class DirectFs:
    def __init__(self, docs_root: str) -> None:
        self.docs_root = docs_root

    def write(self, pid: int, fname: str, data: bytes) -> str:
        d = Path(self.docs_root) / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        p = d / fname
        p.write_bytes(data)
        return str(p)


# ---------- Seeding logic --------------------------------------------------


def seed(db, fs, docs_root: str) -> None:
    cat_rows = db.query(
        f"SELECT id FROM categories WHERE name = '{PHOTO_CATEGORY_NAME}' LIMIT 1;"
    )
    if not cat_rows:
        raise SystemExit(f"category '{PHOTO_CATEGORY_NAME}' not found")
    category_id = int(cat_rows[0][0])

    pid_rows = db.query("SELECT pid FROM patient_data ORDER BY pid;")
    all_pids = [int(r[0]) for r in pid_rows]

    have_rows = db.query(
        f"""
        SELECT DISTINCT d.foreign_id
          FROM documents d
          JOIN categories_to_documents c2d ON c2d.document_id = d.id
         WHERE c2d.category_id = {category_id}
           AND d.deleted = 0
        """
    )
    have = {int(r[0]) for r in have_rows if r[0]}

    todo = [p for p in all_pids if p not in have]
    print(f"[seed] category_id={category_id} total_pids={len(all_pids)} "
          f"already_have={len(have)} todo={len(todo)}")
    if not todo:
        print("[seed] nothing to do")
        return

    img_bytes = ASSET_PATH.read_bytes()
    img_size = len(img_bytes)
    img_hash = hashlib.sha512(img_bytes).hexdigest()

    # `documents.id` has no AUTO_INCREMENT — allocate manually.
    max_rows = db.query("SELECT COALESCE(MAX(id), 0) FROM documents;")
    next_id = int(max_rows[0][0]) + 1

    for pid in todo:
        file_uuid = str(uuid.uuid4())
        on_disk = fs.write(pid, file_uuid, img_bytes)
        # `url` must use the in-container path (legacy convention).
        in_container_url = f"file://{docs_root}/{pid}/{file_uuid}"
        doc_id = next_id
        next_id += 1
        db.execute(
            f"""
            INSERT INTO documents
                (id, uuid, type, size, date, url, mimetype,
                 foreign_id, docdate, hash, list_id, storagemethod,
                 path_depth, imported, encounter_id, encrypted,
                 name, deleted)
            VALUES
                ({doc_id}, UNHEX(REPLACE(UUID(),'-','')), 'file_url',
                 {img_size}, NOW(),
                 '{in_container_url}', 'image/jpeg',
                 {pid}, CURDATE(), '{img_hash}', 0, 0,
                 1, 0, 0, 0,
                 'patient_photograph.jpg', 0)
            """
        )
        db.execute(
            f"INSERT INTO categories_to_documents (category_id, document_id) "
            f"VALUES ({category_id}, {doc_id});"
        )
        print(f"[seed] pid={pid} doc_id={doc_id} file={on_disk}")

    print(f"[seed] done — seeded {len(todo)} patients")


# ---------- Entry ---------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode", choices=("local", "direct"), default="local",
        help="local = docker exec into dev containers; direct = use DB_* env vars",
    )
    ap.add_argument(
        "--db-container", default=DEFAULT_DOCKER_DB,
        help="(local mode) mariadb container name",
    )
    ap.add_argument(
        "--app-container", default=DEFAULT_DOCKER_APP,
        help="(local mode) openemr container name (for file writes)",
    )
    ap.add_argument(
        "--docs-root",
        default=os.environ.get("DOCS_ROOT", DEFAULT_DOCS_ROOT_IN_CONTAINER),
        help="absolute documents/ path *as seen by the openemr PHP runtime*",
    )
    args = ap.parse_args()

    if not ASSET_PATH.exists():
        raise SystemExit(f"missing asset: {ASSET_PATH}")

    if args.mode == "local":
        if not shutil.which("docker"):
            raise SystemExit("docker CLI not found")
        db = DockerMariaDB(args.db_container)
        fs = DockerFs(args.app_container, args.docs_root)
    else:
        db = DirectMariaDB()
        fs = DirectFs(args.docs_root)

    seed(db, fs, args.docs_root)


if __name__ == "__main__":
    main()
