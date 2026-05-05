"""Indexer entry point: read corpus, chunk, embed, UPSERT into Postgres.

Run via:
    python3 -m rag.index agent-api/corpus/

Idempotent: chunk_id is deterministic (``f"{source_id}-{page}-{idx:03d}"``)
so re-running re-upserts content + embedding without duplicating rows.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from audit.writer import get_pool
from rag.chunker import Chunk, chunk_guideline_pdf
from rag.embed import embed

_logger = logging.getLogger(__name__)


_UPSERT_SQL = """
    INSERT INTO copilot_guideline_chunks
        (chunk_id, source_id, document_title, section, page_number,
         indexed_version_date, content, embedding)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector)
    ON CONFLICT (chunk_id) DO UPDATE SET
        source_id            = EXCLUDED.source_id,
        document_title       = EXCLUDED.document_title,
        section              = EXCLUDED.section,
        page_number          = EXCLUDED.page_number,
        indexed_version_date = EXCLUDED.indexed_version_date,
        content              = EXCLUDED.content,
        embedding            = EXCLUDED.embedding,
        indexed_at           = NOW()
"""


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _load_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    with manifest_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    sources = data.get("sources") or []
    if not isinstance(sources, list):
        raise ValueError(f"manifest 'sources' must be a list, got {type(sources)}")
    return sources


async def _index_one_source(
    *,
    pool: Any,
    source: dict[str, Any],
    corpus_dir: Path,
) -> int:
    source_id = str(source["id"])
    title = str(source["title"])
    version_date = source["indexed_version_date"]
    if isinstance(version_date, str):
        version_date = _dt.date.fromisoformat(version_date)
    pdf_path = corpus_dir / source["file"]

    if not pdf_path.exists():
        _logger.warning(
            "rag_index_source_missing",
            extra={"source_id": source_id, "path": str(pdf_path)},
        )
        return 0

    pdf_bytes = pdf_path.read_bytes()
    chunks: list[Chunk] = chunk_guideline_pdf(pdf_bytes)
    if not chunks:
        return 0

    contents = [c.content for c in chunks]
    embeddings = await embed(contents)
    if len(embeddings) != len(chunks):
        # Defensive: pad with zeros so the UPSERT still runs.
        while len(embeddings) < len(chunks):
            embeddings.append([0.0] * 1024)

    # chunk_id: source_id-page-idx — page-and-index keep idempotency stable
    # across re-runs even when chunker output drifts (we rebuild the same
    # rows by chunk_id and overwrite content + embedding).
    rows_for_insert: list[tuple[Any, ...]] = []
    per_page_idx: dict[int, int] = {}
    for chunk, vec in zip(chunks, embeddings):
        idx = per_page_idx.get(chunk.page_number, 0)
        per_page_idx[chunk.page_number] = idx + 1
        chunk_id = f"{source_id}-p{chunk.page_number}-{idx:03d}"
        rows_for_insert.append(
            (
                chunk_id,
                source_id,
                title,
                chunk.section,
                chunk.page_number,
                version_date,
                chunk.content,
                _vector_literal(vec),
            )
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(_UPSERT_SQL, rows_for_insert)

    _logger.info(
        "rag_index_source_complete",
        extra={
            "source_id": source_id,
            "chunks_indexed": len(rows_for_insert),
            "pages_seen": len(per_page_idx),
        },
    )
    return len(rows_for_insert)


async def index_corpus(
    corpus_dir: Path,
    *,
    manifest_path: Path | None = None,
) -> int:
    """Index every source in the manifest. Returns total chunks indexed."""
    corpus_dir = Path(corpus_dir).resolve()
    if manifest_path is None:
        manifest_path = corpus_dir / "manifest.yaml"
    sources = _load_manifest(manifest_path)

    pool = await get_pool()
    if pool is None:
        raise RuntimeError(
            "rag.index requires audit_db_url to be configured "
            "(no Postgres pool available)"
        )

    total = 0
    for source in sources:
        total += await _index_one_source(
            pool=pool, source=source, corpus_dir=corpus_dir
        )
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Index the guideline corpus.")
    parser.add_argument(
        "corpus_dir",
        nargs="?",
        default="corpus/",
        help="Directory containing manifest.yaml and the PDFs.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Override manifest path (defaults to <corpus_dir>/manifest.yaml).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    corpus_dir = Path(args.corpus_dir)
    manifest_path = Path(args.manifest) if args.manifest else None
    try:
        total = asyncio.run(index_corpus(corpus_dir, manifest_path=manifest_path))
    except Exception as exc:
        print(f"index_corpus failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Indexed {total} chunks from {corpus_dir}")


if __name__ == "__main__":
    main()


__all__ = ["index_corpus", "main"]
