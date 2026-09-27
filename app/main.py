import json
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import conversations
from app.reply_brain import handle_reply
from app.store import VALID_SCOPES, store

app = FastAPI(title="Vera Bot")
START = time.time()
MAX_PAYLOAD_BYTES = 500 * 1024  # 500 KB cap from the testing brief


# ---------- Health + identity ----------

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": store.counts(),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "MANAS SHARMA",                 # <-- change
        "team_members": ["MANAS SHARMA"],            # <-- change
        "model": "tbd",                           # we'll fill this in Step 5
        "approach": "Deterministic fact-sheet extraction + LLM composer with post-validation",
        "contact_email": "manassharma1708@gmail.com.com",       # <-- change
        "version": "0.3.0",
        "submitted_at": "2026-04-26T08:00:00Z",
    }


# ---------- Context push ----------

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str | None = None


@app.exception_handler(RequestValidationError)
async def bad_request(request: Request, exc: RequestValidationError):
    """Turn FastAPI's default 422 into the 400 shape the judge expects."""
    return JSONResponse(
        status_code=400,
        content={"accepted": False, "reason": "invalid_body", "details": str(exc.errors())[:500]},
    )


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in VALID_SCOPES:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "invalid_scope",
                     "details": f"scope must be one of {list(VALID_SCOPES)}"},
        )

    size = len(json.dumps(body.payload, ensure_ascii=False).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "payload_too_large", "details": f"{size} bytes"},
        )

    accepted, current_version, stored_at = store.put(
        body.scope, body.context_id, body.version, body.payload
    )
    if not accepted:
        return JSONResponse(
            status_code=409,
            content={"accepted": False, "reason": "stale_version", "current_version": current_version},
        )

    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": stored_at,
    }


# ---------- Temporary stub (replaced in Step 7) ----------

@app.post("/v1/tick")
async def tick(request: Request):
    return {"actions": []}


# ---------- Reply ----------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str = "merchant"
    message: str = ""
    received_at: str | None = None
    turn_number: int = 0


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    try:
        return handle_reply(body.conversation_id, body.merchant_id, body.customer_id,
                            body.from_role, body.message, body.turn_number)
    except Exception as exc:  # never return a 500 to the judge
        return {"action": "wait", "wait_seconds": 3600,
                "rationale": f"Internal error handled safely ({type(exc).__name__}); backing off."}


@app.post("/v1/teardown")
async def teardown():
    store.clear()
    conversations.clear()
    return {"ok": True}