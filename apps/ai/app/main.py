import json
import time
import uuid

from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import Response


def log_json(**fields):
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False))



app = FastAPI(title="Apalym AI")

@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start = time.time()
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
            service="ai",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=locals().get("status_code", 500),
            latency_ms=latency_ms,
        )


@app.get("/health")
def health():
    return {"ok": True, "service": "ai"}


@app.post("/infer")
def infer(payload: dict, request: Request):
    tenant_id = request.headers.get("x-tenant-id", "unknown-tenant")

    return {
        "ok": True,
        "request_id": request.state.request_id,
        "tenant_id": tenant_id,
        "echo": payload,
    }
