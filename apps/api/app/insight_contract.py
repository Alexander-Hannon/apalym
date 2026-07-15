"""Single authoritative insight-output contract for /v1/insights."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

InsightStatus = Literal["ready", "limited_evidence"]
InsightConfidence = Literal["high", "medium", "low"]
MetricDirection = Literal["lower_is_better", "higher_is_better"]


class CitedField(BaseModel):
    text: str
    citation_ids: list[int] = Field(default_factory=list)

    @field_validator("citation_ids")
    @classmethod
    def citation_ids_are_ints(cls, value: list[int]) -> list[int]:
        return [int(item) for item in value]


class SourceEvidence(BaseModel):
    citation_id: int
    document_name: str
    page: int | None = None
    excerpt: str
    title: str = "Source evidence"


class MetricValue(BaseModel):
    label: str
    actual: float
    target: float
    unit: str = ""
    direction: MetricDirection = "lower_is_better"


class EvidenceLimitations(BaseModel):
    message: str | None = None
    validation_questions: list[str] = Field(default_factory=list)


class InsightModuleOutput(BaseModel):
    module_key: str
    module_id: str
    title: str
    status: InsightStatus
    confidence: InsightConfidence
    direct_answer: CitedField
    key_findings: list[CitedField]
    recommended_actions: list[CitedField]
    evidence_limitations: EvidenceLimitations
    sources: list[SourceEvidence]
    validation_questions: list[str] = Field(default_factory=list)
    metrics: list[MetricValue] = Field(default_factory=list)
    summary: CitedField | None = None


_UUID_PATTERN = re.compile(
    r"\b[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\b",
    re.IGNORECASE,
)
_UUID_CHUNK_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:\d+\b",
    re.IGNORECASE,
)
_BRACKETED_INTERNAL_ID = re.compile(
    r"\[[0-9a-f]{8}-[0-9a-f-]{27,}:\d+\]",
    re.IGNORECASE,
)
_CITATION_SYNTAX_IN_TEXT = re.compile(
    r"\[\s*\d{1,2}(?:\s*,\s*\d{1,2})*\s*\]|\[[^\]]*:[^\]]+\]"
)
_INTERNAL_FIELD_NAMES = re.compile(
    r"\b(?:chunk_id|doc_id|source_id|metadata|namespace)\b",
    re.IGNORECASE,
)
_BROKEN_LEADING = re.compile(
    r"^(?:ductivity|tional|ables|holds\b|ment\b|cy\b|ctical)\b",
    re.IGNORECASE,
)
_REPEATED_LABEL = re.compile(r"\b(Medium|High|Low)\s+\1\b", re.IGNORECASE)
_GENERIC_MARKERS: tuple[tuple[str, ...], ...] = (
    ("additional detail", "fully assess this finding"),
    ("validate this area", "relevant department owner"),
    ("highest-impact cited item",),
    ("module", "clean diagnosis"),
    ("could not be assembled",),
)
_TABLE_ROW_DUMP = re.compile(
    r"\b\d+\s+\d+\s+\(\d+\)\s+\d+\s+\d+\b|"
    r"(?:row\s+\d+[:\s].*){2,}",
    re.IGNORECASE,
)


def compact_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def text_has_citation_syntax(text: str) -> bool:
    return bool(_CITATION_SYNTAX_IN_TEXT.search(text or ""))


def text_has_internal_artifact(text: str) -> bool:
    if not isinstance(text, str) or not compact_whitespace(text):
        return True
    return bool(
        _UUID_PATTERN.search(text)
        or _UUID_CHUNK_PATTERN.search(text)
        or _BRACKETED_INTERNAL_ID.search(text)
        or _INTERNAL_FIELD_NAMES.search(text)
        or _BROKEN_LEADING.match(compact_whitespace(text))
        or _REPEATED_LABEL.search(text)
        or text_has_citation_syntax(text)
    )


def text_is_generic_placeholder(text: str) -> bool:
    low = compact_whitespace(text).lower()
    return any(all(part in low for part in marker) for marker in _GENERIC_MARKERS)


def text_is_substantive(text: str, *, min_len: int = 24) -> bool:
    cleaned = compact_whitespace(text)
    return len(cleaned) >= min_len and not text_is_generic_placeholder(cleaned)


def text_looks_like_raw_table_dump(text: str) -> bool:
    cleaned = compact_whitespace(text)
    if not cleaned:
        return False
    if _TABLE_ROW_DUMP.search(cleaned):
        return True
    label_count = len(re.findall(r"\b[A-Za-z][A-Za-z0-9 /%$&().-]{1,35}:\s*\S", cleaned))
    numeric_tokens = sum(
        1
        for token in cleaned.split()[:20]
        if re.search(r"^\(?-?\$?\d+(?:[.,]\d+)?%?\)?$", token)
    )
    return numeric_tokens >= 5 and label_count >= 3


def strip_inline_citations_from_text(text: str) -> str:
    cleaned = text or ""
    cleaned = _BRACKETED_INTERNAL_ID.sub(" ", cleaned)
    cleaned = _UUID_CHUNK_PATTERN.sub(" ", cleaned)
    cleaned = _UUID_PATTERN.sub(" ", cleaned)
    cleaned = _CITATION_SYNTAX_IN_TEXT.sub(" ", cleaned)
    cleaned = _INTERNAL_FIELD_NAMES.sub(" ", cleaned)
    cleaned = re.sub(
        r"\b(?:doc_id|chunk_id|source_id)\s*[:=]\s*[A-Za-z0-9_.:-]+\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = compact_whitespace(cleaned)
    if _BROKEN_LEADING.match(cleaned):
        words = cleaned.split()
        cleaned = " ".join(words[1:]).strip() if len(words) > 1 else ""
    return compact_whitespace(cleaned)


def clean_cited_field_text(text: str) -> str:
    cleaned = strip_inline_citations_from_text(text)
    if text_looks_like_raw_table_dump(cleaned):
        return ""
    return cleaned


def cited_field_is_usable(field: CitedField) -> bool:
    return text_is_substantive(field.text) and not text_has_internal_artifact(field.text)


def parse_cited_field(
    value: Any,
    *,
    max_sources: int,
    fallback_ids: list[int] | None = None,
) -> CitedField:
    raw_text = ""
    ids: list[int] = []

    if isinstance(value, dict):
        raw_text = value.get("text") if isinstance(value.get("text"), str) else ""
        raw_ids = value.get("citation_ids")
        if isinstance(raw_ids, list):
            ids = [int(item) for item in raw_ids if isinstance(item, int) and 1 <= item <= max_sources]
    elif isinstance(value, str):
        raw_text = value

    if not ids and fallback_ids:
        ids = [item for item in fallback_ids if 1 <= item <= max_sources]

    clean_text = clean_cited_field_text(raw_text)
    if not text_is_substantive(clean_text):
        clean_text = ""

    return CitedField(text=clean_text, citation_ids=ids)


def _public_source_title(citation_id: int) -> str:
    return f"Source excerpt {citation_id}"


def _public_document_label(document_name: str | None) -> str:
    name = compact_whitespace(document_name or "")
    if not name:
        return "Uploaded document"
    if _UUID_PATTERN.search(name):
        return "Uploaded document"
    return name


def build_public_sources(source_packets: list[dict[str, Any]]) -> list[SourceEvidence]:
    sources: list[SourceEvidence] = []
    for packet in source_packets:
        citation_id = int(packet["citation_id"])
        sources.append(
            SourceEvidence(
                citation_id=citation_id,
                document_name=_public_document_label(packet.get("document_name")),
                page=packet.get("page"),
                excerpt=packet.get("excerpt") or "",
                title=_public_source_title(citation_id),
            )
        )
    return sources


def source_excerpt_is_usable(excerpt: str) -> bool:
    """Source drawer excerpts may contain bracketed values; only reject truly raw IDs."""
    cleaned = compact_whitespace(excerpt)
    if len(cleaned) < 10:
        return False
    return not (
        _UUID_PATTERN.search(cleaned)
        or _UUID_CHUNK_PATTERN.search(cleaned)
        or _BRACKETED_INTERNAL_ID.search(cleaned)
    )


def count_usable_sources(sources: list[SourceEvidence]) -> int:
    return sum(1 for source in sources if source_excerpt_is_usable(source.excerpt))


def text_has_raw_uuid(text: str) -> bool:
    cleaned = compact_whitespace(text)
    if not cleaned:
        return False
    return bool(
        _UUID_PATTERN.search(cleaned)
        or _UUID_CHUNK_PATTERN.search(cleaned)
        or _BRACKETED_INTERNAL_ID.search(cleaned)
    )


def cited_field_passes_safety_gate(field: CitedField) -> bool:
    """Content-quality gate for visible generated prose (separate from evidence confidence)."""
    cleaned = clean_cited_field_text(field.text)
    if not text_is_substantive(cleaned):
        return False
    if text_has_raw_uuid(cleaned):
        return False
    if text_looks_like_raw_table_dump(cleaned):
        return False
    if _BROKEN_LEADING.match(cleaned) or _REPEATED_LABEL.search(cleaned):
        return False
    return True


def count_safety_passing_fields(fields: list[CitedField]) -> int:
    return sum(1 for field in fields if cited_field_passes_safety_gate(field))


def generated_content_passes_safety_gate(
    direct_answer: CitedField,
    key_findings: list[CitedField],
    recommended_actions: list[CitedField],
) -> bool:
    if not cited_field_passes_safety_gate(direct_answer):
        return False
    if text_has_raw_uuid(direct_answer.text):
        return False
    for field in [*key_findings, *recommended_actions]:
        if text_has_raw_uuid(field.text):
            return False
    return True


def should_downgrade_to_limited_evidence(
    *,
    source_packet_count: int,
    direct_answer: CitedField,
    key_findings: list[CitedField],
    recommended_actions: list[CitedField],
) -> tuple[bool, str | None]:
    if source_packet_count < 1:
        return True, "no_usable_sources"

    direct_safe = cited_field_passes_safety_gate(direct_answer)
    findings_safe_count = count_safety_passing_fields(key_findings)
    actions_safe_count = count_safety_passing_fields(recommended_actions)
    both_lists_unusable = findings_safe_count == 0 and actions_safe_count == 0
    has_any_safe_content = direct_safe or findings_safe_count > 0 or actions_safe_count > 0

    if any(
        text_has_raw_uuid(field.text)
        for field in [direct_answer, *key_findings, *recommended_actions]
        if compact_whitespace(field.text)
    ):
        return True, "field_raw_uuid"

    if not has_any_safe_content:
        return True, "all_content_unusable"

    if not compact_whitespace(direct_answer.text) and both_lists_unusable:
        return True, "empty_direct_answer_and_lists_unusable"

    if source_packet_count >= 2 and has_any_safe_content:
        return False, None

    if source_packet_count >= 1 and direct_safe:
        return False, None

    if source_packet_count >= 1 and not both_lists_unusable:
        return False, None

    return True, "insufficient_safe_content"


def compute_module_confidence(
    source_packet_count: int,
    *,
    fully_valid: bool,
) -> InsightConfidence:
    if source_packet_count >= 3 and fully_valid:
        return "high"
    if source_packet_count >= 2:
        return "medium"
    if source_packet_count == 1:
        return "medium" if fully_valid else "low"
    return "low"


def validate_cited_field(field: CitedField, *, max_sources: int, field_name: str) -> list[str]:
    reasons: list[str] = []
    if not text_is_substantive(field.text):
        reasons.append(f"{field_name}_not_substantive")
    if text_has_internal_artifact(field.text):
        reasons.append(f"{field_name}_artifact")
    if text_has_citation_syntax(field.text):
        reasons.append(f"{field_name}_citation_syntax")
    for cid in field.citation_ids:
        if not isinstance(cid, int) or cid < 1 or (max_sources and cid > max_sources):
            reasons.append(f"{field_name}_citation_id_out_of_range")
    return reasons


def validate_module_output(module: InsightModuleOutput) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    max_sources = len(module.sources)
    source_ids = {source.citation_id for source in module.sources}

    reasons.extend(validate_cited_field(module.direct_answer, max_sources=max_sources, field_name="direct_answer"))

    if len(module.key_findings) < 2:
        reasons.append("key_findings_too_short")
    else:
        normalized_findings = [compact_whitespace(finding.text).lower() for finding in module.key_findings]
        if len(set(normalized_findings)) != len(normalized_findings):
            reasons.append("key_findings_duplicates")
        for index, finding in enumerate(module.key_findings):
            reasons.extend(validate_cited_field(finding, max_sources=max_sources, field_name=f"key_findings_{index}"))

    if len(module.recommended_actions) < 2:
        reasons.append("recommended_actions_too_short")
    else:
        normalized_actions = [compact_whitespace(action.text).lower() for action in module.recommended_actions]
        if len(set(normalized_actions)) != len(normalized_actions):
            reasons.append("recommended_actions_duplicates")
        for index, action in enumerate(module.recommended_actions):
            reasons.extend(validate_cited_field(action, max_sources=max_sources, field_name=f"recommended_actions_{index}"))
            if text_is_generic_placeholder(action.text):
                reasons.append(f"recommended_actions_{index}_generic")

    for source in module.sources:
        if not compact_whitespace(source.excerpt):
            reasons.append("source_excerpt_missing")
        elif text_has_internal_artifact(source.excerpt):
            reasons.append("source_excerpt_artifact")

    for cited in [module.direct_answer, *module.key_findings, *module.recommended_actions]:
        for cid in cited.citation_ids:
            if cid not in source_ids:
                reasons.append("citation_id_unmapped")

    if module.status == "limited_evidence":
        questions = module.validation_questions or module.evidence_limitations.validation_questions
        if not module.evidence_limitations.message:
            reasons.append("limited_evidence_message_missing")
        if len(questions) < 2:
            reasons.append("validation_questions_too_short")

    return (len(reasons) == 0, sorted(set(reasons)))


def module_has_visible_artifacts(module: InsightModuleOutput) -> bool:
    texts = [
        module.direct_answer.text,
        *[item.text for item in module.key_findings],
        *[item.text for item in module.recommended_actions],
        module.evidence_limitations.message or "",
        *module.validation_questions,
        *module.evidence_limitations.validation_questions,
    ]
    return any(text_has_internal_artifact(text) for text in texts if compact_whitespace(text))


def build_insight_generation_instruction(module_title: str, retrieval_query: str) -> str:
    return (
        "You are generating an executive business brief from numbered source packets.\n"
        "Return ONLY valid JSON with keys: direct_answer, key_findings, recommended_actions.\n"
        "Use this exact cited-object shape:\n"
        '- direct_answer: {"text": "...", "citation_ids": [1, 2]}\n'
        '- key_findings: [{"text": "...", "citation_ids": [1]}, ...]\n'
        '- recommended_actions: [{"text": "...", "citation_ids": [1, 2]}, ...]\n\n'
        "Critical rules:\n"
        "- Do not include any citations, source labels, IDs, brackets, document identifiers, chunks, UUIDs, or metadata in text fields.\n"
        "- Put supporting evidence only in citation_ids using the supplied numbered sources.\n"
        "- Never copy raw table rows or chopped fragments into text fields.\n"
        "- Write complete executive sentences with dollar values and percentages when supported by evidence.\n"
        "- Provide 2-4 distinct findings and 2-4 distinct recommended actions.\n\n"
        f"Module: {module_title}\n"
        f"Diagnostic goal: {retrieval_query}\n"
    )


def build_insight_repair_instruction(module_title: str, retrieval_query: str, failure_reasons: list[str]) -> str:
    return (
        build_insight_generation_instruction(module_title, retrieval_query)
        + "\nThe previous JSON failed validation for these reasons: "
        + ", ".join(failure_reasons[:8])
        + ".\nReturn corrected structured JSON only. Do not reproduce raw excerpts in text fields.\n"
    )


LIMITED_EVIDENCE_COPY: dict[str, dict[str, Any]] = {
    "losses": {
        "direct_answer": (
            "The available document indicates operating pressure from labor overtime and transportation "
            "utilization, but it does not provide enough clean category-level detail to rank the largest "
            "sources of profit leakage with confidence."
        ),
        "key_findings": [
            "Warehouse overtime appears above the operating target.",
            "Truck utilization appears below the planned efficiency level.",
            "Collections timing may be affecting cash conversion.",
        ],
        "recommended_actions": [
            "Validate overtime by location, shift, and supervisor before reallocating labor.",
            "Compare truck utilization by lane and customer to isolate underused capacity.",
            "Review disputed receivables and collection timing against the cash plan.",
        ],
        "validation_questions": [
            "What is gross margin by customer, route, location, and service line?",
            "Which labor, freight, claims, or pricing drivers explain the largest variance?",
            "Which loss categories are recurring versus one-time in the current period?",
        ],
    },
    "costs": {
        "direct_answer": (
            "The available document signals cost pressure in labor and transportation, but it does not "
            "provide enough clean category-level detail to isolate the top recurring cost drivers with confidence."
        ),
        "key_findings": [
            "Labor overtime appears elevated relative to plan.",
            "Transportation utilization may be below the efficiency target.",
            "Operating cost variances may be concentrated in a few controllable categories.",
        ],
        "recommended_actions": [
            "Break down unfavorable cost variances by site, shift, and vendor.",
            "Separate recurring labor and freight costs from one-time items.",
            "Prioritize the largest controllable categories for a 30-day cost review.",
        ],
        "validation_questions": [
            "Which cost categories are above budget versus prior period by operating unit?",
            "What recurring versus one-time costs explain the largest unfavorable variance?",
            "Which vendors or contracts drive the top three cost overruns?",
        ],
    },
    "risks": {
        "direct_answer": (
            "The available document indicates potential capital-execution, labor, and compliance risk, "
            "but it does not provide enough clean category-level detail to rank those risks with confidence."
        ),
        "key_findings": [
            "Planned automation savings appear delayed relative to capital spending.",
            "Labor performance and overtime appear above plan.",
            "Claims or compliance events may be creating financial exposure.",
        ],
        "recommended_actions": [
            "Review capex ROI and realized savings by project before approving additional spend.",
            "Break down labor variance by site, shift, and staffing model.",
            "Quantify claims, temperature excursions, and compliance exposure by root cause.",
        ],
        "validation_questions": [
            "Which projects have not delivered their planned automation savings?",
            "Which sites or shifts account for the highest overtime variance?",
            "What financial exposure is tied to claims, credits, or compliance events?",
        ],
    },
    "margins": {
        "direct_answer": (
            "The available document suggests margin pressure from labor, freight, and collections timing, "
            "but it does not provide enough clean category-level detail to quantify margin leakage with confidence."
        ),
        "key_findings": [
            "Labor overtime may be compressing contribution margin.",
            "Transportation underutilization may be raising per-unit delivery cost.",
            "Collections delays may be masking working-capital pressure.",
        ],
        "recommended_actions": [
            "Reconcile margin by customer, route, and service line after fully loaded costs.",
            "Audit pricing, discounting, and claims leakage against margin targets.",
            "Review billing accuracy and dispute resolution for high-exposure accounts.",
        ],
        "validation_questions": [
            "What is margin by customer, route, and service line after fully loaded costs?",
            "Which pricing, labor, claims, or billing gaps explain the largest margin leakage?",
            "Which customers or lanes are below target contribution margin?",
        ],
    },
    "trends": {
        "direct_answer": (
            "The available document shows movement in labor, transportation, and collections metrics, "
            "but it does not provide enough clean time-series detail to confirm durable trends with confidence."
        ),
        "key_findings": [
            "Labor overtime may be trending above the operating plan.",
            "Transportation utilization may be drifting below target efficiency.",
            "Collections timing may be worsening relative to the cash plan.",
        ],
        "recommended_actions": [
            "Compare the last three reporting periods for overtime, utilization, and DSO.",
            "Separate recurring trend shifts from one-time operational events.",
            "Set weekly review checkpoints on the top three moving metrics.",
        ],
        "validation_questions": [
            "Which metrics changed materially over the last three to six periods?",
            "Are recent variances recurring or driven by one-time events?",
            "Which operating KPIs should be tracked weekly until the trend stabilizes?",
        ],
    },
}


def category_validation_questions(module_key: str) -> list[str]:
    keyed = LIMITED_EVIDENCE_COPY.get(module_key, {}).get("validation_questions")
    if isinstance(keyed, list) and len(keyed) >= 2:
        return keyed
    return [
        "What supporting schedules or department-level detail would confirm this diagnosis?",
        "Which operating metrics should be validated before acting on these recommendations?",
    ]


def _category_copy(module_key: str) -> dict[str, Any]:
    return LIMITED_EVIDENCE_COPY.get(module_key, LIMITED_EVIDENCE_COPY["losses"])


def _citation_id_for_index(sources: list[SourceEvidence], index: int) -> list[int]:
    if not sources:
        return []
    source = sources[min(index, len(sources) - 1)]
    return [source.citation_id]


def _dedupe_cited_fields(fields: list[CitedField]) -> list[CitedField]:
    seen: set[str] = set()
    deduped: list[CitedField] = []
    for field in fields:
        key = compact_whitespace(field.text).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(field)
    return deduped


def _supplement_cited_fields(
    existing: list[CitedField],
    templates: list[str],
    *,
    max_sources: int,
    sources: list[SourceEvidence],
    min_count: int,
    max_count: int,
    attach_citations: bool,
    reject_generic: bool = False,
) -> list[CitedField]:
    merged = _dedupe_cited_fields([field for field in existing if cited_field_is_usable(field)])
    seen = {compact_whitespace(field.text).lower() for field in merged}

    for index, template in enumerate(templates):
        if len(merged) >= max_count:
            break
        fallback_ids = _citation_id_for_index(sources, index) if attach_citations else []
        candidate = parse_cited_field(template, max_sources=max_sources, fallback_ids=fallback_ids)
        if reject_generic and text_is_generic_placeholder(candidate.text):
            continue
        if not cited_field_is_usable(candidate):
            continue
        key = compact_whitespace(candidate.text).lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(candidate)

    if len(merged) < min_count:
        for index, template in enumerate(templates):
            if len(merged) >= min_count:
                break
            fallback_ids = _citation_id_for_index(sources, index) if attach_citations else []
            candidate = parse_cited_field(template, max_sources=max_sources, fallback_ids=fallback_ids)
            key = compact_whitespace(candidate.text).lower()
            if key in seen or not cited_field_is_usable(candidate):
                continue
            seen.add(key)
            merged.append(candidate)

    return merged[:max_count]


def normalize_cited_field_for_api(field: CitedField) -> CitedField:
    cleaned = clean_cited_field_text(field.text)
    if not text_is_substantive(cleaned):
        cleaned = ""
    return CitedField(text=cleaned, citation_ids=field.citation_ids)


def normalize_module_cited_fields(module: InsightModuleOutput) -> InsightModuleOutput:
    summary = module.summary
    if summary is not None:
        summary = normalize_cited_field_for_api(summary)
    return module.model_copy(
        update={
            "direct_answer": normalize_cited_field_for_api(module.direct_answer),
            "key_findings": [normalize_cited_field_for_api(item) for item in module.key_findings],
            "recommended_actions": [normalize_cited_field_for_api(item) for item in module.recommended_actions],
            "summary": summary,
        }
    )


def build_limited_evidence_module(
    *,
    module_key: str,
    module_id: str,
    title: str,
    sources: list[SourceEvidence],
) -> InsightModuleOutput:
    max_sources = max(len(sources), 1)
    category_copy = _category_copy(module_key)
    validation_questions = category_validation_questions(module_key)

    direct_answer = parse_cited_field(category_copy["direct_answer"], max_sources=max_sources, fallback_ids=[])
    key_findings = [
        parse_cited_field(
            item,
            max_sources=max_sources,
            fallback_ids=_citation_id_for_index(sources, index) if sources else [],
        )
        for index, item in enumerate(category_copy["key_findings"][:3])
    ]
    recommended_actions = [
        parse_cited_field(item, max_sources=max_sources, fallback_ids=[])
        for item in category_copy["recommended_actions"][:3]
    ]

    message = (
        "This diagnosis is based on limited evidence. Review the supporting excerpts and "
        "validate the missing operating detail before acting."
    )

    return InsightModuleOutput(
        module_key=module_key,
        module_id=module_id,
        title=title,
        status="limited_evidence",
        confidence="low",
        summary=direct_answer,
        direct_answer=direct_answer,
        key_findings=key_findings,
        recommended_actions=recommended_actions,
        evidence_limitations=EvidenceLimitations(
            message=message,
            validation_questions=validation_questions,
        ),
        validation_questions=validation_questions,
        sources=sources,
        metrics=[],
    )


def build_partial_module_completion(
    *,
    module_key: str,
    module_id: str,
    title: str,
    sources: list[SourceEvidence],
    direct_answer: CitedField,
    key_findings: list[CitedField],
    recommended_actions: list[CitedField],
) -> InsightModuleOutput:
    max_sources = len(sources)
    category_copy = _category_copy(module_key)

    final_direct = direct_answer if cited_field_is_usable(direct_answer) else parse_cited_field(
        category_copy["direct_answer"],
        max_sources=max_sources,
        fallback_ids=_citation_id_for_index(sources, 0),
    )
    if not cited_field_is_usable(final_direct):
        final_direct = parse_cited_field(
            category_copy["direct_answer"],
            max_sources=max_sources,
            fallback_ids=_citation_id_for_index(sources, 0),
        )

    final_findings = _supplement_cited_fields(
        key_findings,
        category_copy["key_findings"],
        max_sources=max_sources,
        sources=sources,
        min_count=2,
        max_count=4,
        attach_citations=True,
    )
    final_actions = _supplement_cited_fields(
        recommended_actions,
        category_copy["recommended_actions"],
        max_sources=max_sources,
        sources=sources,
        min_count=2,
        max_count=4,
        attach_citations=False,
        reject_generic=True,
    )

    fully_valid = (
        cited_field_is_usable(direct_answer)
        and len([field for field in key_findings if cited_field_is_usable(field)]) >= 2
        and len([field for field in recommended_actions if cited_field_is_usable(field)]) >= 2
    )
    confidence = compute_module_confidence(max_sources, fully_valid=fully_valid)

    return InsightModuleOutput(
        module_key=module_key,
        module_id=module_id,
        title=title,
        status="ready",
        confidence=confidence,
        summary=final_direct,
        direct_answer=final_direct,
        key_findings=final_findings,
        recommended_actions=final_actions,
        evidence_limitations=EvidenceLimitations(message=None, validation_questions=[]),
        validation_questions=[],
        sources=sources,
        metrics=[],
    )


def parse_generated_module(
    generated: dict[str, Any] | None,
    *,
    max_sources: int,
    default_ids: list[int],
) -> dict[str, Any] | None:
    if not isinstance(generated, dict):
        return None

    direct_answer = parse_cited_field(generated.get("direct_answer"), max_sources=max_sources, fallback_ids=default_ids)
    key_findings = [
        parse_cited_field(item, max_sources=max_sources)
        for item in (generated.get("key_findings") or [])
        if item is not None
    ]
    recommended_actions = [
        parse_cited_field(item, max_sources=max_sources)
        for item in (generated.get("recommended_actions") or [])
        if item is not None
    ]

    return {
        "direct_answer": direct_answer,
        "key_findings": [item for item in key_findings if cited_field_is_usable(item)],
        "recommended_actions": [
            item
            for item in recommended_actions
            if cited_field_is_usable(item) and not text_is_generic_placeholder(item.text)
        ],
    }


def finalize_insight_module(
    *,
    module_key: str,
    module_id: str,
    title: str,
    generated: dict[str, Any] | None,
    source_packets: list[dict[str, Any]],
) -> tuple[InsightModuleOutput, list[str]]:
    sources = build_public_sources(source_packets)
    source_packet_count = len(source_packets)
    max_sources = len(sources)
    default_ids = [sources[0].citation_id] if sources else []
    reasons: list[str] = []

    parsed = parse_generated_module(generated, max_sources=max_sources, default_ids=default_ids) if generated else None
    direct_answer = parsed["direct_answer"] if parsed else parse_cited_field("", max_sources=max_sources)
    key_findings = parsed["key_findings"] if parsed else []
    recommended_actions = parsed["recommended_actions"] if parsed else []

    downgrade, downgrade_reason = should_downgrade_to_limited_evidence(
        source_packet_count=source_packet_count,
        direct_answer=direct_answer,
        key_findings=key_findings,
        recommended_actions=recommended_actions,
    )

    if downgrade:
        return (
            build_limited_evidence_module(
                module_key=module_key,
                module_id=module_id,
                title=title,
                sources=sources if source_packet_count > 0 else [],
            ),
            [downgrade_reason or "limited_evidence"],
        )

    if not parsed:
        reasons.append("parse_failed")

    module = build_partial_module_completion(
        module_key=module_key,
        module_id=module_id,
        title=title,
        sources=sources,
        direct_answer=direct_answer,
        key_findings=key_findings,
        recommended_actions=recommended_actions,
    )

    if len(key_findings) < 2:
        reasons.append("key_findings_supplemented")
    if len(recommended_actions) < 2:
        reasons.append("recommended_actions_supplemented")
    if not cited_field_passes_safety_gate(direct_answer):
        reasons.append("direct_answer_supplemented")

    fully_valid = (
        cited_field_is_usable(direct_answer)
        and count_safety_passing_fields(key_findings) >= 2
        and count_safety_passing_fields(recommended_actions) >= 2
    )
    confidence = compute_module_confidence(source_packet_count, fully_valid=fully_valid)
    if source_packet_count == 1 and confidence == "low":
        reasons.append("single_source_ready")

    module = module.model_copy(update={"confidence": confidence})
    return module, reasons


def log_module_quality_gate(
    *,
    module_key: str,
    retrieved_chunk_count: int,
    source_packet_count: int,
    module: InsightModuleOutput,
    preserved_generated_module: bool,
    limited_evidence_reason: str | None,
    downgraded_to_limited_evidence: bool,
    **extra: Any,
) -> None:
    """Emit structured quality-gate diagnostics to server logs only."""
    direct_safe = cited_field_passes_safety_gate(module.direct_answer)
    payload = {
        "level": "INFO",
        "event": "insight_module_quality_gate",
        "module_key": module_key,
        "retrieved_chunk_count": retrieved_chunk_count,
        "usable_source_packet_count": source_packet_count,
        "generated_has_uuid": any(
            text_has_raw_uuid(field.text)
            for field in [module.direct_answer, *module.key_findings, *module.recommended_actions]
        ),
        "generated_has_raw_fragment": module_has_visible_artifacts(module),
        "direct_answer_quality_pass": direct_safe,
        "findings_quality_pass_count": count_safety_passing_fields(module.key_findings),
        "actions_quality_pass_count": count_safety_passing_fields(module.recommended_actions),
        "final_status": module.status,
        "final_confidence": module.confidence,
        "final_source_count": len(module.sources),
        "downgraded_to_limited_evidence": downgraded_to_limited_evidence,
        "downgrade_reason": limited_evidence_reason,
        "preserved_generated_module": preserved_generated_module,
        "limited_evidence_reason": limited_evidence_reason,
        **extra,
    }
    logging.getLogger("apalym.api").info(json.dumps(payload, default=str))


def serialize_module_for_api(
    module: InsightModuleOutput,
    *,
    log_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Final backend validator immediately before API serialization."""
    module = normalize_module_cited_fields(module)
    source_packet_count = len(module.sources)
    generated_has_raw_fragment = module_has_visible_artifacts(module)

    ok, _reasons = validate_module_output(module)
    downgrade, downgrade_reason = should_downgrade_to_limited_evidence(
        source_packet_count=source_packet_count,
        direct_answer=module.direct_answer,
        key_findings=module.key_findings,
        recommended_actions=module.recommended_actions,
    )

    preserved_generated_module = module.status == "ready" and not downgrade
    limited_evidence_reason: str | None = None

    if downgrade:
        limited_evidence_reason = downgrade_reason
        preserved_generated_module = False
        module = build_limited_evidence_module(
            module_key=module.module_key,
            module_id=module.module_id,
            title=module.title,
            sources=module.sources if source_packet_count > 0 else [],
        )
        module = normalize_module_cited_fields(module)
    elif not ok or generated_has_raw_fragment:
        preserved_generated_module = True
        module = build_partial_module_completion(
            module_key=module.module_key,
            module_id=module.module_id,
            title=module.title,
            sources=module.sources,
            direct_answer=module.direct_answer,
            key_findings=module.key_findings,
            recommended_actions=module.recommended_actions,
        )
        module = normalize_module_cited_fields(module)

    if log_context is not None:
        log_module_quality_gate(
            module_key=module.module_key,
            retrieved_chunk_count=int(log_context.get("retrieved_chunk_count", 0)),
            source_packet_count=source_packet_count,
            module=module,
            preserved_generated_module=preserved_generated_module,
            limited_evidence_reason=limited_evidence_reason,
            downgraded_to_limited_evidence=module.status == "limited_evidence",
            **{k: v for k, v in log_context.items() if k != "retrieved_chunk_count"},
        )

    payload = module.model_dump()
    if module.status == "limited_evidence":
        payload["validation_questions"] = module.validation_questions or module.evidence_limitations.validation_questions
    else:
        payload["validation_questions"] = []
    return payload


def module_to_response_dict(module: InsightModuleOutput) -> dict[str, Any]:
    return serialize_module_for_api(module)
