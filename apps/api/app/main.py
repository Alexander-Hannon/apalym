from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi import UploadFile, File
from pathlib import Path
from pypdf import PdfReader
from google import genai
from pinecone.grpc import PineconeGRPC as Pinecone
from typing import Optional, Any
from google.genai import errors as genai_errors

import json
import time
import uuid
import os
import httpx
import math


#---------------------------------------------
# HELPERS

def log_json(**fields):
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False))


STORAGE_DIR = Path("/app/storage")

def get_tenant_id(request: Request) -> str:
    # For now: hardcoded. Later: from auth / workspace selection.
    tenant_id = "demo-tenant"
    request.state.tenant_id = tenant_id
    return tenant_id


def get_doc_path(tenant_id: str, doc_id: str) -> Path:
    return STORAGE_DIR / tenant_id / f"{doc_id}.pdf"


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100):

    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if overlap < 0:
        raise ValueError("overlap must be >= 0")
    if overlap >= chunk_size:
        raise ValueError("overlap must be < chunk_size")

    chunks = []
    n = len(text)
    start = 0
    idx = 0

    while start < n:
        end = min(start + chunk_size, n)
        chunk_str = text[start:end]

        # skip empty/whitespace chunks
        if chunk_str.strip():
            chunks.append({
                "chunk_id": str(idx),
                "start": start,
                "end": end,
                "text": chunk_str,
            })
            idx += 1

        # move start forward with overlap
        next_start = end - overlap

        # Guarantee progress to avoid infinite loops
        if next_start <= start:
            next_start = end  # drop overlap if overlap would stall

        start = next_start

        # safety: if overlap math causes no progress, break
        if start < 0:
            start = 0
        if start >= n:
            break

    return chunks

def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def get_gemini_client() -> genai.Client:
    # google-genai can read GEMINI_API_KEY from env automatically,
    # but we’ll validate it exists so failures are obvious.
    if not os.getenv("GEMINI_API_KEY"):
        raise RuntimeError("Missing GEMINI_API_KEY")
    return genai.Client()

def get_pinecone_index():
    api_key = os.getenv("PINECONE_API_KEY")
    host = os.getenv("PINECONE_HOST")
    if not api_key:
        raise RuntimeError("Missing PINECONE_API_KEY")
    if not host:
        raise RuntimeError("Missing PINECONE_HOST")

    pc = Pinecone(api_key=api_key)
    return pc.Index(host=host)

def retrieve_matches(
    tenant_id: str,
    query_text: str,
    top_k: int,
    doc_id: Optional[str] = None,
    request_id: Optional[str] = None,
):
    embed_dim = 768
    embed_model = "gemini-embedding-001"

    gemini = get_gemini_client()
    result = gemini.models.embed_content(
        model=embed_model,
        contents=[query_text],
        config={"output_dimensionality": embed_dim},
    )

    emb0 = result.embeddings[0]
    qvec = getattr(emb0, "values", None)
    if qvec is None and isinstance(emb0, dict):
        qvec = emb0.get("values")
    if qvec is None:
        raise RuntimeError(f"Unexpected embedding shape: {type(emb0)}")

    qvec = l2_normalize(qvec)

    index = get_pinecone_index()

    pinecone_filter = None
    if doc_id:
        pinecone_filter = {"doc_id": {"$eq": doc_id}}

    res = index.query(
        vector=qvec,
        top_k=top_k,
        namespace=tenant_id,
        include_metadata=True,
        filter=pinecone_filter,
    )

    raw_matches = getattr(res, "matches", None) or res.get("matches", [])
    matches = []
    retrieved_doc_ids: set[str] = set()
    for m in raw_matches:
        mid = getattr(m, "id", None) or m.get("id")
        score = getattr(m, "score", None) or m.get("score")
        md = getattr(m, "metadata", None) or m.get("metadata", {}) or {}

        if md.get("doc_id"):
            retrieved_doc_ids.add(md["doc_id"])

        citation = {
            "source_id": mid,
            "doc_id": md.get("doc_id"),
            "chunk_id": md.get("chunk_id"),
            "start": md.get("start"),
            "end": md.get("end"),
            "score": score,
        }

        matches.append({
            "id": mid,
            "score": score,
            "text": md.get("text", ""),
            "metadata": {
                "doc_id": md.get("doc_id"),
                "chunk_id": md.get("chunk_id"),
                "start": md.get("start"),
                "end": md.get("end"),
            },
            "citation": citation,
        })

    log_json(
        level="INFO",
        event="retrieval",
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        embedding_model=embed_model,
        embed_dim=embed_dim,
        top_k=top_k,
        retrieved_k=len(matches),
        retrieved_doc_ids=sorted(retrieved_doc_ids) if retrieved_doc_ids else None,
        doc_id_filter=doc_id,
    )

    return matches



#----------------------------------------
# CLASSES

AI_BASE_URL = os.getenv("AI_BASE_URL", "http://ai:8001")

class DemoRequest(BaseModel):
    message: str

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(5, ge=1, le=20)
    doc_id: Optional[str] = None  # optional filter to a specific doc

class AskRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(5, ge=1, le=10)
    doc_id: Optional[str] = None  # optional: restrict to a document


#---------------------------------------------
# ENDPOINTS


app = FastAPI(title="Apalym API")

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start = time.time()

    # Store on request.state so handlers can access it
    request.state.request_id = request_id
    request.state.tenant_id = None
    request.state.user_id = None

    try:
        response: Response = await call_next(request)
        response.headers["x-request-id"] = request_id
        status_code = response.status_code
        return response
    finally:
        latency_ms = int((time.time() - start) * 1000)

        route_pattern = None
        route_obj = request.scope.get("route")
        if route_obj is not None:
            route_pattern = getattr(route_obj, "path", None)

        log_json(
            level="INFO",
            event="request_complete",
            service="api",
            request_id=request_id,
            tenant_id=getattr(request.state, "tenant_id", None),
            user_id=getattr(request.state, "user_id", None),
            route=route_pattern,
            method=request.method,
            path=request.url.path,
            status_code=locals().get("status_code", 500),
            latency_ms=latency_ms,
        )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)





@app.get("/health")
def health():
    return {"ok": True, "service": "api"}


@app.post("/v1/demo")
async def demo(req: DemoRequest, request: Request):
     
    request_id = request.state.request_id
    tenant_id = "demo-tenant"
    request.state.tenant_id = tenant_id

    payload = {"message": req.message}

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
                f"{AI_BASE_URL}/infer", 
                json=payload, 
                headers={
                    "x-request-id": request_id,
                    "x-tenant-id": tenant_id,
                    } ,
        )

        r.raise_for_status()
        ai_data = r.json()

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "sent_to_ai": payload,
        "ai_response": ai_data,

    }



@app.post("/v1/docs/upload")
async def upload_doc(request: Request, file: UploadFile = File(...)):
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)

    # Basic content-type check (not perfect, but good for MVP)
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported right now")

    doc_id = str(uuid.uuid4())
    tenant_dir = STORAGE_DIR / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)

    dest_path = tenant_dir / f"{doc_id}.pdf"

    # Stream file to disk (safe for larger files)
    size_bytes = 0
    with dest_path.open("wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)  # 1MB
            if not chunk:
                break
            size_bytes += len(chunk)
            f.write(chunk)

    log_json(
        level="INFO",
        event="doc_uploaded",
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=None,
        doc_id=doc_id,
        filename=file.filename,
        size_bytes=size_bytes,
    )

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "doc_id": doc_id,
        "filename": file.filename,
        "size_bytes": size_bytes,
    }



@app.get("/v1/docs/{doc_id}/text")
def get_doc_text(doc_id: str, request: Request):
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)

    pdf_path = get_doc_path(tenant_id, doc_id)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")

    text = "\n".join(parts)
    preview = text[:1000]

    log_json(
        level="INFO",
        event="doc_text_extracted",
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=None,
        doc_id=doc_id,
        chars=len(text),
    )

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "doc_id": doc_id,
        "chars": len(text),
        "text_preview": preview,
    }


@app.get("/v1/docs/{doc_id}/chunks")
def get_doc_chunks(
    doc_id: str,
    request: Request,
    chunk_size: int = 800,
    overlap: int = 100,
    limit: int = 200,
):
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)

    pdf_path = get_doc_path(tenant_id, doc_id)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    # Extract text (same pattern as /text)
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts)

    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)

    # Limit chunk count for safety (avoid huge payloads)
    chunks = chunks[:max(0, limit)]

    log_json(
        level="INFO",
        event="doc_chunked",
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=None,
        doc_id=doc_id,
        chars=len(text),
        chunk_size=chunk_size,
        overlap=overlap,
        num_chunks=len(chunks),
    )

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "doc_id": doc_id,
        "chunk_size": chunk_size,
        "overlap": overlap,
        "num_chunks": len(chunks),
        "chunks": chunks,
    }


@app.post("/v1/docs/{doc_id}/index")
def index_document(
    doc_id: str,
    request: Request,
    chunk_size: int = 800,
    overlap: int = 100,
    limit: int = 500,
    batch_size: int = 50,
    embed_dim: int = 768,
):
    """
    Index a document:
    - extract text from stored PDF
    - chunk with overlap
    - embed chunks with Gemini
    - upsert vectors into Pinecone under tenant namespace
    """
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)

    # 1) Load PDF
    pdf_path = get_doc_path(tenant_id, doc_id)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    # 2) Extract text (same approach as /text)
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts).strip()

    if not text:
        return {
            "ok": True,
            "service": "api",
            "request_id": request_id,
            "tenant_id": tenant_id,
            "doc_id": doc_id,
            "num_chunks": 0,
            "upserted_count": 0,
        }

    # 3) Chunk
    chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)[:max(0, limit)]
    chunk_texts = [c["text"] for c in chunks]

    # 4) Embed (Gemini)
    gemini = get_gemini_client()

    # Gemini supports embedding multiple strings at once via contents=[...] :contentReference[oaicite:4]{index=4}
    all_vectors: list[list[float]] = []
    for i in range(0, len(chunk_texts), batch_size):
        batch = chunk_texts[i:i + batch_size]
        result = gemini.models.embed_content(
            model="gemini-embedding-001",
            contents=batch,
            # keep vector size consistent for Pinecone index
            config={"output_dimensionality": embed_dim},
        )
        # result.embeddings should align 1:1 with contents
        # Each embedding object contains "values" (list[float]) in the SDK.
        # We’ll support both common shapes safely.
        for emb in result.embeddings:
            vec = getattr(emb, "values", None) or emb.get("values")
            all_vectors.append(l2_normalize(vec))

    # 5) Build Pinecone vectors
    vectors = []
    for c, vec in zip(chunks, all_vectors):
        vectors.append({
            "id": f"{doc_id}:{c['chunk_id']}",
            "values": vec,
            "metadata": {
                "tenant_id": tenant_id,
                "doc_id": doc_id,
                "chunk_id": c["chunk_id"],
                "start": c["start"],
                "end": c["end"],
                # MVP choice: store chunk text in metadata for easy retrieval.
                # Later we may store only pointers to reduce metadata size.
                "text": c["text"],
            },
        })

    # 6) Upsert to Pinecone namespace=tenant_id :contentReference[oaicite:5]{index=5}
    index = get_pinecone_index()
    upserted_total = 0
    for i in range(0, len(vectors), 100):  # Pinecone recommends batching; 100 is safe
        resp = index.upsert(vectors=vectors[i:i + 100], namespace=tenant_id)
        # resp is usually dict-like with upsertedCount
        upserted_total += getattr(resp, "upserted_count", None) or resp.get("upsertedCount", 0)

    log_json(
        level="INFO",
        event="doc_indexed",
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=None,
        doc_id=doc_id,
        num_chunks=len(chunks),
        embedding_model="gemini-embedding-001",
        embed_dim=embed_dim,
        upserted_count=upserted_total,
    )

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "doc_id": doc_id,
        "num_chunks": len(chunks),
        "embed_dim": embed_dim,
        "upserted_count": upserted_total,
    }


@app.post("/v1/search")
def search(req: SearchRequest, request: Request):
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)

    query_text = req.query.strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    # 1) Embed the query with Gemini (must match the SAME dim as your indexed vectors)
    embed_dim = 768
    gemini = get_gemini_client()
    result = gemini.models.embed_content(
        model="gemini-embedding-001",
        contents=[query_text],
        config={"output_dimensionality": embed_dim},
    )

    emb0 = result.embeddings[0]
    qvec = getattr(emb0, "values", None)
    if qvec is None and isinstance(emb0, dict):
        qvec = emb0.get("values")
    if qvec is None:
        raise RuntimeError(f"Unexpected embedding shape: {type(emb0)}")

    qvec = l2_normalize(qvec)

    # 2) Query Pinecone in tenant namespace
    index = get_pinecone_index()

    # Optional filter: restrict to a specific doc_id
    pinecone_filter = None
    if req.doc_id:
        pinecone_filter = {"doc_id": {"$eq": req.doc_id}}

    res = index.query(
        vector=qvec,
        top_k=req.top_k,
        namespace=tenant_id,
        include_metadata=True,
        filter=pinecone_filter,
    )

    # 3) Normalize response into a clean payload
    matches = []
    retrieved_doc_ids: set[str] = set()
    raw_matches = getattr(res, "matches", None) or res.get("matches", [])
    for m in raw_matches:
        mid = getattr(m, "id", None) or m.get("id")
        score = getattr(m, "score", None) or m.get("score")
        md = getattr(m, "metadata", None) or m.get("metadata", {}) or {}

        if md.get("doc_id"):
            retrieved_doc_ids.add(md["doc_id"])

        # "citation" is the minimal info we need to reference the source chunk
        citation = {
            "source_id": mid,
            "doc_id": md.get("doc_id"),
            "chunk_id": md.get("chunk_id"),
            "start": md.get("start"),
            "end": md.get("end"),
        }

        matches.append({
            "id": mid,
            "score": score,
            "text": md.get("text", ""),
            "metadata": {
                "doc_id": md.get("doc_id"),
                "chunk_id": md.get("chunk_id"),
                "start": md.get("start"),
                "end": md.get("end"),
            },
            "citation": citation,
        })

    log_json(
        level="INFO",
        event="search",
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        embedding_model="gemini-embedding-001",
        top_k=req.top_k,
        retrieved_k=len(matches),
        retrieved_doc_ids=sorted(retrieved_doc_ids) if retrieved_doc_ids else None,
        doc_id_filter=req.doc_id,
    )

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "query": req.query,
        "top_k": req.top_k,
        "matches": matches,
    }



@app.post("/v1/ask")
def ask(req: AskRequest, request: Request):
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)

    query_text = req.query.strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    # 1) Retrieve top chunks
    matches = retrieve_matches(tenant_id, query_text, req.top_k, req.doc_id, request_id=request_id)

    if not matches:
        return {
            "ok": True,
            "service": "api",
            "request_id": request_id,
            "tenant_id": tenant_id,
            "query": req.query,
            "answer": "I couldn’t find that in the provided documents.",
            "citations": [],
            "matches": [],
        }

    # 2) Build grounded context with explicit citations
    # We'll tag each snippet like: [doc_id:chunk_id]
    snippets = []
    citations = []
    for m in matches:
        md = m["metadata"]
        tag = f"[{md.get('doc_id')}:{md.get('chunk_id')}]"
        text = (m.get("text") or "").strip()
        if text:
            snippets.append(f"{tag}\n{text}")
        citations.append(m["citation"])

    context_block = "\n\n---\n\n".join(snippets)

    system_instruction = (
        "You are an assistant that answers ONLY using the provided document excerpts.\n"
        "If the answer is not in the excerpts, say you cannot find it.\n"
        "When you use a fact, include the citation tag like [doc_id:chunk_id]."
    )

    user_prompt = (
        f"Question: {query_text}\n\n"
        f"Document excerpts:\n{context_block}\n\n"
        "Answer the question using only the excerpts. Include citations."
    )

    # 3) Generate answer (Gemini)
    gemini = get_gemini_client()
    model_name = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.0-flash")

    full_prompt = f"{system_instruction}\n\n{user_prompt}"

    try:
        resp = gemini.models.generate_content(
            model=model_name,
            contents=full_prompt,
        )
    except genai_errors.ClientError as e:
        msg = str(e)
        log_json(
            level="ERROR",
            event="llm_call_failed",
            service="api",
            request_id=request_id,
            tenant_id=tenant_id,
            user_id=None,
            llm_provider="google",
            llm_model=model_name,
            error="GENAI_CLIENT_ERROR",
            detail=msg[:500],
            retrieved_k=len(matches),
            token_usage=None,
        )
        # Extractive fallback: answer from sources without generation
        top = matches[:3]
        bullets = []
        for m in top:
            txt = (m.get("text") or "").strip().replace("\n", " ")
            if txt:
                bullets.append(f"- {txt}")

        fallback_answer = (
            "⚠️ LLM generation is currently unavailable. "
            "Here are the most relevant excerpts from your documents:\n\n"
            + "\n".join(bullets)
            + "\n\nAnswer mode: extractive (no AI generation)."
        )

        return {
            "ok": True,
            "service": "api",
            "request_id": request_id,
            "tenant_id": tenant_id,
            "query": req.query,
            "answer": fallback_answer,
            "answer_mode": "extractive",
            "citations": citations,
            "matches": matches,
        }



    # Extract text safely
    answer = getattr(resp, "text", None)
    if answer is None and isinstance(resp, dict):
        answer = resp.get("text")
    if not answer:
        answer = "I couldn't generate an answer from the provided excerpts."

    # Extract token usage if the SDK exposes it
    usage_metadata = getattr(resp, "usage_metadata", None)
    token_usage = None
    if usage_metadata is not None:
        token_usage = {
            "prompt_tokens": getattr(usage_metadata, "prompt_token_count", None),
            "candidates_tokens": getattr(usage_metadata, "candidates_token_count", None),
            "total_tokens": getattr(usage_metadata, "total_token_count", None),
        }

    retrieved_doc_ids: set[str] = set()
    for m in matches:
        d = (m.get("metadata") or {}).get("doc_id")
        if d:
            retrieved_doc_ids.add(d)

    log_json(
        level="INFO",
        event="llm_call",
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=None,
        llm_provider="google",
        llm_model=model_name,
        answer_mode="rag",
        retrieved_k=len(matches),
        retrieved_doc_ids=sorted(retrieved_doc_ids) if retrieved_doc_ids else None,
        top_k=req.top_k,
        doc_id_filter=req.doc_id,
        token_usage=token_usage,
    )

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "query": req.query,
        "answer": answer,
        "answer_mode": "rag",
        "citations": citations,
        "matches": matches,  # keep for debugging/UI “sources panel”
    }

