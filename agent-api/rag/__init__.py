"""Hybrid-RAG retrieval pillar (W2 §6).

Submodules (import directly):
    rag.chunker  — section-aware PDF chunker
    rag.embed    — Voyage-3 (or hash-stub) embedding wrapper
    rag.rerank   — Cohere rerank (or identity-stub) wrapper
    rag.retrieve — top-level sparse+dense+rerank pipeline
    rag.index    — bulk indexer entry point

We deliberately do NOT re-export the symbol ``embed`` at package level —
shadowing the ``rag.embed`` submodule with the function of the same name
breaks ``import rag.embed as embed_mod`` patterns in tests + tooling.
Same reasoning for ``chunker`` / ``rerank`` / ``retrieve``.
"""
