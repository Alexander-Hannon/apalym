#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE:-<api-base-url>}"
PDF_PATH="${PDF_PATH:-meridian_cold_chain_financial_package.pdf}"
OUT_DIR="${OUT_DIR:-/tmp/apalym-insight-runs}"

if [[ ! -f "$PDF_PATH" ]]; then
  echo "Pilot PDF not found at $PDF_PATH"
  echo "Set PDF_PATH to meridian_cold_chain_financial_package.pdf before running repeatability test."
  exit 1
fi

mkdir -p "$OUT_DIR"

echo "Uploading pilot PDF..."
UPLOAD_JSON=$(curl -sS -F "file=@${PDF_PATH}" "${API_BASE}/v1/docs/upload")
DOC_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["doc_id"])' <<<"$UPLOAD_JSON")
echo "doc_id=$DOC_ID"

echo "Indexing..."
curl -sS -X POST "${API_BASE}/v1/docs/${DOC_ID}/index?chunk_size=700&overlap=80" -H 'Content-Type: application/json' -d '{}' >/dev/null

for RUN in 1 2; do
  echo "Generate Brief run ${RUN}..."
  curl -sS -X POST "${API_BASE}/v1/insights" \
    -H 'Content-Type: application/json' \
    -d "{\"doc_id\":\"${DOC_ID}\",\"top_k\":3}" \
    > "${OUT_DIR}/insights_run_${RUN}.json"
done

python3 - <<'PY'
import json
import re
from pathlib import Path

out_dir = Path("/tmp/apalym-insight-runs")
uuid_re = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
chunk_re = re.compile(r"[0-9a-f-]{36}:\d+", re.I)
bracket_re = re.compile(r"\[[^\]]*:[^\]]+\]")

for run in (1, 2):
    payload = json.loads((out_dir / f"insights_run_{run}.json").read_text())
    modules = payload.get("modules") or []
    print(f"RUN {run}: modules={len(modules)}")
    assert len(modules) == 5, "expected 5 modules"
    for module in modules:
        status = module.get("status")
        assert status in {"ready", "limited_evidence"}, status
        texts = []
        for key in ("direct_answer", "summary"):
            val = module.get(key)
            if isinstance(val, dict):
                texts.append(val.get("text", ""))
        for key in ("key_findings", "recommended_actions"):
            for item in module.get(key) or []:
                if isinstance(item, dict):
                    texts.append(item.get("text", ""))
        for text in texts:
            assert not uuid_re.search(text or ""), text
            assert not chunk_re.search(text or ""), text
            assert not bracket_re.search(text or ""), text
        print(f"  - {module.get('module_id')}: {status}")
print("Repeatability payload checks passed.")
PY

echo "Saved outputs under ${OUT_DIR}"
