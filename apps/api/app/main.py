from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi import UploadFile, File
from pathlib import Path
from pypdf import PdfReader

import json
import time
import uuid
import os
import httpx


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
    return "demo-tenant"


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




#----------------------------------------

app = FastAPI(title="Apalym API")

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start = time.time()

    # Store on request.state so handlers can access it
    request.state.request_id = request_id

    try:
        response: Response = await call_next(request)
        response.headers["x-request-id"] = request_id
        status_code = response.status_code
        return response
    finally:
        latency_ms = int((time.time() - start) * 1000)

        log_json(
            level="INFO",
            service="api",
            request_id=request_id,
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


AI_BASE_URL = os.getenv("AI_BASE_URL", "http://ai:8001")


class DemoRequest(BaseModel):
    message: str


@app.get("/health")
def health():
    return {"ok": True, "service": "api"}


@app.post("/v1/demo")
async def demo(req: DemoRequest, request: Request):
     
    request_id = request.state.request_id
    tenant_id = "demo-tenant"

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
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        event="doc_uploaded",
        doc_id=doc_id,
        filename=file.filename,
        size_bytes=size_bytes,
        stored_path=str(dest_path),
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
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        event="doc_text_extracted",
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
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        event="doc_chunked",
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


