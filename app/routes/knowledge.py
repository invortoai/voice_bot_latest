"""
Knowledge Base API — CRUD for knowledge bases and document ingestion.

All routes require X-API-Key header.

Endpoints:
  POST   /knowledge-bases                          Create a knowledge base
  GET    /knowledge-bases                          List knowledge bases
  GET    /knowledge-bases/{kb_id}                  Get a knowledge base
  DELETE /knowledge-bases/{kb_id}                  Delete a knowledge base

  POST   /knowledge-bases/{kb_id}/documents        Upload a document (triggers processing)
  GET    /knowledge-bases/{kb_id}/documents        List documents
  DELETE /knowledge-bases/{kb_id}/documents/{id}   Delete a document

  POST   /knowledge-bases/{kb_id}/search           Test RAG search
"""

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.auth import require_api_key
from app.services import knowledge_service

router = APIRouter(
    prefix="/knowledge-bases",
    tags=["Knowledge Base"],
    dependencies=[require_api_key],
)


# ── Request / Response Models ─────────────────────────────────────────────────


class CreateKnowledgeBaseRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    chunk_size: int = Field(500, ge=100, le=2000)
    chunk_overlap: int = Field(50, ge=0, le=200)


class CreateDocumentRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    content: str = Field(..., min_length=1)
    source_type: str = Field("text", pattern="^(text|pdf|url)$")
    source_url: Optional[str] = None
    metadata: Optional[dict] = None


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    top_k: int = Field(5, ge=1, le=20)
    score_threshold: float = Field(0.35, ge=0.0, le=1.0)


# ── Knowledge Base Endpoints ──────────────────────────────────────────────────


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(request: Request, body: CreateKnowledgeBaseRequest):
    org_id = request.headers.get("X-Org-Id")
    kb = knowledge_service.create_knowledge_base(
        name=body.name,
        org_id=org_id,
        description=body.description,
        chunk_size=body.chunk_size,
        chunk_overlap=body.chunk_overlap,
    )
    return kb


@router.get("")
async def list_knowledge_bases(request: Request):
    org_id = request.headers.get("X-Org-Id")
    return knowledge_service.list_knowledge_bases(org_id=org_id)


@router.get("/{kb_id}")
async def get_knowledge_base(kb_id: str, request: Request):
    org_id = request.headers.get("X-Org-Id")
    kb = knowledge_service.get_knowledge_base(kb_id, org_id=org_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return kb


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(kb_id: str, request: Request):
    org_id = request.headers.get("X-Org-Id")
    deleted = knowledge_service.delete_knowledge_base(kb_id, org_id=org_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Knowledge base not found")


# ── Document Endpoints ────────────────────────────────────────────────────────


@router.post("/{kb_id}/documents", status_code=status.HTTP_201_CREATED)
async def create_document(
    kb_id: str,
    body: CreateDocumentRequest,
    background_tasks: BackgroundTasks,
    request: Request,
):
    """
    Upload a document to a knowledge base.
    Document is saved immediately (status=pending), then chunked and embedded
    in the background. Poll GET /documents to check when status=ready.
    """
    org_id = request.headers.get("X-Org-Id")

    # Verify KB exists
    kb = knowledge_service.get_knowledge_base(kb_id, org_id=org_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    doc = knowledge_service.create_document(
        kb_id=kb_id,
        title=body.title,
        content=body.content,
        org_id=org_id,
        source_type=body.source_type,
        source_url=body.source_url,
        metadata=body.metadata,
    )

    # Process in background (chunk + embed)
    background_tasks.add_task(knowledge_service.process_document, str(doc["id"]))

    return doc


@router.get("/{kb_id}/documents")
async def list_documents(kb_id: str, request: Request):
    org_id = request.headers.get("X-Org-Id")
    kb = knowledge_service.get_knowledge_base(kb_id, org_id=org_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return knowledge_service.list_documents(kb_id, org_id=org_id)


@router.delete("/{kb_id}/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(kb_id: str, doc_id: str, request: Request):
    org_id = request.headers.get("X-Org-Id")
    deleted = knowledge_service.delete_document(doc_id, org_id=org_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")


# ── Search (for testing) ──────────────────────────────────────────────────────


@router.post("/{kb_id}/search")
async def search_knowledge_base(kb_id: str, body: SearchRequest, request: Request):
    """
    Test RAG search against a knowledge base.
    Returns top-K chunks with similarity scores.
    """
    org_id = request.headers.get("X-Org-Id")
    kb = knowledge_service.get_knowledge_base(kb_id, org_id=org_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found")

    chunks = await knowledge_service.search_knowledge(
        kb_id=kb_id,
        query=body.query,
        top_k=body.top_k,
        score_threshold=body.score_threshold,
    )
    return {"query": body.query, "results": chunks, "count": len(chunks)}
