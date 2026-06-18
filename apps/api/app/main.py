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

from app import client_test_auth as client_auth

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
    """
    Resolve tenant from client-test access keys when APALYM_CLIENT_TEST_KEYS is set.
    Otherwise use open demo mode (single shared DEMO_TENANT_ID). Tenant is never
    taken from client-supplied x-tenant-id headers.
    """
    key_map = client_auth.load_client_test_key_map()
    if not key_map:
        tenant_id = os.getenv("DEMO_TENANT_ID", "demo-tenant")
        request.state.tenant_id = tenant_id
        request.state.workspace_id = tenant_id
        request.state.client_display_label = None
        return tenant_id

    token = client_auth.extract_bearer_token(request.headers.get("authorization"))
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing access key. Sign in or send Authorization: Bearer <access_key>.",
        )
    principal = client_auth.resolve_principal(token)
    if not principal:
        raise HTTPException(status_code=401, detail="Unknown access key.")

    request.state.tenant_id = principal.tenant_id
    request.state.workspace_id = principal.workspace_id
    request.state.client_display_label = principal.display_label
    return principal.tenant_id


def _cors_allow_origins() -> list[str]:
    raw = os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    # Empty misconfiguration would block all browser origins; fall back to local dev default.
    if not origins:
        return ["http://localhost:3000"]
    return origins


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
ASK_MAX_GENERATION_ATTEMPTS = 4
ASK_RETRY_BASE_BACKOFF_SECONDS = 0.75
INSIGHT_TOP_K_CAP = 3
INSIGHT_MAX_CHUNK_CHARS = 480
INSIGHT_MAX_CONTEXT_CHARS = 1500
INSIGHT_MAX_GENERATION_ATTEMPTS = 5
INSIGHT_RETRY_BASE_BACKOFF_SECONDS = 1.0
INSIGHT_MODULE_PAUSE_SECONDS = 0.35

INSIGHT_MODULE_DEFINITIONS = [
    {
        "module_id": "loss-leakage",
        "title": "Where are we losing money?",
        "retrieval_query": (
            "Identify where profitability is leaking, including revenue leakage, excess costs, "
            "inefficiencies, write-offs, or recurring losses."
        ),
    },
    {
        "module_id": "top-cost-drivers",
        "title": "Top cost drivers",
        "retrieval_query": (
            "Identify the largest cost drivers, recurring expense categories, and operational drivers "
            "with the highest financial impact."
        ),
    },
    {
        "module_id": "key-risks",
        "title": "Key risks",
        "retrieval_query": (
            "Identify key business risks, including compliance, liquidity, customer concentration, "
            "operational bottlenecks, and execution risks."
        ),
    },
    {
        "module_id": "margin-opportunities",
        "title": "Margin improvement opportunities",
        "retrieval_query": (
            "Identify practical margin improvement opportunities through pricing, discount discipline, "
            "mix shifts, process efficiency, and cost optimization."
        ),
    },
    {
        "module_id": "trends-anomalies",
        "title": "Notable trends or anomalies",
        "retrieval_query": (
            "Identify notable trends, abrupt changes, outliers, seasonality signals, and anomalies "
            "that deserve investigation."
        ),
    },
]

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

class InsightsRequest(BaseModel):
    doc_id: str = Field(..., min_length=1)
    top_k: int = Field(3, ge=1, le=8)

class AskBrief(BaseModel):
    direct_answer: str
    key_findings: list[str]
    recommended_next_actions: list[str]
    confidence: Optional[str] = None
    evidence_note: Optional[str] = None

class InsightModuleBrief(BaseModel):
    summary: str
    direct_answer: str
    key_findings: list[str]
    recommended_actions: list[str]

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

FALLBACK_EVIDENCE_NOTE = (
    "Needs validation — this diagnosis is based on limited or messy source evidence "
    "and should be validated with supporting data."
)

MODULE_FALLBACK_COPY: dict[str, dict[str, Any]] = {
    "losses": {
        "title": "Where are we losing money?",
        "direct_answer": (
            "The document does not provide enough clean detail to isolate exact loss drivers. "
            "Based on the available evidence, leadership should review likely leakage areas "
            "and validate them with supporting schedules."
        ),
        "key_findings": [
            "The available evidence suggests possible revenue or profit leakage, but it does not quantify each driver with confidence.",
            "Likely areas to review include discounts, returns, labor inefficiency, underutilized assets, delayed collections, or billing issues.",
            "Additional transaction-level, department-level, or customer-level detail is needed to separate recurring leakage from one-time variance.",
        ],
        "recommended_actions": [
            "Identify the top three variance categories by dollar impact.",
            "Separate one-time issues from recurring operating leakage.",
            "Assign owners to validate each suspected loss driver with supporting P&L, A/R, labor, refunds, or operations schedules.",
            "Re-run the analysis after uploading supporting detail.",
        ],
        "confidence": "Low",
        "evidence_note": FALLBACK_EVIDENCE_NOTE,
        "module_llm_status": "fallback_polished",
        "fallback_reason": "module_not_usable",
    },
    "costs": {
        "title": "Top cost drivers",
        "direct_answer": (
            "The document does not provide enough clean detail to fully isolate cost pressure. "
            "Based on the available evidence, likely cost drivers should be reviewed across "
            "labor, logistics, vendor spend, maintenance, claims, and overhead."
        ),
        "key_findings": [
            "Cost pressure may be present, but the available source evidence is not detailed enough to quantify each driver with confidence.",
            "Labor, logistics, vendor spend, maintenance, claims, and overhead should be reviewed as potential controllable cost categories.",
            "More granular cost schedules are needed to separate controllable costs from one-time or non-controllable variance.",
        ],
        "recommended_actions": [
            "Break costs into labor, logistics, vendor, maintenance, claims, and overhead.",
            "Compare each category against budget and prior period.",
            "Identify the top three recurring cost drivers by dollar impact.",
            "Assign cost owners and 30-day reduction targets.",
        ],
        "confidence": "Low",
        "evidence_note": FALLBACK_EVIDENCE_NOTE,
        "module_llm_status": "fallback_polished",
        "fallback_reason": "module_not_usable",
    },
    "risks": {
        "title": "Key risks",
        "direct_answer": (
            "The document does not provide enough clean detail to create a complete risk register. "
            "Based on the available evidence, leadership should review operational execution, "
            "cost control, compliance, vendor dependency, and cash flow risks."
        ),
        "key_findings": [
            "The available evidence suggests possible risk areas, but it does not fully define likelihood, impact, owners, or mitigation steps.",
            "Possible risks include execution delays, margin pressure, labor inefficiency, compliance exposure, vendor dependency, or cash flow volatility.",
            "More detailed operating and financial support is needed to prioritize risk severity.",
        ],
        "recommended_actions": [
            "Create a simple risk tracker with owner, likelihood, impact, mitigation, and deadline.",
            "Validate high-risk areas with department owners.",
            "Prioritize risks by financial impact and time sensitivity.",
            "Review mitigation progress weekly.",
        ],
        "confidence": "Low",
        "evidence_note": FALLBACK_EVIDENCE_NOTE,
        "module_llm_status": "fallback_polished",
        "fallback_reason": "module_not_usable",
    },
    "margins": {
        "title": "Margin improvement opportunities",
        "direct_answer": (
            "The document does not provide enough clean detail to calculate precise margin drivers. "
            "Based on the available evidence, leadership should review pricing, discounts, labor, "
            "logistics, returns, claims, and customer or product mix."
        ),
        "key_findings": [
            "Margin pressure may be present, but the source evidence does not provide enough detail to isolate exact drivers.",
            "Pricing, discounting, labor, logistics, returns, claims, and mix should be reviewed as possible margin factors.",
            "More detailed gross margin by customer, product, service line, or channel would improve confidence.",
        ],
        "recommended_actions": [
            "Break margin by product, customer, channel, or service line.",
            "Identify low-margin segments and high-cost-to-serve customers.",
            "Compare pricing and discounting against profitability.",
            "Build a 30-day margin improvement action plan.",
        ],
        "confidence": "Low",
        "evidence_note": FALLBACK_EVIDENCE_NOTE,
        "module_llm_status": "fallback_polished",
        "fallback_reason": "module_not_usable",
    },
    "trends": {
        "title": "Notable trends or anomalies",
        "direct_answer": (
            "The document does not provide enough clean time-series detail to confirm a trend "
            "with confidence. Based on the available evidence, leadership should monitor "
            "recurring changes in revenue, costs, utilization, returns, collections, labor, and demand."
        ),
        "key_findings": [
            "The available excerpts may indicate changes over time, but they do not provide enough clean period-over-period detail to confirm trend strength.",
            "Possible patterns may exist in revenue, costs, utilization, labor, returns, collections, or demand.",
            "More historical data is needed to distinguish recurring trends from one-time variance.",
        ],
        "recommended_actions": [
            "Compare the last three to six periods for the relevant metrics.",
            "Separate one-time anomalies from recurring patterns.",
            "Track leading indicators weekly.",
            "Re-run the analysis with updated monthly data.",
        ],
        "confidence": "Low",
        "evidence_note": FALLBACK_EVIDENCE_NOTE,
        "module_llm_status": "fallback_polished",
        "fallback_reason": "module_not_usable",
    },
}

_GENERIC_FALLBACK_COPY: dict[str, Any] = {
    "title": "Executive diagnosis",
    "direct_answer": (
        "The document does not provide enough clean detail to make a definitive diagnosis "
        "for this category. Based on the available evidence, leadership should validate "
        "the issue with supporting data before acting."
    ),
    "key_findings": [
        "The available evidence is directionally useful but incomplete.",
        "The document does not provide enough detail to confirm severity, owner, or root cause.",
        "Additional supporting schedules or department-level detail would improve confidence.",
    ],
    "recommended_actions": [
        "Identify the top three possible drivers by financial or operational impact.",
        "Assign an owner to validate each driver with supporting data.",
        "Set a follow-up review date and re-run the analysis after uploading more detail.",
    ],
    "confidence": "Low",
    "evidence_note": FALLBACK_EVIDENCE_NOTE,
    "module_llm_status": "fallback_polished",
    "fallback_reason": "module_not_usable",
}


def normalize_module_key(value: str, module_title: str = "") -> str:
    combined = compact_whitespace(f"{value or ''} {module_title or ''}").lower()
    if combined in {"losses", "loss", "where are we losing money?", "losing money"}:
        return "losses"
    if combined in {"costs", "cost", "top cost drivers", "cost drivers"}:
        return "costs"
    if combined in {"risks", "risk", "key risks"}:
        return "risks"
    if combined in {"margins", "margin", "margin improvement opportunities"}:
        return "margins"
    if combined in {"trends", "trend", "notable trends or anomalies", "anomalies"}:
        return "trends"
    if "loss" in combined or "leakage" in combined or "losing money" in combined:
        return "losses"
    if "cost" in combined or "expense" in combined:
        return "costs"
    if "risk" in combined or "compliance" in combined:
        return "risks"
    if "margin" in combined or "pricing" in combined:
        return "margins"
    if "trend" in combined or "anomal" in combined:
        return "trends"
    return "generic"


_GENERIC_PLACEHOLDER_MARKERS: tuple[tuple[str, ...], ...] = (
    ("additional detail", "fully assess this finding"),
    ("validate this area", "relevant department owner"),
    ("module", "clean diagnosis"),
    ("regenerate", "upload supporting detail"),
    ("highest-impact cited item",),
    ("root causes", "source evidence"),
    ("could not be assembled", "your document"),
)
_BROKEN_LEADING_SUFFIXES = re.compile(
    r"^(ductivity|tional|ant\b|holds\b|d\s+dock|ment\b|cy\b)\s",
    re.IGNORECASE,
)
_REPEATED_QUALITY_WORDS = re.compile(
    r"\b(Medium|High|Low|Growth)\s+\1\b",
    re.IGNORECASE,
)
_UUID_LIKE_PATTERN = re.compile(
    r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b",
    re.IGNORECASE,
)
_BRACKETED_CITATION_PATTERN = re.compile(r"\[([^\[\]]+)\]")
_NUMERIC_CITATION_PATTERN = re.compile(r"^\d{1,2}$")


def _citation_token_key(value: Any) -> str:
    token = compact_whitespace(str(value or "")).strip("[]")
    token = re.sub(r"^source:", "", token, flags=re.IGNORECASE)
    return token.replace(" ", "").lower()


def _citation_lookup(citations: list[dict[str, Any]]) -> dict[str, int]:
    lookup: dict[str, int] = {}
    for index, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        candidates = [
            citation.get("source_id"),
            citation.get("doc_id"),
            citation.get("chunk_id"),
        ]
        if citation.get("doc_id") and citation.get("chunk_id"):
            candidates.append(f"{citation.get('doc_id')}:{citation.get('chunk_id')}")
        if citation.get("source_id"):
            candidates.append(f"source:{citation.get('source_id')}")
        for candidate in candidates:
            key = _citation_token_key(candidate)
            if key and key not in lookup:
                lookup[key] = index
    return lookup


def _looks_like_raw_citation_token(token: str) -> bool:
    key = _citation_token_key(token)
    if not key or _NUMERIC_CITATION_PATTERN.fullmatch(key):
        return False
    if _UUID_LIKE_PATTERN.search(key):
        return True
    if key.startswith(("source_", "chunk_", "doc_")):
        return True
    if ":" in key and not re.search(r"\s", key):
        return True
    return False


def normalize_visible_citations(text: str, citations: list[dict[str, Any]] | None = None) -> str:
    if not isinstance(text, str) or not text:
        return ""

    sources = citations or []
    source_count = len(sources)
    lookup = _citation_lookup(sources)
    fallback_numbers: dict[str, int] = {}
    next_number = 1

    def number_for_token(token: str) -> int | None:
        nonlocal next_number
        key = _citation_token_key(token)
        if not key:
            return None
        if _NUMERIC_CITATION_PATTERN.fullmatch(key):
            return int(key)
        if key in lookup:
            return lookup[key]
        if source_count == 0:
            return None
        if key not in fallback_numbers:
            fallback_numbers[key] = min(next_number, source_count)
            next_number += 1
        return fallback_numbers[key]

    def replace_bracket(match: re.Match[str]) -> str:
        content = match.group(1)
        parts = [part.strip() for part in content.split(",") if part.strip()]
        if not parts:
            return ""
        if not any(_looks_like_raw_citation_token(part) for part in parts):
            return match.group(0)

        numbers: list[int] = []
        for part in parts:
            citation_number = number_for_token(part)
            if citation_number is not None and citation_number not in numbers:
                numbers.append(citation_number)

        if not numbers:
            return ""
        return ", ".join(f"[{number}]" for number in numbers)

    cleaned = _BRACKETED_CITATION_PATTERN.sub(replace_bracket, text)
    cleaned = re.sub(r"(\[\d{1,2}\])(?:\s+\1)+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    return compact_whitespace(cleaned)


def _has_generic_placeholder(text: str) -> bool:
    low = compact_whitespace(text).lower()
    return any(all(part in low for part in marker_parts) for marker_parts in _GENERIC_PLACEHOLDER_MARKERS)


def _looks_like_table_row_dump(text: str) -> bool:
    t = compact_whitespace(text)
    label_count = len(re.findall(r"\b[A-Za-z][A-Za-z0-9 /%$&().-]{1,35}:\s*\S", t))
    if re.match(r"^row\s+\d+[:\s]", t, re.IGNORECASE) and label_count >= 2:
        return True
    if label_count >= 3:
        return True
    if len(t.split()) > 14 and not re.search(r"[.!?]", t) and (t.count("%") + t.count("$") >= 3):
        return True
    return False


def contains_raw_output_pattern(text: str) -> bool:
    if not text or not text.strip():
        return False
    t = compact_whitespace(text)
    return bool(
        _BROKEN_LEADING_SUFFIXES.match(t)
        or _REPEATED_QUALITY_WORDS.search(t)
        or _UUID_LIKE_PATTERN.search(t)
        or _looks_like_table_row_dump(t)
    )


def _clean_module_text(value: Any, citations: list[dict[str, Any]] | None = None) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = normalize_visible_citations(value, citations)
    cleaned = strip_markdown_emphasis(cleaned).lstrip("-• ").strip()
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned)
    return compact_whitespace(cleaned)


def _clean_module_list(value: Any, citations: list[dict[str, Any]] | None = None) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = _clean_module_text(item, citations)
        if text:
            cleaned.append(text)
    return cleaned


def _module_direct_answer_is_usable(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    cleaned = compact_whitespace(text)
    return len(cleaned) >= 80 and not _has_generic_placeholder(cleaned) and not contains_raw_output_pattern(cleaned)


def _module_list_is_usable(items: Any) -> bool:
    if not isinstance(items, list):
        return False
    cleaned = [compact_whitespace(item) for item in items if isinstance(item, str) and compact_whitespace(item)]
    normalized = [item.lower() for item in cleaned]
    return bool(
        len(cleaned) >= 3
        and len(set(normalized)) == len(normalized)
        and not any(_has_generic_placeholder(item) or contains_raw_output_pattern(item) for item in cleaned)
    )


def module_is_usable(module: dict[str, Any]) -> bool:
    return (
        _module_direct_answer_is_usable(module.get("direct_answer"))
        and _module_list_is_usable(module.get("key_findings"))
        and _module_list_is_usable(module.get("recommended_actions"))
    )


def build_category_fallback(normalized_key: str, module_result: dict[str, Any] | None = None) -> dict[str, Any]:
    original = module_result or {}
    copy = MODULE_FALLBACK_COPY.get(normalized_key, _GENERIC_FALLBACK_COPY)
    title = original.get("title") if isinstance(original.get("title"), str) else copy["title"]
    module_id = original.get("module_id") if isinstance(original.get("module_id"), str) else normalized_key

    fallback = {
        "module_id": module_id,
        "title": title,
        "status": "fallback",
        "summary": copy["direct_answer"],
        "direct_answer": copy["direct_answer"],
        "key_findings": list(copy["key_findings"]),
        "recommended_actions": list(copy["recommended_actions"]),
        "citations": original.get("citations") if isinstance(original.get("citations"), list) else [],
        "matches": original.get("matches") if isinstance(original.get("matches"), list) else [],
        "confidence": copy["confidence"],
        "evidence_note": copy["evidence_note"],
        "module_llm_status": copy["module_llm_status"],
        "fallback_reason": copy["fallback_reason"],
    }
    if original.get("error"):
        fallback["error"] = original["error"]
    return fallback


def minimally_clean_module(module_result: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in module_result.items() if not key.startswith("_finalize_")}
    citations = cleaned.get("citations") if isinstance(cleaned.get("citations"), list) else []
    cleaned["summary"] = _clean_module_text(cleaned.get("summary"), citations) or _clean_module_text(
        cleaned.get("direct_answer"),
        citations,
    )
    cleaned["direct_answer"] = _clean_module_text(cleaned.get("direct_answer"), citations)
    cleaned["key_findings"] = _clean_module_list(cleaned.get("key_findings"), citations)
    cleaned["recommended_actions"] = _clean_module_list(cleaned.get("recommended_actions"), citations)
    if isinstance(cleaned.get("assumptions"), list):
        cleaned["assumptions"] = _clean_module_list(cleaned.get("assumptions"), citations)
    if isinstance(cleaned.get("evidence_note"), str):
        cleaned["evidence_note"] = _clean_module_text(cleaned.get("evidence_note"), citations)
    if not cleaned["summary"]:
        cleaned["summary"] = cleaned["direct_answer"]
    return cleaned


def normalize_module_visible_text(module: dict[str, Any]) -> dict[str, Any]:
    citations = module.get("citations") if isinstance(module.get("citations"), list) else []
    normalized = dict(module)
    for field in ("direct_answer", "summary", "evidence_note"):
        if isinstance(normalized.get(field), str):
            normalized[field] = normalize_visible_citations(normalized[field], citations)
    for field in ("key_findings", "recommended_actions", "assumptions"):
        if isinstance(normalized.get(field), list):
            normalized[field] = [
                normalize_visible_citations(item, citations) if isinstance(item, str) else item
                for item in normalized[field]
            ]
    return normalized


def finalize_insight_module(module_key: str, module_result: dict[str, Any]) -> dict[str, Any]:
    normalized_key = normalize_module_key(module_key, module_result.get("title", ""))
    fallback = build_category_fallback(normalized_key, module_result)
    cleaned = minimally_clean_module(module_result)
    original_status = module_result.get("status", "unknown")

    direct_answer_usable = _module_direct_answer_is_usable(cleaned.get("direct_answer"))
    findings_usable = _module_list_is_usable(cleaned.get("key_findings"))
    actions_usable = _module_list_is_usable(cleaned.get("recommended_actions"))

    direct_answer_preserved = False
    findings_replaced = False
    actions_replaced = False
    whole_module_replaced = False
    reason = "module_usable"

    if module_is_usable(cleaned):
        final_module = cleaned
        direct_answer_preserved = True
    elif direct_answer_usable:
        final_module = {
            **cleaned,
            "status": "fallback",
            "summary": cleaned["direct_answer"],
            "key_findings": fallback["key_findings"],
            "recommended_actions": fallback["recommended_actions"],
            "confidence": "Low",
            "evidence_note": FALLBACK_EVIDENCE_NOTE,
            "module_llm_status": "fallback_polished",
            "fallback_reason": "module_not_usable",
        }
        direct_answer_preserved = True
        findings_replaced = True
        actions_replaced = True
        reason = "findings_or_actions_not_usable"
    else:
        final_module = fallback
        findings_replaced = True
        actions_replaced = True
        whole_module_replaced = True
        reason = "module_not_usable"

    log_json(
        level="INFO",
        event="insight_module_finalized",
        service="api",
        request_id=module_result.get("_finalize_request_id"),
        tenant_id=module_result.get("_finalize_tenant_id"),
        doc_id_filter=module_result.get("_finalize_doc_id_filter"),
        module_key=module_key,
        normalized_module_key=normalized_key,
        original_status=original_status,
        final_status=final_module.get("status"),
        direct_answer_preserved=direct_answer_preserved,
        findings_replaced=findings_replaced,
        actions_replaced=actions_replaced,
        whole_module_replaced=whole_module_replaced,
        reason=reason,
    )
    return final_module

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
        or "503" in detail
        or "resource_exhausted" in detail
        or "rate limit" in detail
        or "quota" in detail
        or "unavailable" in detail
        or "deadline" in detail
        or "timeout" in detail
        or "temporarily" in detail
        or "try again" in detail
        or "internal" in detail
    )


def generate_content_with_retry(
    gemini: genai.Client,
    model_name: str,
    full_prompt: str,
    request_id: str,
    tenant_id: str,
    retrieved_k: int,
    *,
    max_attempts: int = ASK_MAX_GENERATION_ATTEMPTS,
    base_backoff_seconds: float = ASK_RETRY_BASE_BACKOFF_SECONDS,
) -> Any:
    last_error: genai_errors.ClientError | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return gemini.models.generate_content(
                model=model_name,
                contents=full_prompt,
            )
        except genai_errors.ClientError as error:
            last_error = error
            retryable = is_retryable_generation_error(error)
            if not retryable or attempt >= max_attempts:
                raise

            backoff_seconds = base_backoff_seconds * (2 ** (attempt - 1))
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

def build_insight_module_prompt(
    module_title: str,
    retrieval_query: str,
    context_block: str,
) -> str:
    return (
        "You are a senior business analyst writing for an executive audience.\n"
        "Use the provided document excerpts as primary evidence.\n"
        "When citing evidence, use citation tags like [doc_id:chunk_id].\n"
        "If evidence is limited or indirect, still provide a best-effort executive analysis.\n"
        "Infer plausible items from context and label them with 'Confidence: Low' or 'Inferred'.\n"
        "NEVER say 'the document does not mention' or 'no information available'.\n\n"
        f"Insight module: {module_title}\n"
        f"Analysis focus: {retrieval_query}\n\n"
        f"Document excerpts:\n{context_block}\n\n"
        "Return ONLY valid JSON with keys:\n"
        "- summary: concise executive summary with citations\n"
        "- direct_answer: direct executive answer to this module with citations\n"
        "- key_findings: 2-4 specific findings, each with citations where available\n"
        "- recommended_actions: 2-4 specific actions tied to findings"
    )

def generate_insight_module(
    gemini: genai.Client,
    model_name: str,
    tenant_id: str,
    request_id: str,
    doc_id: str,
    top_k: int,
    module_def: dict[str, str],
) -> dict[str, Any]:
    module_id = module_def["module_id"]
    title = module_def["title"]
    retrieval_query = module_def["retrieval_query"]

    matches = retrieve_matches(
        tenant_id=tenant_id,
        query_text=retrieval_query,
        top_k=top_k,
        doc_id=doc_id,
        request_id=request_id,
    )
    citations = [m["citation"] for m in matches]
    fallback = build_category_fallback(
        normalize_module_key(module_id, title),
        {"module_id": module_id, "title": title, "citations": citations, "matches": matches},
    )

    if not matches:
        return {
            **fallback,
            "status": "no_evidence",
            "error": None,
        }

    snippets, _ = build_context_snippets(matches, max_chunk_chars=520, max_total_chars=2200)
    context_block = "\n\n---\n\n".join(snippets)
    full_prompt = build_insight_module_prompt(title, retrieval_query, context_block)

    try:
        resp = generate_content_with_retry(
            gemini=gemini,
            model_name=model_name,
            full_prompt=full_prompt,
            request_id=request_id,
            tenant_id=tenant_id,
            retrieved_k=len(matches),
            max_attempts=INSIGHT_MAX_GENERATION_ATTEMPTS,
            base_backoff_seconds=INSIGHT_RETRY_BASE_BACKOFF_SECONDS,
        )
        model_text = getattr(resp, "text", None)
        if model_text is None and isinstance(resp, dict):
            model_text = resp.get("text")
        if not model_text:
            model_text = ""

        parsed_brief: InsightModuleBrief | None = None
        brief_obj = parse_model_json(model_text)
        if brief_obj is not None:
            try:
                parsed_brief = InsightModuleBrief.model_validate(brief_obj)
            except Exception:
                parsed_brief = None

        if parsed_brief is not None:
            final_brief = {
                "summary": parsed_brief.summary,
                "direct_answer": parsed_brief.direct_answer,
                "key_findings": parsed_brief.key_findings,
                "recommended_actions": parsed_brief.recommended_actions,
                "status": "ok",
            }
        else:
            final_brief = {**fallback, "status": "fallback", "error": None}
        return {
            "module_id": module_id,
            "title": title,
            "summary": final_brief["summary"],
            "direct_answer": final_brief["direct_answer"],
            "key_findings": final_brief["key_findings"],
            "recommended_actions": final_brief["recommended_actions"],
            "citations": citations,
            "matches": matches,
            "status": final_brief["status"],
            "error": final_brief.get("error"),
        }
    except Exception as error:
        log_json(
            level="ERROR",
            event="insight_module_failed",
            service="api",
            request_id=request_id,
            tenant_id=tenant_id,
            doc_id=doc_id,
            module_id=module_id,
            module_title=title,
            error=type(error).__name__,
            detail=str(error)[:500],
        )
        return {
            **fallback,
            "status": "error",
            "error": f"Module generation failed: {type(error).__name__}",
        }


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
    allow_origins=_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)





@app.get("/health")
def health():
    return {"ok": True, "service": "api"}


@app.get("/v1/auth/config")
def auth_config():
    return {
        "ok": True,
        "service": "api",
        "client_test_auth_enabled": client_auth.client_test_auth_enabled(),
    }


class ClientLoginRequest(BaseModel):
    access_key: str = Field(..., min_length=1)


@app.post("/v1/auth/login")
def auth_login(req: ClientLoginRequest):
    if not client_auth.client_test_auth_enabled():
        raise HTTPException(
            status_code=400,
            detail="Client test keys are not configured on the server (open demo mode).",
        )
    principal = client_auth.resolve_principal(req.access_key.strip())
    if not principal:
        raise HTTPException(status_code=401, detail="Invalid access key.")
    return {
        "ok": True,
        "service": "api",
        "tenant_id": principal.tenant_id,
        "workspace_id": principal.workspace_id,
        "display_label": principal.display_label,
    }


@app.get("/v1/auth/session")
def auth_session(request: Request):
    tenant_id = get_tenant_id(request)
    label = getattr(request.state, "client_display_label", None) or tenant_id
    return {
        "ok": True,
        "service": "api",
        "tenant_id": tenant_id,
        "workspace_id": getattr(request.state, "workspace_id", tenant_id),
        "display_label": label,
    }


@app.post("/v1/demo")
async def demo(req: DemoRequest, request: Request):
     
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)

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


def _neutral_insight_unavailable_copy() -> str:
    return (
        "This perspective could not be assembled from your document for this run. "
        "Other diagnosis tabs or follow-up Q&A may still help."
    )


def _insight_module_for_response(module: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in module.items() if key != "error"}


@app.post("/v1/insights")
def generate_insights(req: InsightsRequest, request: Request):
    request_id = request.state.request_id
    tenant_id = get_tenant_id(request)
    doc_id = req.doc_id.strip()

    if not doc_id:
        raise HTTPException(status_code=400, detail="doc_id is required")

    doc_path = get_doc_path(tenant_id, doc_id)
    if not doc_path.exists():
        raise HTTPException(status_code=404, detail="Document not found")

    effective_top_k = max(1, min(req.top_k, INSIGHT_TOP_K_CAP))
    model_name = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.5-flash")
    gemini = get_gemini_client()

    module_results: list[dict[str, Any]] = []
    success_count = 0
    neutral_copy = _neutral_insight_unavailable_copy()

    for mod_index, module in enumerate(INSIGHT_MODULE_DEFINITIONS):
        module_id = module["module_id"]
        module_title = module["title"]
        retrieval_query = module["retrieval_query"]

        try:
            matches = retrieve_matches(
                tenant_id=tenant_id,
                query_text=retrieval_query,
                top_k=effective_top_k,
                doc_id=doc_id,
                request_id=request_id,
            )
        except Exception as error:
            log_json(
                level="ERROR",
                event="insight_module_retrieval_failed",
                service="api",
                request_id=request_id,
                tenant_id=tenant_id,
                doc_id=doc_id,
                module_id=module_id,
                module_title=module_title,
                doc_id_filter=doc_id,
                top_k=effective_top_k,
                detail=str(error)[:500],
            )
            module_results.append(
                {
                    "module_id": module_id,
                    "title": module_title,
                    "status": "failed",
                    "error": f"Retrieval failed: {str(error)[:500]}",
                    "summary": neutral_copy,
                    "direct_answer": neutral_copy,
                    "key_findings": [
                        "If this keeps happening, try a smaller file or retry once the workspace is less busy."
                    ],
                    "recommended_actions": [
                        "Regenerate the diagnosis after upload completes, or ask a targeted follow-up question in Q&A.",
                    ],
                    "citations": [],
                    "matches": [],
                }
            )
            log_json(
                level="INFO",
                event="insight_module_status",
                service="api",
                request_id=request_id,
                tenant_id=tenant_id,
                doc_id=doc_id,
                module_id=module_id,
                module_llm_status="failed",
                retrieved_k=0,
                doc_id_filter=doc_id,
                top_k=effective_top_k,
            )
            if mod_index < len(INSIGHT_MODULE_DEFINITIONS) - 1:
                time.sleep(INSIGHT_MODULE_PAUSE_SECONDS)
            continue

        citations = [m["citation"] for m in matches]
        fallback_module = build_category_fallback(
            normalize_module_key(module_id, module_title),
            {"module_id": module_id, "title": module_title, "citations": citations, "matches": matches},
        )

        if not matches:
            module_results.append(
                {
                    "module_id": module_id,
                    "title": module_title,
                    "status": "insufficient_evidence",
                    "summary": fallback_module["summary"],
                    "direct_answer": fallback_module["direct_answer"],
                    "key_findings": fallback_module["key_findings"],
                    "recommended_actions": fallback_module["recommended_actions"],
                    "citations": citations,
                    "matches": matches,
                    "confidence": fallback_module["confidence"],
                    "evidence_note": fallback_module["evidence_note"],
                    "module_llm_status": "fallback_polished",
                    "fallback_reason": "module_not_usable",
                }
            )
            success_count += 1
            log_json(
                level="INFO",
                event="insight_module_status",
                service="api",
                request_id=request_id,
                tenant_id=tenant_id,
                doc_id=doc_id,
                module_id=module_id,
                module_llm_status="insufficient_evidence",
                retrieved_k=0,
                doc_id_filter=doc_id,
                top_k=effective_top_k,
            )
            if mod_index < len(INSIGHT_MODULE_DEFINITIONS) - 1:
                time.sleep(INSIGHT_MODULE_PAUSE_SECONDS)
            continue

        snippets, context_chars = build_context_snippets(
            matches,
            max_chunk_chars=INSIGHT_MAX_CHUNK_CHARS,
            max_total_chars=INSIGHT_MAX_CONTEXT_CHARS,
        )
        context_block = "\n\n---\n\n".join(snippets)

        module_instruction = (
            "You are a senior business diagnostics analyst writing for an executive audience.\n"
            "Return ONLY valid JSON with keys: summary, direct_answer, key_findings, recommended_actions.\n"
            "Rules:\n"
            "- summary: one concise diagnosis sentence grounded in evidence with citations [doc_id:chunk_id]\n"
            "- direct_answer: direct executive-level answer for this module with citations [doc_id:chunk_id]\n"
            "- key_findings: 2-4 specific findings as COMPLETE, READABLE sentences with citations\n"
            "- recommended_actions: 2-4 concrete, non-generic business actions tied to findings\n"
            "- If evidence is limited or indirect, still provide a best-effort executive analysis.\n"
            "  Infer plausible items from context and label them with 'Confidence: Low'.\n"
            "- NEVER say 'the document does not mention' or 'no information available'.\n"
            "- NEVER copy raw text fragments. Always rewrite source text as clean executive prose.\n"
            "- NEVER include broken sentences, unexplained abbreviations, or repeated words like 'Medium Medium'.\n"
            "- Each finding must be a complete, grammatically correct sentence that an executive can read."
        )
        module_prompt = (
            f"Module: {module_title}\n"
            f"Diagnostic goal: {retrieval_query}\n\n"
            f"Document excerpts:\n{context_block}\n\n"
            "Generate the JSON response now."
        )
        full_prompt = f"{module_instruction}\n\n{module_prompt}"

        try:
            resp = generate_content_with_retry(
                gemini=gemini,
                model_name=model_name,
                full_prompt=full_prompt,
                request_id=request_id,
                tenant_id=tenant_id,
                retrieved_k=len(matches),
                max_attempts=INSIGHT_MAX_GENERATION_ATTEMPTS,
                base_backoff_seconds=INSIGHT_RETRY_BASE_BACKOFF_SECONDS,
            )
            raw_text = getattr(resp, "text", None)
            if raw_text is None and isinstance(resp, dict):
                raw_text = resp.get("text")
            raw_text = raw_text or ""

            parsed_obj = parse_model_json(raw_text)
            parsed_brief = None
            if parsed_obj is not None:
                try:
                    parsed_brief = InsightModuleBrief.model_validate(parsed_obj)
                except Exception:
                    parsed_brief = None

            if parsed_brief is not None:
                module_status = "ok"
                llm_status_log = "generated"
                fallback_reason = None
                module_payload = {
                    "summary": parsed_brief.summary,
                    "direct_answer": parsed_brief.direct_answer,
                    "key_findings": parsed_brief.key_findings,
                    "recommended_actions": parsed_brief.recommended_actions,
                }
            else:
                module_status = "fallback"
                llm_status_log = "fallback_polished"
                fallback_reason = "json_parse_failed"
                module_payload = {
                    "summary": fallback_module["summary"],
                    "direct_answer": fallback_module["direct_answer"],
                    "key_findings": fallback_module["key_findings"],
                    "recommended_actions": fallback_module["recommended_actions"],
                    "confidence": fallback_module["confidence"],
                    "evidence_note": fallback_module["evidence_note"],
                    "module_llm_status": "fallback_polished",
                    "fallback_reason": "module_not_usable",
                }

            module_results.append(
                {
                    "module_id": module_id,
                    "title": module_title,
                    "status": module_status,
                    **module_payload,
                    "citations": citations,
                    "matches": matches,
                }
            )
            success_count += 1
            log_json(
                level="INFO",
                event="insight_module_status",
                service="api",
                request_id=request_id,
                tenant_id=tenant_id,
                doc_id=doc_id,
                module_id=module_id,
                module_llm_status=llm_status_log,
                fallback_reason=fallback_reason,
                retrieved_k=len(matches),
                doc_id_filter=doc_id,
                top_k=effective_top_k,
                context_chars=context_chars,
                llm_model=model_name,
            )
        except Exception as error:
            log_json(
                level="ERROR",
                event="insight_module_generation_failed",
                service="api",
                request_id=request_id,
                tenant_id=tenant_id,
                doc_id=doc_id,
                module_id=module_id,
                module_title=module_title,
                doc_id_filter=doc_id,
                top_k=effective_top_k,
                retrieved_k=len(matches),
                error=type(error).__name__,
                detail=str(error)[:500],
            )
            module_results.append(
                {
                    "module_id": module_id,
                    "title": module_title,
                    "status": "grounded",
                    "error": f"Generation failed: {type(error).__name__}",
                    "summary": fallback_module["summary"],
                    "direct_answer": fallback_module["direct_answer"],
                    "key_findings": fallback_module["key_findings"],
                    "recommended_actions": fallback_module["recommended_actions"],
                    "citations": citations,
                    "matches": matches,
                    "confidence": fallback_module["confidence"],
                    "evidence_note": fallback_module["evidence_note"],
                    "module_llm_status": "fallback_polished",
                    "fallback_reason": "module_not_usable",
                }
            )
            success_count += 1
            log_json(
                level="INFO",
                event="insight_module_status",
                service="api",
                request_id=request_id,
                tenant_id=tenant_id,
                doc_id=doc_id,
                module_id=module_id,
                module_llm_status="fallback_polished",
                fallback_reason="llm_failed",
                retrieved_k=len(matches),
                doc_id_filter=doc_id,
                top_k=effective_top_k,
                llm_model=model_name,
            )

        if mod_index < len(INSIGHT_MODULE_DEFINITIONS) - 1:
            time.sleep(INSIGHT_MODULE_PAUSE_SECONDS)

    log_json(
        level="INFO",
        event="insights_generated",
        service="api",
        request_id=request_id,
        tenant_id=tenant_id,
        doc_id=doc_id,
        doc_id_filter=doc_id,
        module_count=len(module_results),
        successful_modules=success_count,
        llm_model=model_name,
    )

    finalized_modules: list[dict[str, Any]] = []
    for m in module_results:
        m["_finalize_request_id"] = request_id
        m["_finalize_tenant_id"] = tenant_id
        m["_finalize_doc_id_filter"] = doc_id
        finalized = finalize_insight_module(m.get("module_id", ""), m)
        finalized_modules.append(normalize_module_visible_text(finalized))

    client_modules = [_insight_module_for_response(m) for m in finalized_modules]

    return {
        "ok": True,
        "service": "api",
        "request_id": request_id,
        "tenant_id": tenant_id,
        "doc_id": doc_id,
        "module_count": len(client_modules),
        "successful_modules": success_count,
        "modules": client_modules,
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
        "You are Apalym, an executive business advisor for SMB operators.\n"
        "Use the uploaded document excerpts as your grounding context.\n"
        "When the document directly supports a claim, cite it with [doc_id:chunk_id].\n\n"
        "If the user asks for implementation guidance, next steps, strategy, prioritization,\n"
        "or 'how do I do this', you MAY provide reasonable business recommendations that go\n"
        "beyond the literal document text. Clearly separate source-backed facts (with citations)\n"
        "from your advisor recommendations.\n\n"
        "NEVER say 'the document does not mention this' or 'I cannot find specific instructions'\n"
        "as the main answer. If evidence is limited, say something like:\n"
        "'The document does not provide a full implementation plan, but based on the issues\n"
        "it identifies, here is a practical approach:'\n\n"
        "Keep every answer useful, executive, and action-oriented."
    )

    user_prompt = (
        f"Question: {query_text}\n\n"
        f"Document excerpts:\n{context_block}\n\n"
        "Provide a helpful executive answer. For 'how' questions, give a practical plan.\n"
        "Cite document evidence where available. Add advisor recommendations beyond the doc if useful."
    )

    brief_instruction = (
        "Return ONLY valid JSON with keys: direct_answer, key_findings, recommended_next_actions, confidence, evidence_note.\n"
        "Rules:\n"
        "- direct_answer: one concise, helpful executive answer. Cite with [doc_id:chunk_id] where applicable.\n"
        "  NEVER start with 'I cannot find' or 'The document does not provide'.\n"
        "- key_findings: 2-4 bullet points. Source-backed observations should include citations.\n"
        "  Advisory points that go beyond the document are welcome and should be clearly phrased as recommendations.\n"
        "- recommended_next_actions: 2-4 specific, practical, implementation-ready steps.\n"
        "  These should directly answer the user's question. If they asked 'how do I do this', answer with concrete steps.\n"
        "  Do NOT output generic filler like 'validate answer', 'ask follow-up', or 'review sources'.\n"
        "- confidence: 'High' if answer is well-supported by document, 'Medium' if partially supported, 'Low' if mostly advisory.\n"
        "- evidence_note: one sentence explaining what is directly supported by the document vs inferred/advised.\n"
        "- Return ONLY valid JSON, no markdown fences."
    )

    # 3) Generate answer (Gemini)
    gemini = get_gemini_client()
    model_name = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.5-flash")

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
    except Exception as e:
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
            error=type(e).__name__,
            detail=msg[:500],
            retrieved_k=len(matches),
            context_chars=context_chars,
            top_k=effective_top_k,
            doc_id_filter=req.doc_id,
            token_usage=None,
            answer_mode="extractive",
        )
        extractive_findings: list[str] = []
        for m in matches[:4]:
            raw = (m.get("text") or "").strip()
            if not raw:
                continue
            cleaned = _clean_module_text(raw)
            if not cleaned or contains_raw_output_pattern(cleaned):
                continue
            txt = trim_snippet_text(cleaned, 220)
            if txt and not contains_raw_output_pattern(txt):
                md = m.get("metadata") or {}
                tag = f"[{md.get('doc_id')}:{md.get('chunk_id')}]"
                extractive_findings.append(f"{txt} {tag}")

        if not extractive_findings:
            extractive_findings = [
                "The available source excerpts could not be cleanly summarized. Try rephrasing or asking about a specific aspect of the document."
            ]

        direct_line = (
            "The AI analysis could not be completed for this question, but the following "
            "points are drawn from retrieved excerpts in your document."
        )
        fallback_brief = AskBrief(
            direct_answer=direct_line,
            key_findings=extractive_findings[:4],
            recommended_next_actions=build_dynamic_fallback_actions(query_text, matches)[:4],
            confidence="Low",
            evidence_note="This is a source-grounded fallback. AI generation was unavailable for this request.",
        )
        fallback_brief.direct_answer = normalize_visible_citations(fallback_brief.direct_answer, citations)
        fallback_brief.key_findings = [
            normalize_visible_citations(finding, citations) for finding in fallback_brief.key_findings
        ]
        fallback_brief.recommended_next_actions = [
            normalize_visible_citations(action, citations) for action in fallback_brief.recommended_next_actions
        ]
        if fallback_brief.evidence_note:
            fallback_brief.evidence_note = normalize_visible_citations(fallback_brief.evidence_note, citations)

        return {
            "ok": True,
            "service": "api",
            "request_id": request_id,
            "tenant_id": tenant_id,
            "query": req.query,
            "answer": fallback_brief.direct_answer,
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

    if not final_brief.confidence and parsed_brief and parsed_brief.confidence:
        final_brief.confidence = parsed_brief.confidence
    if not final_brief.evidence_note and parsed_brief and parsed_brief.evidence_note:
        final_brief.evidence_note = parsed_brief.evidence_note

    final_brief.direct_answer = normalize_visible_citations(final_brief.direct_answer, citations)
    final_brief.key_findings = [
        normalize_visible_citations(finding, citations) for finding in final_brief.key_findings
    ]
    final_brief.recommended_next_actions = [
        normalize_visible_citations(action, citations) for action in final_brief.recommended_next_actions
    ]
    if final_brief.evidence_note:
        final_brief.evidence_note = normalize_visible_citations(final_brief.evidence_note, citations)

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
        "answer_mode": "advisor",
        "citations": citations,
        "matches": matches,  # keep for debugging/UI “sources panel”
    }

    response_payload["brief"] = {
        "direct_answer": final_brief.direct_answer,
        "key_findings": final_brief.key_findings,
        "recommended_next_actions": final_brief.recommended_next_actions,
        "confidence": final_brief.confidence,
        "evidence_note": final_brief.evidence_note,
    }

    return response_payload

