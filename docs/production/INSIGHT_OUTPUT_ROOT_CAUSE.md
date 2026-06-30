# Insight Output Root Cause

## Rollback reference

- Branch: `cursor/fix-insight-output-contract-reset-55e5`
- Pre-patch stack began ~`1f90eb6` (`Normalize visible citation markers`)
- Active hot-path contract: `apps/api/app/insight_contract.py`

---

## `/v1/insights` execution map

| Step | Function | File | Input → Output |
|------|----------|------|----------------|
| 1 | `retrieve_matches()` | `main.py` | query → matches with `id`=`uuid:chunk`, `text`=raw Pinecone metadata text |
| 2 | `build_clean_source_packets()` | `main.py` | matches → numbered packets; `clean_source_excerpt()` cleans excerpt; `internal_*` never serialized |
| 3 | `format_source_packet_block()` | `main.py` | packet → LLM context string `[N]\nPage: …\nEvidence: …` |
| 4 | `generate_content_with_retry()` | `main.py` | prompt → raw Gemini JSON string |
| 5 | `parse_model_json()` | `main.py` | string → dict |
| 6 | `finalize_insight_module()` | `insight_contract.py` | dict + packets → `InsightModuleOutput` |
| 7 | Repair attempt (optional) | `main.py` `generate_insights()` | one retry with `build_insight_repair_instruction()` |
| 8 | `module_to_response_dict()` | `insight_contract.py` | typed model → API JSON |

### Normal generation path

`generate_insights` → retrieve → `build_clean_source_packets` → Gemini → `finalize_insight_module` → status `ready` when validation passes.

### Fallback / limited-evidence path

Triggered when generation fails, validation fails, or repair fails:

`build_limited_evidence_module()` in `insight_contract.py`

**Root leak (pre-fix):** `build_limited_evidence_module(partial=…)` merged failed LLM fields into limited-evidence `key_findings` / `recommended_actions`. Those fields still contained chopped retrieval copies (`ductivity…`, `Medium Medium…`, `tional…`).

### Citation path

- LLM instructed to return `citation_ids` only (no brackets in `text`)
- `parse_cited_field()` maps structured JSON → `CitedField`
- **Secondary leak (pre-fix):** `strip_inline_citations_from_text()` removed `[uuid:chunk]` markers but could leave broken remainder in `text` instead of rejecting

### Where raw UUID/chunk references entered visible strings

1. **LLM echoed `[doc_id:chunk]` or `[uuid:N]` in prose** when older prompts conflicted with numbered packets
2. **`parse_cited_field` stripped brackets instead of rejecting** contaminated text
3. **`build_limited_evidence_module(partial=…)`** copied failed LLM output into limited-evidence visible fields

### Where chopped retrieval text entered visible strings

1. **`partial` merge in limited-evidence builder** (primary)
2. Legacy isolated paths (not on hot path): `finalize_visible_insight_response`, `build_clean_grounded_module`
3. Ask extractive fallback: `build_dynamic_fallback_actions()` appended `match.text` snippets (fixed separately)

---

## `/v1/ask` execution map

| Step | Function | Notes |
|------|----------|-------|
| 1 | `retrieve_matches()` | same as insights |
| 2 | `build_clean_source_packets()` | shared packet builder |
| 3 | Gemini + `parse_model_json()` | structured brief |
| 4 | `finalize_ask_response()` | `parse_cited_field` + `build_public_sources` |
| 5 | API JSON | `answer`, `source_backed_findings`, `advisor_recommendations`, `sources` |

Extractive fallback on LLM failure previously surfaced raw excerpts in findings (fixed).

---

## Frontend rendering (`apps/web/app/page.tsx`)

| Function | Role |
|----------|------|
| `citedTextStr()` | reads `CitedText.text` verbatim |
| `renderCitedContent()` | renders text + citation pills from `citation_ids` only |
| `openEvidence()` | opens source modal from `sources[]` |

No citation regex parsing in hot path. Decorative `hero-visual` bars replaced with Evidence Quality card.

---

## Stale payloads

- `fetchInsights(docId, clearOldInsights=true)` on upload clears prior modules
- Regenerate calls `/v1/insights` fresh; no server-side insight cache
- Run 1 and Run 2 use identical code path; variance is LLM-only

---

## Fix summary

| Fix | Location |
|-----|----------|
| Remove `partial` merge from limited-evidence | `insight_contract.build_limited_evidence_module` |
| Reject contaminated text instead of strip-only | `insight_contract.parse_cited_field` |
| Author all category limited-evidence templates | `insight_contract.LIMITED_EVIDENCE_COPY` |
| Final pre-serialize safety gate | `insight_contract.serialize_module_for_api` |
| Remove match-text snippets from ask fallbacks | `main.build_dynamic_fallback_actions` |
| Evidence Quality card (no fake graph) | `page.tsx` |
