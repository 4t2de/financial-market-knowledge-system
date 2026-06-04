"""
LLM Extraction Layer
--------------------
Sends article content to Groq and parses the structured JSON response
into ArticleExtractionResult models.

Swap GROQ_MODEL in .env to switch between models.
When Azure OpenAI credentials arrive, only this file needs to change.
"""
import json
import re
from typing import Optional

from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from loguru import logger

from config.settings import GROQ_API_KEY, GROQ_MODEL, LLM_MAX_RETRIES
from pipeline.models import ExtractedEvent, ArticleExtractionResult

# contains principal instructions for structured data extraction.
# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a senior financial analyst and market intelligence engine.
Your task is to read financial news articles and extract structured market events with deep analytical detail.

For each distinct market event you identify in the article, output a JSON object.
Return your answer as a JSON array (even if there is only one event).

Each event object MUST follow this exact schema:
{
  "summary": "<Thorough analytical summary — 3-5 sentences. Include: what happened, who was involved, what was said, market implications, and which entities are affected and how. Write this as a financial intelligence briefing paragraph.>",

  "event_type": "<one of: RATE_DECISION | EARNINGS_REPORT | MERGER_ACQUISITION | POLICY_CHANGE | REGULATORY_ACTION | GEOPOLITICAL_EVENT | ECONOMIC_DATA_RELEASE | MARKET_MOVEMENT | LEGISLATIVE_PROPOSAL | SANCTIONS | IPO | LEADERSHIP_CHANGE | OTHER>",

  "confidence_score": <float 0.0-1.0 — confidence this is a genuine distinct market event>,

  "asset_classes": ["<one or more of: Equities, Fixed Income, Commodities, FX, Crypto, Real Estate, Money Market>"],

  "sectors": [
    {
      "name": "<sector name e.g. Financials, Energy, Healthcare, Technology, Real Estate, Industrials, Consumer, Materials, Telecom, Utilities>",
      "sentiment_score": <float -1.0 to +1.0 — how this event affects this sector>,
      "impact_reason": "<one sentence: why this score>",
      "confidence_score": <float 0.0-1.0>
    }
  ],

  "regions": [
    {
      "name": "<region name e.g. Nordic, US, Europe, Global, Asia, Middle East, UK, China, Emerging Markets>",
      "sentiment_score": <float -1.0 to +1.0 — how this event affects this region's markets>,
      "impact_reason": "<one sentence: why this score>",
      "confidence_score": <float 0.0-1.0>
    }
  ],

  "themes": ["<e.g. Inflation, Volatility, Interest Rates, Geopolitical Risk, Earnings, M&A, Regulation, Recession, Central Bank Policy, ESG>"],

  "companies": [
    {
      "name": "<exact company name as in text>",
      "sentiment_score": <float -1.0 to +1.0>,
      "impact_reason": "<one sentence: why this score>",
      "confidence_score": <float 0.0-1.0>
    }
  ],

  "organisations": [
    {
      "name": "<government, central bank, regulator, international body — NOT companies>",
      "sentiment_score": <float -1.0 to +1.0>,
      "impact_reason": "<one sentence: why this score>",
      "confidence_score": <float 0.0-1.0>
    }
  ],

  "persons": [
    {
      "name": "<full name as in text>",
      "role": "<their title e.g. 'Fed Chair', 'CEO of Nordea'>",
      "quote_summary": "<concise analytical summary of what they said or did>",
      "statement_direction": "<hawkish | dovish | bullish | bearish | neutral>",
      "confidence_score": <float 0.0-1.0>,
      "company_impacts": [
        {
          "company_name": "<exact company name — only if this person's statement DIRECTLY affected this company>",
          "sentiment_score": <float -1.0 to +1.0>,
          "impact_reason": "<one sentence: what specifically did this person say that affected this company>",
          "confidence_score": <float 0.0-1.0>
        }
      ]
    }
  ],

  "overall_direction": "<positive | negative | neutral>"
}

Rules:
- Return ONLY the JSON array. No preamble, no explanation, no markdown fences.
- If the article contains no identifiable market events, return an empty array: []
- If the article has no direct or clearly implied financial market relevance (e.g. culture, sport, entertainment, human interest, book awards), return an empty array []. Do not invent financial implications.
- Extract ALL distinct events — one article can have multiple.
- sentiment_score is per entity — the same event can score -0.8 for Real Estate but +0.5 for Financials.
- sectors and regions now carry sentiment scores just like companies — score them honestly.
- persons[].company_impacts must only be populated when the article EXPLICITLY links what a person said to a specific company. Leave empty if the connection is only indirect.
- Do NOT repeat all event companies inside every person's company_impacts — only direct causal links.
- Do not duplicate entries between companies and organisations.
- confidence_score is your honest self-assessment — do not default everything to 0.9.
- summary must be rich enough that a portfolio manager could act on it without reading the article.
"""

# initializes API call driver
# ── Groq client (singleton) ───────────────────────────────────────────────────
_client: Optional[Groq] = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        import httpx
        http_client = httpx.Client()
        _client = Groq(api_key=GROQ_API_KEY, http_client=http_client)
    return _client

# article details are plugged in, in order to provide data for structured extraction from the LLM.
def _build_user_prompt(title: str, content: str, source: str) -> str:
    truncated_content = content[:6000] + ("..." if len(content) > 6000 else "")
    return f"""Source: {source}
Title: {title}

Article Content:
{truncated_content}

Extract all market events from this article and return the JSON array."""


# for the output that is obtained, it is validated using pydantic (refer models.py).
# response is rejected if it is found invalid/not adhering to the intended schema.
# if rejected, the article is retried upto a max number of x times, where x can be set in the config files.

def _parse_llm_response(raw: str, article_id: int) -> list[ExtractedEvent]:
    """Parse and validate the LLM JSON output."""
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error(f"Article {article_id}: JSON parse failed — {e}\nRaw: {raw[:500]}")
        raise ValueError(f"LLM returned invalid JSON: {e}") from e

    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array, got {type(data).__name__}")

    events = []
    for i, item in enumerate(data):
        try:
            events.append(ExtractedEvent(**item))
        except Exception as e:
            logger.warning(f"Article {article_id}: event[{i}] validation failed — {e}. Skipping.")
    return events

# retry logic, as mentioned earlier, if invalid responses are observed.
@retry(
    stop=stop_after_attempt(LLM_MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)

# call the LLM in order to generate the structured response for each article.
# system_prompt and user_prompt are plugged into the LLM call.

def extract_events(
    article_id: int,
    title: str,
    content: str,
    source: str,
) -> ArticleExtractionResult:
    """
    Main entry point: send one article to Groq, return structured events.
    Retries up to LLM_MAX_RETRIES times with exponential backoff.
    """
    client = _get_client()
    user_prompt = _build_user_prompt(title, content, source)

    logger.debug(f"Calling Groq for article_id={article_id} ({len(content)} chars)")

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.1,
        max_tokens=4096,
    )

    raw = response.choices[0].message.content or ""
    events = _parse_llm_response(raw, article_id)

    logger.info(f"Article {article_id}: extracted {len(events)} event(s) via Groq.")
    return ArticleExtractionResult(article_id=article_id, events=events)