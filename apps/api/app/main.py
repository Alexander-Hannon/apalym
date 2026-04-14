from datetime import date, datetime, timezone
import csv
import re
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi import UploadFile, File
from pathlib import Path
from openpyxl import load_workbook
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
SUPPORTED_DOC_EXTENSIONS = ("pdf", "csv", "xlsx")

def get_tenant_id(request: Request) -> str:
    # For now: hardcoded. Later: from auth / workspace selection.
    tenant_id = "demo-tenant"
    request.state.tenant_id = tenant_id
    return tenant_id


def get_doc_path(tenant_id: str, doc_id: str) -> Path:
    tenant_dir = STORAGE_DIR / tenant_id
    for ext in SUPPORTED_DOC_EXTENSIONS:
        candidate = tenant_dir / f"{doc_id}.{ext}"
        if candidate.exists():
            return candidate
    return tenant_dir / f"{doc_id}.pdf"


def infer_doc_extension(filename: str | None, content_type: str | None) -> str:
    filename_lower = (filename or "").lower()
    content_type_lower = (content_type or "").lower()

    if (
        filename_lower.endswith(".xlsx")
        or content_type_lower
        in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        )
    ):
        return "xlsx"

    if filename_lower.endswith(".csv") or content_type_lower in ("text/csv", "application/csv"):
        return "csv"

    if filename_lower.endswith(".pdf") or content_type_lower in ("application/pdf", "application/octet-stream"):
        return "pdf"

    raise HTTPException(status_code=400, detail="Only PDF, CSV, and XLSX uploads are supported")


def extract_pdf_text(doc_path: Path) -> str:
    reader = PdfReader(str(doc_path))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts)


def extract_csv_text(doc_path: Path) -> str:
    with doc_path.open("r", encoding="utf-8-sig", newline="", errors="replace") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            return ""

        header_pairs: list[tuple[str, str]] = []
        for idx, source_header in enumerate(reader.fieldnames, start=1):
            source_key = source_header or ""
            cleaned = source_key.strip()
            label = cleaned if cleaned else f"column_{idx}"
            header_pairs.append((source_key, label))

        sections = [f"CSV columns: {', '.join(label for _, label in header_pairs)}"]

        for row_index, raw_row in enumerate(reader, start=1):
            row_lines = []
            for source_key, label in header_pairs:
                value = (raw_row.get(source_key) or "").strip()
                if value:
                    row_lines.append(f"{label}: {value}")

            extra_cells = raw_row.get(None) or []
            for extra_index, value in enumerate(extra_cells, start=1):
                cleaned = (value or "").strip()
                if cleaned:
                    row_lines.append(f"extra_column_{extra_index}: {cleaned}")

            if row_lines:
                sections.append(f"Row {row_index}:\n" + "\n".join(row_lines))

        return "\n\n".join(sections)


def normalize_sheet_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        # Drop trailing .0 for integers stored as floats
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    return text


def extract_xlsx_text(doc_path: Path) -> str:
    workbook = load_workbook(filename=str(doc_path), read_only=True, data_only=True)
    sections: list[str] = []
    try:
        for worksheet in workbook.worksheets:
            rows = worksheet.iter_rows(values_only=True)
            sheet_lines = [f"Sheet: {worksheet.title}"]

            first_row = next(rows, None)
            if first_row is None:
                sheet_lines.append("(empty sheet)")
                sections.append("\n".join(sheet_lines))
                continue

            header_cells = [normalize_sheet_cell(value) for value in first_row]
            headers = [cell if cell else f"column_{idx + 1}" for idx, cell in enumerate(header_cells)]
            sheet_lines.append(f"Columns: {', '.join(headers)}")

            row_number = 2
            for raw_row in rows:
                row_parts = []
                for idx, value in enumerate(raw_row):
                    normalized = normalize_sheet_cell(value)
                    if not normalized:
                        continue
                    label = headers[idx] if idx < len(headers) else f"column_{idx + 1}"
                    row_parts.append(f"{label}: {normalized}")
                if row_parts:
                    sheet_lines.append(f"Row {row_number}: " + ", ".join(row_parts))
                row_number += 1

            sections.append("\n".join(sheet_lines))
    finally:
        workbook.close()

    return "\n\n".join(sections)


def extract_doc_text(doc_path: Path) -> str:
    suffix = doc_path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(doc_path)
    if suffix == ".csv":
        return extract_csv_text(doc_path)
    if suffix == ".xlsx":
        return extract_xlsx_text(doc_path)
    raise HTTPException(status_code=400, detail="Unsupported stored document type")


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
ASK_TOP_K_CAP = 3
ASK_MAX_CHUNK_CHARS = 600
ASK_MAX_CONTEXT_CHARS = 1800
ASK_MAX_GENERATION_ATTEMPTS = 3
ASK_RETRY_BASE_BACKOFF_SECONDS = 0.6

class DemoRequest(BaseModel):
    message: str

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(5, ge=1, le=20)
    doc_id: Optional[str] = None  # optional filter to a specific doc

class AskRequest(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(3, ge=1, le=10)
    doc_id: Optional[str] = None  # optional: restrict to a document

class AskBrief(BaseModel):
    direct_answer: str
    key_findings: list[str]
    recommended_next_actions: list[str]

def compact_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def strip_markdown_emphasis(text: str) -> str:
    # Keep plain text if model returns markdown emphasis or code markers.
    no_code = re.sub(r"`([^`]+)`", r"\1", text)
    no_bold = re.sub(r"\*\*([^*]+)\*\*", r"\1", no_code)
    no_italic_star = re.sub(r"\*([^*]+)\*", r"\1", no_bold)
    no_italic_underscore = re.sub(r"_([^_]+)_", r"\1", no_italic_star)
    return compact_whitespace(no_italic_underscore)

def clean_brief_list_items(items: list[Any], fallback: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        normalized = strip_markdown_emphasis(item).lstrip("-• ").strip()
        if normalized:
            cleaned.append(normalized)
    return cleaned if cleaned else fallback

def is_generic_action(text: str) -> bool:
    normalized = compact_whitespace(text).lower()
    generic_phrases = (
        "validate the answer",
        "validate answer",
        "ask a follow-up",
        "ask a follow up",
        "follow-up question",
        "follow up question",
        "cross-check",
        "cross check",
        "verify the answer",
        "review a source",
        "review sources",
    )
    return any(phrase in normalized for phrase in generic_phrases)

def build_dynamic_fallback_actions(query_text: str, matches: list[dict[str, Any]]) -> list[str]:
    q = compact_whitespace(query_text).lower()
    top_match_text = compact_whitespace((matches[0].get("text") if matches else "") or "")
    snippet = top_match_text[:140] + ("..." if len(top_match_text) > 140 else "")

    if any(token in q for token in ("aging", "receivable", "ar ", "a/r", "invoice")):
        return [
            "Review AR balances over 60 days and prioritize top delinquent accounts for collection outreach this week.",
            "Break down overdue receivables by customer segment and assign owners for high-exposure accounts."
        ]
    if any(token in q for token in ("fleet", "vehicle", "van", "maintenance")):
        return [
            "Audit maintenance spend by vehicle and flag units above the fleet median for immediate review.",
            "Compare preventive vs. reactive maintenance patterns and schedule interventions for high-cost vehicles."
        ]
    if any(token in q for token in ("discount", "pricing", "margin", "category")):
        return [
            "Check discount rates by category and identify where discounting is compressing margin beyond policy thresholds.",
            "Review outlier deals with high discount percentages and require approval adjustments for repeat patterns."
        ]
    if any(token in q for token in ("churn", "retention", "renewal")):
        return [
            "Segment at-risk accounts by renewal window and trigger targeted retention actions for the highest-risk tier.",
            "Analyze drivers shared across churn-risk accounts and feed the top driver into next-cycle account plans."
        ]

    generic = f"Run a focused drill-down on '{compact_whitespace(query_text)}' by segment, owner, and period to isolate decision-ready outliers."
    if snippet:
        generic += f" Start with this evidence thread: {snippet}"
    return [
        generic,
        "Convert the top finding into an operational action owner, target metric, and review date for this reporting cycle.",
    ]

def build_answer_brief(query_text: str, answer_text: str, matches: list[dict[str, Any]]) -> AskBrief:
    normalized_answer = (answer_text or "").strip()
    lines = [line.strip() for line in normalized_answer.replace("\r", "").splitlines() if line.strip()]
    clean_lines = [strip_markdown_emphasis(line) for line in lines if strip_markdown_emphasis(line)]

    direct_answer = clean_lines[0] if clean_lines else "I couldn't generate a clear direct answer from the provided excerpts."

    bullet_like = []
    for line in clean_lines:
        if re.match(r"^((?:[-•*])|(?:\d+[.)]))\s+", line):
            bullet_like.append(re.sub(r"^((?:[-•*])|(?:\d+[.)]))\s+", "", line).strip())
    key_findings = bullet_like[:4] if bullet_like else clean_lines[1:5]
    if not key_findings:
        key_findings = [direct_answer]

    recommended = build_dynamic_fallback_actions(query_text, matches)
    return AskBrief(
        direct_answer=direct_answer,
        key_findings=key_findings[:4],
        recommended_next_actions=recommended[:4],
    )

def merge_brief_with_fallback(
    query_text: str,
    answer_text: str,
    parsed_brief: AskBrief | None,
    matches: list[dict[str, Any]],
) -> AskBrief:
    fallback = build_answer_brief(query_text, answer_text, matches)
    if parsed_brief is None:
        return fallback

    direct_answer = strip_markdown_emphasis(parsed_brief.direct_answer) or fallback.direct_answer
    key_findings = clean_brief_list_items(parsed_brief.key_findings, fallback.key_findings)

    candidate_actions = clean_brief_list_items(parsed_brief.recommended_next_actions, [])
    filtered_actions = [action for action in candidate_actions if not is_generic_action(action)]
    recommended_actions = filtered_actions if filtered_actions else fallback.recommended_next_actions

    return AskBrief(
        direct_answer=direct_answer,
        key_findings=key_findings[:4],
        recommended_next_actions=recommended_actions[:4],
    )

def trim_snippet_text(text: str, max_chars: int) -> str:
    compact = compact_whitespace(text)
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def build_context_snippets(
    matches: list[dict[str, Any]],
    max_chunk_chars: int = ASK_MAX_CHUNK_CHARS,
    max_total_chars: int = ASK_MAX_CONTEXT_CHARS,
) -> tuple[list[str], int]:
    snippets: list[str] = []
    total_chars = 0

    for m in matches:
        md = m["metadata"]
        tag = f"[{md.get('doc_id')}:{md.get('chunk_id')}]"
        trimmed_text = trim_snippet_text((m.get("text") or "").strip(), max_chunk_chars)
        if not trimmed_text:
            continue

        snippet = f"{tag}\n{trimmed_text}"
        snippet_len = len(snippet)
        if snippets and total_chars + snippet_len > max_total_chars:
            break
        if not snippets and snippet_len > max_total_chars:
            snippet = snippet[: max_total_chars - 1].rstrip() + "…"
            snippet_len = len(snippet)

        snippets.append(snippet)
        total_chars += snippet_len
        if total_chars >= max_total_chars:
            break

    return snippets, total_chars


def is_retryable_generation_error(error: genai_errors.ClientError) -> bool:
    detail = str(error).lower()
    return (
        "429" in detail
        or "resource_exhausted" in detail
        or "rate limit" in detail
        or "quota" in detail
    )


def generate_content_with_retry(
    gemini: genai.Client,
    model_name: str,
    full_prompt: str,
    request_id: str,
    tenant_id: str,
    retrieved_k: int,
) -> Any:
    last_error: genai_errors.ClientError | None = None

    for attempt in range(1, ASK_MAX_GENERATION_ATTEMPTS + 1):
        try:
            return gemini.models.generate_content(
                model=model_name,
                contents=full_prompt,
            )
        except genai_errors.ClientError as error:
            last_error = error
            retryable = is_retryable_generation_error(error)
            if not retryable or attempt >= ASK_MAX_GENERATION_ATTEMPTS:
                raise

            backoff_seconds = ASK_RETRY_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log_json(
                level="WARNING",
                event="llm_call_retry",
                service="api",
                request_id=request_id,
                tenant_id=tenant_id,
                llm_provider="google",
                llm_model=model_name,
                retrieved_k=retrieved_k,
                attempt=attempt,
                backoff_seconds=backoff_seconds,
                error="GENAI_CLIENT_ERROR",
                detail=str(error)[:300],
            )
            time.sleep(backoff_seconds)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Unexpected empty generation retry result")


def parse_model_json(text: str) -> dict | None:
    candidate = text.strip()
    if not candidate:
        return None

    # Handle model responses wrapped in markdown fences.
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].strip()

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return None
    return None


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

    doc_ext = infer_doc_extension(file.filename, file.content_type)

    doc_id = str(uuid.uuid4())
    tenant_dir = STORAGE_DIR / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)

    dest_path = tenant_dir / f"{doc_id}.{doc_ext}"

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
        doc_type=doc_ext,
        size_bytes=size_bytes,
    )

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "doc_id": doc_id,
        "filename": file.filename,
        "doc_type": doc_ext,
        "size_bytes": size_bytes,
    }



@app.get("/v1/docs/{doc_id}/text")
def get_doc_text(doc_id: str, request: Request):
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)

    doc_path = get_doc_path(tenant_id, doc_id)
    if not doc_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    text = extract_doc_text(doc_path)
    preview = text[:1000]

    log_json(
        level="INFO",
        event="doc_text_extracted",
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=None,
        doc_id=doc_id,
        doc_type=doc_path.suffix.lower().lstrip("."),
        chars=len(text),
    )

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "doc_id": doc_id,
        "doc_type": doc_path.suffix.lower().lstrip("."),
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

    doc_path = get_doc_path(tenant_id, doc_id)
    if not doc_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    text = extract_doc_text(doc_path)

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
        doc_type=doc_path.suffix.lower().lstrip("."),
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
        "doc_type": doc_path.suffix.lower().lstrip("."),
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
    - extract text from stored PDF, CSV, or XLSX
    - chunk with overlap
    - embed chunks with Gemini
    - upsert vectors into Pinecone under tenant namespace
    """
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)

    # 1) Load stored document
    doc_path = get_doc_path(tenant_id, doc_id)
    if not doc_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    # 2) Extract text
    text = extract_doc_text(doc_path).strip()

    if not text:
        return {
            "ok": True,
            "service": "api",
            "request_id": request_id,
            "tenant_id": tenant_id,
            "doc_id": doc_id,
            "doc_type": doc_path.suffix.lower().lstrip("."),
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
        doc_type=doc_path.suffix.lower().lstrip("."),
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
        "doc_type": doc_path.suffix.lower().lstrip("."),
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
        model="gemini-3-flash",
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
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="doc_id is required for ask in demo mode")

    effective_top_k = max(1, min(req.top_k, ASK_TOP_K_CAP))

    # 1) Retrieve top chunks scoped to active document
    matches = retrieve_matches(tenant_id, query_text, effective_top_k, req.doc_id, request_id=request_id)

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

    # 2) Build bounded grounded context with explicit citations
    snippets, context_chars = build_context_snippets(matches)
    citations = [m["citation"] for m in matches]
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

    brief_instruction = (
        "Return JSON with keys: direct_answer, key_findings, recommended_next_actions.\n"
        "Rules:\n"
        "- direct_answer: one concise business answer with citations like [doc_id:chunk_id]\n"
        "- key_findings: 2-4 concrete findings tied to the provided evidence and include citations\n"
        "- recommended_next_actions: 2-4 specific operational/business next steps tailored to the question and evidence\n"
        "- Do NOT output generic filler like 'validate answer' or 'ask follow-up'\n"
        "- Return ONLY valid JSON"
    )

    # 3) Generate answer (Gemini)
    gemini = get_gemini_client()
    model_name = os.getenv("GEMINI_CHAT_MODEL", "gemini-3.0-flash")

    full_prompt = f"{system_instruction}\n\n{user_prompt}\n\n{brief_instruction}"

    try:
        resp = generate_content_with_retry(
            gemini=gemini,
            model_name=model_name,
            full_prompt=full_prompt,
            request_id=request_id,
            tenant_id=tenant_id,
            retrieved_k=len(matches),
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
            context_chars=context_chars,
            top_k=effective_top_k,
            doc_id_filter=req.doc_id,
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
        fallback_brief = merge_brief_with_fallback(query_text, fallback_answer, None, matches)

        return {
            "ok": True,
            "service": "api",
            "request_id": request_id,
            "tenant_id": tenant_id,
            "query": req.query,
            "answer": fallback_answer,
            "answer_mode": "extractive",
            "brief": {
                "direct_answer": fallback_brief.direct_answer,
                "key_findings": fallback_brief.key_findings,
                "recommended_next_actions": fallback_brief.recommended_next_actions,
            },
            "citations": citations,
            "matches": matches,
        }



    # Extract text safely
    answer = getattr(resp, "text", None)
    if answer is None and isinstance(resp, dict):
        answer = resp.get("text")
    if not answer:
        answer = "I couldn't generate an answer from the provided excerpts."

    parsed_brief: AskBrief | None = None
    brief_obj = parse_model_json(answer)
    if brief_obj is not None:
        try:
            parsed_brief = AskBrief.model_validate(brief_obj)
        except Exception:
            parsed_brief = None

    final_brief = merge_brief_with_fallback(query_text, answer, parsed_brief, matches)

    answer = final_brief.direct_answer

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
        top_k=effective_top_k,
        context_chars=context_chars,
        doc_id_filter=req.doc_id,
        token_usage=token_usage,
    )

    response_payload = {
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

    response_payload["brief"] = {
        "direct_answer": final_brief.direct_answer,
        "key_findings": final_brief.key_findings,
        "recommended_next_actions": final_brief.recommended_next_actions,
    }

    return response_payload

