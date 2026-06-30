# Insight Output Contract Reset

## Rollback reference

- **Branch:** `fix/insight-output-contract-reset`
- **Parent commit (pre-reset work):** `f982241` — Fix citation evidence contract for insights and ask endpoints
- **Last commit before repeated fallback/citation patch stack:** approximately `78d581d` (UX Fix), before commits `1f90eb6`–`5828005` introduced overlapping sanitizers
- **Do not revert the whole repository.** Legacy sanitizers remain in `apps/api/app/main.py` but are isolated from the `/v1/insights` hot path.

## Baseline production failures

1. User-facing prose contained raw internal labels such as `[c394bf1b-9bce-4a35-9601-23632478f468:67]`
2. Limited-evidence modules showed chopped retrieval fragments (`holds Financial…`, `Medium Medium…`, `tional Labor…`)
3. Findings/actions mixed chopped source text with generic fallback bullets
4. Overlapping backend sanitizers, grounded-module replacement, UUID kill switches, and frontend regex cleanup made output unstable between runs

## Current raw citation format (pre-reset production)

| Stage | Format |
|-------|--------|
| Pinecone vector id | `{doc_uuid}:{chunk_index}` |
| Legacy prompt instruction | `[doc_id:chunk_id]` |
| Leaked prose | `[uuid:67]`, bare UUIDs |
| Legacy API citations | `citations[].source_id`, raw `excerpt` slices |
| Legacy ask UI | raw `matches[].text` |

## Limited-evidence failure mode (pre-reset)

- Modules marked `grounded` / `fallback` rendered amber **Needs validation**
- Hero/findings/actions were sometimes replaced with templates, but **Top Sources** still showed raw 500-char chunk slices
- Quality gates rejected prose while leaving bad excerpts in `citations[]`

## Response path: retrieval → UI

### `/v1/insights` (active path after reset)

1. `retrieve_matches()` — Pinecone hits with internal `id`, `metadata.doc_id`, `metadata.chunk_id`, `metadata.text`
2. `build_clean_source_packets()` — deterministic `[1..N]` packets; internal ids stored only in `internal_*` packet fields (never serialized)
3. `build_context_snippets()` — LLM prompt context from cleaned packets
4. Gemini generation — structured JSON with separate `citation_ids`
5. **One repair attempt** on validation failure (same packets, repair prompt)
6. `insight_contract.finalize_insight_module()` — **single backend finalizer** → `ready` or `limited_evidence`
7. `module_to_response_dict()` — typed Pydantic output, no `matches` / `citations` / internal ids
8. Frontend `page.tsx` — renders `text` verbatim; badges from `citation_ids`; sources on demand

### Isolated / not on hot path

- `finalize_visible_insight_response()`
- `build_clean_grounded_module()`
- `finalize_insight_module()` (legacy string-based)
- `normalize_visible_citations()` (legacy; not used by contract finalizer)

### `/v1/ask`

1. Same `build_clean_source_packets()`
2. Gemini structured JSON
3. `finalize_ask_response()` using `insight_contract.parse_cited_field()` + `build_public_sources()`
4. Frontend renders structured `answer`, `source_backed_findings`, `advisor_recommendations`, `sources`

### Stale UI data

- Frontend clears `insights` on upload and before regenerate (`fetchInsights(docId, clearOldInsights=true)`)
- No server-side insight cache; each Generate Brief call hits `/v1/insights` fresh
- Run 1 and Run 2 use the same code branch; differences come from LLM variability, not alternate serializers

## Root cause statement

The pipeline mixed **numbered source packets** with **legacy `[doc_id:chunk_id]` instructions**, then ran **multiple downstream mutation passes** (grounded replacement, UUID kill switch, frontend regex). Raw chunk text remained in parallel citation objects, so sanitizing prose did not prevent chopped fragments and internal IDs from reaching the UI.

## New contract summary

See `apps/api/app/insight_contract.py`:

- Typed models: `CitedField`, `SourceEvidence`, `EvidenceLimitations`, `InsightModuleOutput`
- Only two module states: `ready` | `limited_evidence`
- Visible `text` fields must not contain citation syntax or internal ids
- Citations only in `citation_ids` (integers)
- Sources inspectable separately via `sources[]`

## Files changed in this task

| File | Purpose |
|------|---------|
| `apps/api/app/insight_contract.py` | Single contract module (models, validation, finalizer) |
| `apps/api/app/main.py` | Wire insights/ask to contract; isolate legacy sanitizers |
| `apps/api/app/test_insight_contract.py` | Contract regression tests |
| `apps/web/app/page.tsx` | Dumb presenter (no prose regex) |
| `scripts/test_insight_repeat.sh` | Pilot PDF repeatability runner |
| `docs/production/INSIGHT_OUTPUT_RESET.md` | This document |

## Test plan

### Automated

```bash
cd apps/api && python3 -m unittest app.test_insight_contract app.test_citation_contract -v
cd apps/web && npm run lint && npx tsc --noEmit && npm run build
python3 -m py_compile apps/api/app/main.py apps/api/app/insight_contract.py
curl -sS "${API_BASE%/}/health"
```

### Pilot repeatability (requires PDF + API keys)

```bash
PDF_PATH=/path/to/meridian_cold_chain_financial_package.pdf API_BASE=<api-base-url> bash scripts/test_insight_repeat.sh
```

### Production validation (post-deploy)

1. Confirm ECS task healthy and `https://api.apalym.com/health` OK
2. Generate Brief twice in production UI
3. Inspect raw `/v1/insights` JSON in DevTools — no UUIDs/chunk labels in visible text fields
4. Confirm citation badges open clean source excerpts only

## What was removed or isolated

- Removed duplicate `finalize_executive_module` / local `build_limited_evidence_module` from hot path
- Stopped calling `finalize_visible_insight_response()` on `/v1/insights` responses
- Contract finalizer no longer uses `normalize_visible_citations()` on visible text
- Frontend no longer parses `[uuid:chunk]` from prose

## Remaining limitations

- Pilot PDF not present in repository; repeatability script requires local file
- Page numbers estimated from chunk offset when PDF page metadata unavailable
- Production browser validation pending deploy
- Legacy helper functions remain in `main.py` for reference but are not invoked by `/v1/insights`
