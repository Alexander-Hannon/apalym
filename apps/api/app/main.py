
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import os
import httpx


app = FastAPI(title="Apalym API")

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
async def demo(req: DemoRequest):
    """
    Demo orchestration endpoint:
    - receives a message from the web app
    - calls the AI service (/infer)
    - returns the AI response
    """
    payload = {"message": req.message}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{AI_BASE_URL}/infer", json=payload)
            r.raise_for_status()
            ai_data = r.json()

        return {
            "ok": True,
            "service": "api",
            "sent_to_ai": payload,
            "ai_response": ai_data,
        }

    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Could not connect to AI service")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"AI error: {e.response.text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")

