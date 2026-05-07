-- ── Knowledge Base RAG Migration ─────────────────────────────────────────────
-- Enables pgvector, adds knowledge_bases, knowledge_documents, knowledge_chunks
-- and links assistants to a knowledge base.

CREATE EXTENSION IF NOT EXISTS vector;

-- ── knowledge_bases ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    description         TEXT,
    embedding_model     TEXT NOT NULL DEFAULT 'text-embedding-3-small',
    embedding_dimensions INT NOT NULL DEFAULT 1536,
    chunk_size          INT NOT NULL DEFAULT 500,
    chunk_overlap       INT NOT NULL DEFAULT 50,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_knowledge_bases_org ON knowledge_bases(org_id);

-- ── knowledge_documents ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id       UUID REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    org_id      UUID REFERENCES organizations(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'text',   -- text | pdf | url
    source_url  TEXT,
    content     TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending', -- pending | processing | ready | failed
    error       TEXT,
    chunk_count INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_kb ON knowledge_documents(kb_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_documents_status ON knowledge_documents(status);

-- ── knowledge_chunks ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id      UUID REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    kb_id       UUID REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    org_id      UUID REFERENCES organizations(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content     TEXT NOT NULL,
    embedding   vector(1536),
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- IVFFlat index for fast cosine similarity search
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_embedding
    ON knowledge_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_kb ON knowledge_chunks(kb_id);

-- ── Link assistants to a knowledge base ──────────────────────────────────────
ALTER TABLE assistants
    ADD COLUMN IF NOT EXISTS knowledge_base_id UUID REFERENCES knowledge_bases(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS rag_top_k         INT NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS rag_score_threshold FLOAT NOT NULL DEFAULT 0.35,
    ADD COLUMN IF NOT EXISTS rag_context_query TEXT;

-- ── Helper: search chunks by cosine similarity ────────────────────────────────
CREATE OR REPLACE FUNCTION search_knowledge_chunks(
    p_kb_id       UUID,
    p_embedding   vector(1536),
    p_top_k       INT DEFAULT 5,
    p_threshold   FLOAT DEFAULT 0.35
)
RETURNS TABLE (
    id          UUID,
    doc_id      UUID,
    content     TEXT,
    similarity  FLOAT,
    metadata    JSONB
)
LANGUAGE sql STABLE AS $$
    SELECT
        kc.id,
        kc.doc_id,
        kc.content,
        1 - (kc.embedding <=> p_embedding) AS similarity,
        kc.metadata
    FROM knowledge_chunks kc
    WHERE kc.kb_id = p_kb_id
      AND kc.embedding IS NOT NULL
      AND 1 - (kc.embedding <=> p_embedding) >= p_threshold
    ORDER BY kc.embedding <=> p_embedding
    LIMIT p_top_k;
$$;
