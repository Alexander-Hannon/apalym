# Citation Evidence Contract

## Current raw citation format (before fix)

| Location | Format | Example |
|----------|--------|---------|
| Insights LLM prompt instruction | `[doc_id:chunk_id]` | `Cite evidence with [doc_id:chunk_id]` |
| Pinecone vector id | `{uuid}:{chunk_index}` | `cd3d2a1b-...:0` |
| LLM-generated prose | bracketed UUID/chunk | `[cd3d2a1b-...:0]` |
| API `citations[].source_id` | raw Pinecone id | exposed in JSON |
| API `citations[].excerpt` | raw chunk text, 500-char slice | chopped table rows |
| Ask UI `matches[].text` | raw retrieval text | unfiltered chunk dumps |

## Current user-visible failure

- Executive prose, findings, and actions contained raw `[uuid]`, `[uuid:chunk_id]`, and `doc_id:chunk_id` strings.
- Two modules showed amber **Needs validation** (from `grounded` / `fallback` statuses) while still rendering chopped source fragments in Top Sources.
- Frontend parsed `[uuid:chunk]` patterns out of prose instead of using structured `citation_ids`.
- Weak modules dumped partial table rows into visible answer fields when quality gates replaced prose but not source excerpts.

## Exact backend source of the leak

1. **`generate_insights` prompt** (`apps/api/app/main.py`) instructed the model to cite `[doc_id:chunk_id]` while context used numbered `[1]`, `[2]`, `[3]` packets — contradictory contract.
2. **`_build_structured_citations`** used `compact_whitespace(raw_text)[:500]` instead of `clean_source_excerpt()`, leaking chopped chunks into `citations[].excerpt`.
3. **`finalize_visible_insight_response`** replaced failed prose with `build_clean_grounded_module()` (`status: grounded`) but left raw excerpts in `citations[]`.
4. **`/v1/ask`** returned raw `citations` and `matches` with internal `source_id` values; frontend rendered `match.text` directly.

## Exact frontend rendering path

1. Insights hero / findings: `citedTextStr()` for prose; citation chips parsed from `[uuid:chunk]` via `renderTextWithCitationChips()`.
2. Top Sources: `selectedStructuredCitations` → `sc.excerpt` (raw backend excerpt).
3. Ask answer: `renderTextWithCitationChips(insightBrief.directAnswer, citationOrder)` with `buildCitationOrder(citations)` keyed on `source_id`.
4. Ask sources: `matchesBySource.get(citation.source_id)` → `match.text` (raw).

## Proposed corrected contract

Both `/v1/insights` modules and `/v1/ask` responses now use:

```json
{
  "direct_answer": { "text": "Clean prose only.", "citation_ids": [1, 2] },
  "key_findings": [{ "text": "...", "citation_ids": [1] }],
  "recommended_actions": [{ "text": "...", "citation_ids": [1, 2] }],
  "sources": [
    {
      "citation_id": 1,
      "title": "Source evidence",
      "page": 7,
      "excerpt": "Clean human-readable excerpt.",
      "document_name": "meridian_cold_chain_financial_package.pdf"
    }
  ],
  "status": "ready | limited_evidence",
  "evidence_limitations": {
    "has_limitations": false,
    "message": null,
    "validation_questions": []
  }
}
```

Rules enforced:
- `citation_ids` are small positive integers only.
- No UUIDs, Pinecone ids, doc/chunk ids, or metadata keys in visible text fields.
- LLM receives clean numbered source packets; citations returned separately in JSON.
- Frontend renders purple citation badges from `citation_ids` only; badge click opens evidence modal.
- `limited_evidence` modules include validation questions and clean excerpts in `sources[]` only.

## Files changed

| File | Change |
|------|--------|
| `apps/api/app/main.py` | Source packets, contract finalizers, insights/ask serializers, narrow validator |
| `apps/web/app/page.tsx` | Structured citation badges, evidence modal, limited-evidence UX |
| `apps/web/app/globals.css` | Citation badge, validation list, evidence modal styles |
| `apps/api/app/test_citation_contract.py` | Regression tests |
| `docs/production/CITATION_EVIDENCE_CONTRACT.md` | This document |

## Regression tests added

`apps/api/app/test_citation_contract.py`:
- Contract validation rejects UUID / `doc_id:chunk_id` / chopped fragments in visible fields
- `_parse_cited_field` strips inline `[N]` from prose and preserves `citation_ids`
- `_build_public_sources` never includes internal ids
- `build_limited_evidence_module` returns validation questions and clean sources
- Optional integration runner for repeat-generation against uploaded pilot PDF when API keys and document are present
