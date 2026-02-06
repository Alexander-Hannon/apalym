from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

import json
import time
import uuid
import os
import httpx


def log_json(**fields):
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False))


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

    try:
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

    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Could not connect to AI service")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"AI error: {e.response.text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

