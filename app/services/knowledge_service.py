"""
Knowledge Base service — document ingestion, chunking, embedding, and RAG search.

Flow:
  1. Create a knowledge base (POST /knowledge-bases)
  2. Upload documents (POST /knowledge-bases/{id}/documents)
     → text is chunked → each chunk is embedded via OpenAI → stored in pgvector
  3. At call start, runner calls search_knowledge() with the assistant's
     rag_context_query → top-K chunks are injected into the system prompt.
"""

from __future__ import annotations

import asyncio
import textwrap
from typing import Optional
from uuid import UUID

import httpx
from loguru import logger

from app.config import OPENAI_API_KEY
from app.core.database import get_cursor

# ── Embedding ─────────────────────────────────────────────────────────────────

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_BATCH_SIZE = 100  # OpenAI limit per request


async def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Call OpenAI embeddings API; returns one vector per input text."""
    if not texts:
        return []

    results: list[list[float]] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[i : i + EMBEDDING_BATCH_SIZE]
            resp = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={"model": EMBEDDING_MODEL, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            # Sort by index to preserve order
            sorted_data = sorted(data["data"], key=lambda x: x["index"])
            results.extend([item["embedding"] for item in sorted_data])

    return results


# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """
    Split text into overlapping chunks by word count.
    Each chunk is ~chunk_size words with overlap words of context from previous chunk.
    """
    words = text.split()
    if not words:
        return []

    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        if end == len(words):
            break
        start += chunk_size - overlap

    return chunks


# ── Knowledge Base CRUD ───────────────────────────────────────────────────────

def create_knowledge_base(
    name: str,
    org_id: Optional[str] = None,
    description: Optional[str] = None,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> dict:
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO knowledge_bases (org_id, name, description, chunk_size, chunk_overlap)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (org_id, name, description, chunk_size, chunk_overlap),
        )
        return dict(cur.fetchone())


def get_knowledge_base(kb_id: str, org_id: Optional[str] = None) -> Optional[dict]:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "SELECT * FROM knowledge_bases WHERE id = %s AND org_id = %s",
                (kb_id, org_id),
            )
        else:
            cur.execute("SELECT * FROM knowledge_bases WHERE id = %s", (kb_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_knowledge_bases(org_id: Optional[str] = None) -> list[dict]:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "SELECT * FROM knowledge_bases WHERE org_id = %s AND is_active = true ORDER BY name",
                (org_id,),
            )
        else:
            cur.execute(
                "SELECT * FROM knowledge_bases WHERE is_active = true ORDER BY name"
            )
        return [dict(r) for r in cur.fetchall()]


def delete_knowledge_base(kb_id: str, org_id: Optional[str] = None) -> bool:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "DELETE FROM knowledge_bases WHERE id = %s AND org_id = %s RETURNING id",
                (kb_id, org_id),
            )
        else:
            cur.execute(
                "DELETE FROM knowledge_bases WHERE id = %s RETURNING id", (kb_id,)
            )
        return cur.fetchone() is not None


# ── Document CRUD ─────────────────────────────────────────────────────────────

def create_document(
    kb_id: str,
    title: str,
    content: str,
    org_id: Optional[str] = None,
    source_type: str = "text",
    source_url: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    from psycopg2.extras import Json
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO knowledge_documents
                (kb_id, org_id, title, content, source_type, source_url, metadata, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING *
            """,
            (
                kb_id,
                org_id,
                title,
                content,
                source_type,
                source_url,
                Json(metadata or {}),
            ),
        )
        return dict(cur.fetchone())


def list_documents(kb_id: str, org_id: Optional[str] = None) -> list[dict]:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "SELECT id, kb_id, org_id, title, source_type, status, chunk_count, created_at, updated_at "
                "FROM knowledge_documents WHERE kb_id = %s AND org_id = %s ORDER BY created_at DESC",
                (kb_id, org_id),
            )
        else:
            cur.execute(
                "SELECT id, kb_id, org_id, title, source_type, status, chunk_count, created_at, updated_at "
                "FROM knowledge_documents WHERE kb_id = %s ORDER BY created_at DESC",
                (kb_id,),
            )
        return [dict(r) for r in cur.fetchall()]


def delete_document(doc_id: str, org_id: Optional[str] = None) -> bool:
    with get_cursor() as cur:
        if org_id:
            cur.execute(
                "DELETE FROM knowledge_documents WHERE id = %s AND org_id = %s RETURNING id",
                (doc_id, org_id),
            )
        else:
            cur.execute(
                "DELETE FROM knowledge_documents WHERE id = %s RETURNING id", (doc_id,)
            )
        return cur.fetchone() is not None


# ── Document Processing (chunking + embedding) ────────────────────────────────

async def process_document(doc_id: str) -> None:
    """
    Chunk a document and generate + store embeddings.
    Called as a background task after document creation.
    """
    # 1. Load document
    with get_cursor() as cur:
        cur.execute(
            "SELECT kd.*, kb.chunk_size, kb.chunk_overlap, kb.org_id as kb_org_id "
            "FROM knowledge_documents kd "
            "JOIN knowledge_bases kb ON kb.id = kd.kb_id "
            "WHERE kd.id = %s",
            (doc_id,),
        )
        row = cur.fetchone()

    if not row:
        logger.error(f"knowledge: document {doc_id} not found for processing")
        return

    doc = dict(row)
    kb_id = str(doc["kb_id"])
    org_id = str(doc["org_id"] or doc.get("kb_org_id") or "")

    # Mark as processing
    with get_cursor() as cur:
        cur.execute(
            "UPDATE knowledge_documents SET status = 'processing', updated_at = NOW() WHERE id = %s",
            (doc_id,),
        )

    try:
        # 2. Chunk
        chunks = _chunk_text(
            doc["content"],
            chunk_size=doc.get("chunk_size", 500),
            overlap=doc.get("chunk_overlap", 50),
        )
        if not chunks:
            raise ValueError("Document produced no chunks after splitting")

        logger.info(f"knowledge: doc={doc_id} → {len(chunks)} chunks, embedding...")

        # 3. Embed all chunks
        embeddings = await _embed_texts(chunks)

        # 4. Delete old chunks (re-processing case)
        with get_cursor() as cur:
            cur.execute("DELETE FROM knowledge_chunks WHERE doc_id = %s", (doc_id,))

        # 5. Insert new chunks with embeddings
        from psycopg2.extras import Json, execute_values
        rows = [
            (
                doc_id,
                kb_id,
                org_id or None,
                idx,
                chunk,
                f"[{','.join(str(v) for v in emb)}]",  # pgvector literal
                Json({}),
            )
            for idx, (chunk, emb) in enumerate(zip(chunks, embeddings))
        ]

        with get_cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO knowledge_chunks
                    (doc_id, kb_id, org_id, chunk_index, content, embedding, metadata)
                VALUES %s
                """,
                rows,
                template="(%s, %s, %s, %s, %s, %s::vector, %s)",
            )
            # Update document status + chunk count
            cur.execute(
                """
                UPDATE knowledge_documents
                SET status = 'ready', chunk_count = %s, error = NULL, updated_at = NOW()
                WHERE id = %s
                """,
                (len(chunks), doc_id),
            )

        logger.info(
            f"knowledge: doc={doc_id} processed successfully ({len(chunks)} chunks embedded)"
        )

    except Exception as e:
        logger.error(f"knowledge: doc={doc_id} processing failed: {e}")
        with get_cursor() as cur:
            cur.execute(
                "UPDATE knowledge_documents SET status = 'failed', error = %s, updated_at = NOW() WHERE id = %s",
                (str(e), doc_id),
            )


# ── RAG Search ────────────────────────────────────────────────────────────────

async def search_knowledge(
    kb_id: str,
    query: str,
    top_k: int = 5,
    score_threshold: float = 0.35,
) -> list[dict]:
    """
    Embed query and return top-K most similar chunks above score_threshold.
    Returns list of {content, similarity, metadata}.
    """
    if not query or not kb_id:
        return []

    try:
        embeddings = await _embed_texts([query])
        if not embeddings:
            return []
        query_vec = embeddings[0]
        vec_literal = f"[{','.join(str(v) for v in query_vec)}]"

        with get_cursor() as cur:
            cur.execute(
                "SELECT * FROM search_knowledge_chunks(%s, %s::vector, %s, %s)",
                (kb_id, vec_literal, top_k, score_threshold),
            )
            rows = cur.fetchall()

        return [dict(r) for r in rows]

    except Exception as e:
        logger.error(f"knowledge: search failed for kb={kb_id}: {e}")
        return []


async def build_rag_context(
    kb_id: str,
    query: str,
    top_k: int = 5,
    score_threshold: float = 0.35,
) -> str:
    """
    Search knowledge base and format results as a context block
    to be appended to the system prompt.
    Returns empty string if no relevant chunks found.
    """
    chunks = await search_knowledge(kb_id, query, top_k, score_threshold)
    if not chunks:
        return ""

    lines = ["--- KNOWLEDGE BASE ---"]
    for i, chunk in enumerate(chunks, 1):
        lines.append(f"[{i}] {chunk['content'].strip()}")
    lines.append("--- END KNOWLEDGE BASE ---")

    context = "\n\n".join(lines)
    logger.info(
        f"knowledge: RAG context built — kb={kb_id}, query={query!r}, "
        f"chunks={len(chunks)}, top_score={chunks[0]['similarity']:.3f}"
    )
    return context
