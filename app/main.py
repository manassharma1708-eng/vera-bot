import time
from fastapi import FastAPI

app = FastAPI(title="Vera Bot")
START = time.time()

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": {"category": 0, "merchant": 0, "customer": 0, "trigger": 0},
    }