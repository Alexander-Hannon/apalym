"""Single authoritative insight-output contract for /v1/insights."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

InsightStatus = Literal["ready", "limited_evidence"]
InsightConfidence = Literal["high", "medium", "low"]


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
_CITATION_SYNTAX_IN_TEXT = re.compile(r"\[\s*\d{1,2}\s*\]|\[[^\]]*:[^\]]+\]")
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


def strip_inline_citations_from_text(text: str) -> str:
    cleaned = text or ""
    cleaned = _BRACKETED_INTERNAL_ID.sub("", cleaned)
    cleaned = _UUID_CHUNK_PATTERN.sub("", cleaned)
    cleaned = _UUID_PATTERN.sub("", cleaned)
    cleaned = _CITATION_SYNTAX_IN_TEXT.sub("", cleaned)
    cleaned = _INTERNAL_FIELD_NAMES.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return compact_whitespace(cleaned)


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

    clean_text = strip_inline_citations_from_text(raw_text)
    return CitedField(text=clean_text, citation_ids=ids)


def build_public_sources(source_packets: list[dict[str, Any]]) -> list[SourceEvidence]:
    sources: list[SourceEvidence] = []
    for packet in source_packets:
        sources.append(
            SourceEvidence(
                citation_id=int(packet["citation_id"]),
                document_name=packet.get("document_name") or "Uploaded document",
                page=packet.get("page"),
                excerpt=packet.get("excerpt") or "",
                title=packet.get("title") or "Source evidence",
            )
        )
    return sources


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
        if not module.evidence_limitations.message:
            reasons.append("limited_evidence_message_missing")
        if len(module.evidence_limitations.validation_questions) < 2:
            reasons.append("validation_questions_too_short")

    return (len(reasons) == 0, sorted(set(reasons)))


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
}


def category_validation_questions(module_key: str) -> list[str]:
    keyed = LIMITED_EVIDENCE_COPY.get(module_key, {}).get("validation_questions")
    if isinstance(keyed, list) and len(keyed) >= 2:
        return keyed
    defaults = {
        "losses": [
            "What is gross margin by customer, route, location, and service line?",
            "Which labor, freight, claims, or pricing drivers explain the largest variance?",
        ],
        "costs": [
            "Which cost categories are above budget versus prior period by operating unit?",
            "What recurring versus one-time costs explain the largest unfavorable variance?",
        ],
        "margins": [
            "What is margin by customer, route, and service line after fully loaded costs?",
            "Which pricing, labor, claims, or billing gaps explain the largest margin leakage?",
        ],
        "trends": [
            "Which metrics changed materially over the last three to six periods?",
            "Are recent variances recurring or driven by one-time events?",
        ],
    }
    return defaults.get(module_key, [
        "What supporting schedules or department-level detail would confirm this diagnosis?",
        "Which operating metrics should be validated before acting on these recommendations?",
    ])


def build_limited_evidence_module(
    *,
    module_key: str,
    module_id: str,
    title: str,
    sources: list[SourceEvidence],
    fallback_copy: dict[str, Any],
    partial: dict[str, Any] | None = None,
) -> InsightModuleOutput:
    max_sources = max(len(sources), 1)
    category_copy = LIMITED_EVIDENCE_COPY.get(module_key, {})
    direct_text = category_copy.get("direct_answer") or fallback_copy.get("direct_answer", "")
    findings_text = category_copy.get("key_findings") or fallback_copy.get("key_findings", [])
    actions_text = category_copy.get("recommended_actions") or fallback_copy.get("recommended_actions", [])

    direct_answer = parse_cited_field(direct_text, max_sources=max_sources, fallback_ids=[])
    key_findings = [
        parse_cited_field(item, max_sources=max_sources, fallback_ids=[1] if sources else [])
        for item in findings_text[:3]
    ]
    recommended_actions = [
        parse_cited_field(item, max_sources=max_sources, fallback_ids=[1] if sources else [])
        for item in actions_text[:3]
    ]

    if partial:
        candidate = partial.get("direct_answer")
        if isinstance(candidate, CitedField) and text_is_substantive(candidate.text) and not text_has_internal_artifact(candidate.text):
            direct_answer = candidate

        partial_findings = [
            item for item in (partial.get("key_findings") or [])
            if isinstance(item, CitedField) and text_is_substantive(item.text) and not text_has_internal_artifact(item.text)
        ]
        if len(partial_findings) >= 2:
            key_findings = partial_findings[:3]

        partial_actions = [
            item for item in (partial.get("recommended_actions") or [])
            if isinstance(item, CitedField) and text_is_substantive(item.text) and not text_is_generic_placeholder(item.text)
        ]
        if len(partial_actions) >= 2:
            recommended_actions = partial_actions[:3]

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
            validation_questions=category_validation_questions(module_key),
        ),
        sources=sources,
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

    key_findings = [
        item for item in key_findings
        if text_is_substantive(item.text) and not text_has_internal_artifact(item.text)
    ]
    recommended_actions = [
        item for item in recommended_actions
        if text_is_substantive(item.text) and not text_is_generic_placeholder(item.text) and not text_has_internal_artifact(item.text)
    ]

    return {
        "direct_answer": direct_answer,
        "key_findings": key_findings,
        "recommended_actions": recommended_actions,
    }


def finalize_insight_module(
    *,
    module_key: str,
    module_id: str,
    title: str,
    generated: dict[str, Any] | None,
    source_packets: list[dict[str, Any]],
    fallback_copy: dict[str, Any],
) -> tuple[InsightModuleOutput, list[str]]:
    sources = build_public_sources(source_packets)
    max_sources = len(sources)
    default_ids = [sources[0].citation_id] if sources else []

    if not sources or generated is None:
        return (
            build_limited_evidence_module(
                module_key=module_key,
                module_id=module_id,
                title=title,
                sources=sources,
                fallback_copy=fallback_copy,
            ),
            ["no_sources_or_generation"],
        )

    parsed = parse_generated_module(generated, max_sources=max_sources, default_ids=default_ids)
    if not parsed:
        return (
            build_limited_evidence_module(
                module_key=module_key,
                module_id=module_id,
                title=title,
                sources=sources,
                fallback_copy=fallback_copy,
            ),
            ["parse_failed"],
        )

    direct_answer: CitedField = parsed["direct_answer"]
    key_findings: list[CitedField] = parsed["key_findings"]
    recommended_actions: list[CitedField] = parsed["recommended_actions"]

    if not text_is_substantive(direct_answer.text) or text_has_internal_artifact(direct_answer.text):
        return (
            build_limited_evidence_module(
                module_key=module_key,
                module_id=module_id,
                title=title,
                sources=sources,
                fallback_copy=fallback_copy,
                partial=parsed,
            ),
            ["direct_answer_not_usable"],
        )

    if len(key_findings) < 2 or len(recommended_actions) < 2:
        return (
            build_limited_evidence_module(
                module_key=module_key,
                module_id=module_id,
                title=title,
                sources=sources,
                fallback_copy=fallback_copy,
                partial=parsed,
            ),
            ["insufficient_findings_or_actions"],
        )

    confidence: InsightConfidence = "high" if max_sources >= 2 else "medium"
    module = InsightModuleOutput(
        module_key=module_key,
        module_id=module_id,
        title=title,
        status="ready",
        confidence=confidence,
        summary=direct_answer,
        direct_answer=direct_answer,
        key_findings=key_findings[:4],
        recommended_actions=recommended_actions[:4],
        evidence_limitations=EvidenceLimitations(message=None, validation_questions=[]),
        sources=sources,
    )

    ok, reasons = validate_module_output(module)
    if ok:
        return module, []

    return (
        build_limited_evidence_module(
            module_key=module_key,
            module_id=module_id,
            title=title,
            sources=sources,
            fallback_copy=fallback_copy,
            partial=parsed,
        ),
        reasons,
    )


def module_to_response_dict(module: InsightModuleOutput) -> dict[str, Any]:
    return module.model_dump()
