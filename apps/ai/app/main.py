from fastapi import FastAPI

app = FastAPI(title="Apalym AI")

@app.get("/health")
def health():
    return {"ok": True, "service": "ai"}

@app.post("/infer")
def infer(payload: dict):
    # Placeholder: wire your model here later
    return {"ok": True, "echo": payload}
