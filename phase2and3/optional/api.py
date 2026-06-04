"""
Four endpoints over the hybrid RAG pipeline.
All endpoints require a valid JWT token in the Authorization header.

Auth flow:
  POST /auth/login  →  {"access_token": "...", "expires_in": "1 hour"}
  Authorization: Bearer <token>  on all subsequent requests.
"""
from datetime import date
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List, Dict
import uuid

from dateutil import parser as dateutil_parser
from phase2and3.opt.auth import create_token, verify_token
from rag_pipeline import ask_question
from config.settings import VALID_USERS

app = FastAPI(
    title="Market Knowledge API",
    description="Queryable Market Knowledge Layer for Financial Events",
    version="1.0.0",
)


# ── Request models ─────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class QueryRequest(BaseModel):
    query: str


class QARequest(BaseModel):
    question: str
    history_id: Optional[str] = None


# ── In-memory session store ────────────────────────────────────────────────────
SESSION_HISTORY: Dict[str, List[Dict]] = {}


# ── Routes ─────────────────────────────────────────────────────────────────────

# root to verify API working on specified port.
@app.get("/")
async def root():
    return {"message": "Market Knowledge API is operational"}

# API endpoint to generate bearer token to facilitate usage of all other APIs, based on session-wise auth.
# The duration of the session can be changed as per requirement.

@app.post("/auth/login")
async def login(request: LoginRequest):
    """
    Exchange credentials for a JWT session token.
    Token expires after SESSION_DURATION_HOURS (configurable in .env).
    """
    if VALID_USERS.get(request.username) != request.password:
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    token = create_token(request.username)
    return {"access_token": token, "expires_in": "1 hour"}


# DailySummaryAPI. The description is given below.
# The date field is optional but can be used in order to filter data only from that date.
# If no data exists on that date, it returns that there is insufficient data for the same.

@app.get("/api/summary/daily")
async def get_daily_summary(
    summary_date: Optional[str] = None,
    user: str = Depends(verify_token),
):
    """
    Summarise key market events for a given date.
    Accepts any date format: 2026-03-21, 21/03/2026, March 21 2026, today, yesterday.
    Defaults to today if no date provided.

    Requires: Authorization: Bearer <token>
    """
    parsed_date = None
    if summary_date:
        try:
            parsed_date = dateutil_parser.parse(summary_date).date()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Invalid date format. Try YYYY-MM-DD, DD/MM/YYYY or 'March 21 2026'."
            )

    target_date = parsed_date or date.today()
    date_str = target_date.strftime("%Y-%m-%d")

    query = (
        f"Summarise key market events from {date_str} in simple language. "
        "Include the most significant events, which sectors and regions were affected, "
        "and the overall market direction."
    )

    answer = ask_question(query, event_date=date_str)
    return {"summary": answer, "date": date_str}


# ThemesAPI. Intended usage and description is mentioned below.
# Input: User prompt (req.)

@app.post("/api/themes")
async def get_market_themes(
    request: QueryRequest,
    user: str = Depends(verify_token),
):
    """
    Identify macro events and market themes.

    Examples:
    - "Which global macro events impacted Nordic markets?"
    - "What is causing volatility right now?"
    - "What measures are being taken against rising inflation?"

    Requires: Authorization: Bearer <token>
    """
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="query field cannot be empty.")
    answer = ask_question(request.query)
    return {"themes": answer}

# ClassificationAPI. Works similar to ThemesAPI technically, with a different behaviour specification for the LLM behaviour.
# Input: User prompt (required.)

@app.post("/api/classification")
async def get_classification(
    request: QueryRequest,
    user: str = Depends(verify_token),
):
    """
    Classify market events by asset class, region, sector, theme, or any combination.

    Examples:
    - "Show me events affecting Nordic Financials"
    - "What happened in Fixed Income markets in Europe last week?"
    - "Which companies were most negatively impacted by rate decisions?"

    Requires: Authorization: Bearer <token>
    """
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="query field cannot be empty.")
    answer = ask_question(request.query)
    return {"results": answer}


# CQA API: Chatbot based interface for user queries.
# Uses SESSION_HISTORY to maintain responses for the last 10 prompts (can be changed as per requirement), as mentioned before.

@app.post("/api/qa")
async def conversational_qa(
    request: QARequest,
    user: str = Depends(verify_token),
):
    """
    Conversational Market Q&A with persistent in-memory session history.

    On first call omit history_id — a new session ID is returned.
    Pass that history_id on subsequent calls to maintain context.
    Last 10 turns of history are included in each request.

    Requires: Authorization: Bearer <token>
    """
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="question field cannot be empty.")

    history_id = request.history_id or str(uuid.uuid4())
    history = SESSION_HISTORY.get(history_id, [])[-10:]

    answer = ask_question(request.question, conversation_history=history)

    if history_id not in SESSION_HISTORY:
        SESSION_HISTORY[history_id] = []
    SESSION_HISTORY[history_id].append({"role": "user",      "content": request.question})
    SESSION_HISTORY[history_id].append({"role": "assistant", "content": answer})

    return {"answer": answer, "history_id": history_id}


# run the server.
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
