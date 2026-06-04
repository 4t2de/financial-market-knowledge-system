# Module 2 & 3: Knowledge Pre-Processing Core & Queryable Market Knowledge Layer

Transforms raw scraped articles (Phase 1 PostgreSQL) into a structured market event graph (Neo4j) using Groq LLM, and exposes a hybrid RAG query layer via four REST API endpoints.

---

## Architecture

```
PostgreSQL (Phase 1)          Module 2                        Neo4j
─────────────────────         ────────────────────────────    ──────────────────────
articles        ──────────►  Orchestrator                    (Article)-[:CONTAINS]->
sources                       │                              (Event)-[:CLASSIFIED_AS]->
scraper_logs                  ├─ Seed processing_state       (AssetClass)
                              ├─ Fetch pending articles      (Event)-[:AFFECTS_SECTOR {sentiment_score, impact_reason, confidence_score}]->
processing_state (new) ◄──── ├─ Groq LLM extraction         (Sector)
                              ├─ Parse + validate            (Event)-[:AFFECTS_REGION {sentiment_score, impact_reason, confidence_score}]->
                              └─ Write to Neo4j              (Region)
                                                             (Event)-[:TAGGED_WITH]->
                                                             (Theme)
                                                             (Event)-[:AFFECTS {sentiment_score, impact_reason, confidence_score}]->
                                                             (Company)
                                                             (Event)-[:AFFECTS {sentiment_score, impact_reason, confidence_score}]->
                                                             (Organisation)
                                                             (Person)-[:STATED {quote_summary, statement_direction, role, statement_date, confidence_score}]->
                                                             (Event)
                                                             (Person)-[:IMPACTS {sentiment_score, impact_reason, confidence_score, via_event_id}]->
                                                             (Company)


Neo4j (Phase 2)               Module 3
─────────────────────         ────────────────────────────
Graph + Vector Index ──────► Hybrid Retrieval
                              │
                              ├─ Cypher generation (LLM)
                              ├─ Vector similarity search
                              ├─ Context assembly
                              └─ Answer generation (LLM)
                                        │
                                        ▼
                              REST API (FastAPI)
                              ├─ GET  /api/summary/daily
                              ├─ POST /api/themes
                              ├─ POST /api/classification
                              └─ POST /api/qa
```

### Key design decisions

**Phase 1 untouched** — Module 2 does not alter any Phase 1 tables. It introduces a single new table `processing_state` that references `articles.id` via foreign key. Phase 1 code is QA-locked.

**Sentiment on relationships, not nodes** — sentiment_score lives on the relationship (e.g. AFFECTS_SECTOR) not on the node itself. This means the same company or sector can have different sentiment scores across different events, preserving event-specific context rather than overwriting a node's permanent state.

**Simple schema for reliable Cypher generation** — all entities connect through the Event node. This keeps the schema navigable for the LLM generating Cypher queries in Module 3. More relationship types between more node combinations increases hallucination probability in Text-to-Cypher generation.

---

## Project Structure

```
module2/
├── main.py                         # Entrypoint (--migrate / --run-now / --schedule)
├── api.py                          # FastAPI entrypoint for Module 3
├── rag_pipeline.py                 # Hybrid RAG pipeline (Cypher + vector search)
├── requirements.txt
├── .env.example                    # Copy to .env and fill in credentials
│
├── config/
│   └── settings.py                 # All env vars loaded in one place
│
├── migrations/
│   └── 001_processing_state.sql    # Run once via: python main.py --migrate
│
├── pipeline/
│   ├── db.py                       # PostgreSQL connection pool + run_migration()
│   ├── models.py                   # Pydantic models (ExtractedEvent, ArticleExtractionResult)
│   ├── extractor.py                # Groq LLM call + response parsing
│   ├── embeddings.py               # fastembed embedding service (singleton)
│   └── orchestrator.py             # Full pipeline run logic
│
├── graphdb/
│   └── graph_writer.py             # Neo4j driver + node/relationship writers
│
├── scheduler/
│   └── daily_scheduler.py          # APScheduler daily cron job
│
└── tests/
    ├── test_extractor.py           # Unit tests for extraction (Groq mocked)
    └── test_json_failure.py        # Unit tests for JSON parse failure + retry logic
```

---

## Setup

### 1. Install dependencies

```bash
cd module2
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

Required values in `.env`:

| Variable                                | Description                                |
| --------------------------------------- | ------------------------------------------ |
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | Phase 1 database                           |
| `NEO4J_URI`                           | Default:`bolt://localhost:7687`          |
| `NEO4J_USER / NEO4J_PASSWORD`         | Neo4j credentials                          |
| `GROQ_API_KEY`                        | Your Groq API key                          |
| `GROQ_MODEL`                          | Default:`gpt-oss-120b`                   |
| `EMBEDDING_MODEL`                     | Default:`BAAI/bge-small-en-v1.5`         |
| `LLM_MAX_RETRIES`                     | Default:`3`                              |
| `BATCH_SIZE`                          | Articles processed per run. Default:`20` |
| `SCHEDULER_HOUR`                      | UTC hour for daily run. Default:`6`      |
| `SCHEDULER_MINUTE`                    | UTC minute for daily run. Default:`0`    |

### 3. Apply migration (run once)

```bash
python main.py --migrate
```

This creates `processing_state` in PostgreSQL and sets up Neo4j constraints and vector indexes.

---

## Module 2 — Running the Pipeline

### One-off run (testing / manual backfill)

```bash
python main.py --run-now
```

### Daily scheduled mode (production)

```bash
python main.py --schedule
```

Runs every day at `SCHEDULER_HOUR:SCHEDULER_MINUTE` UTC (default 06:00).

### Run tests

```bash
pytest tests/ -v
```

---

## Processing State Lifecycle

```
[new article in DB] ──► pending ──► processing ──► completed
                                                 └──► skipped   (no events found)
                                         │
                                         └──► failed  (retried up to LLM_MAX_RETRIES, then abandoned)
```

---

## Module 3 — Running the API

### Start the API server

```bash
uvicorn api:app --reload --port 8000
```

### Interactive API docs

Once running, visit:

```
http://127.0.0.1:8000/docs
```

FastAPI's built-in Swagger UI — all four endpoints are testable directly from the browser without Postman or curl.

---

## Module 3 — API Endpoints

### GET /api/summary/daily

No input required. Returns a plain English summary of the most recent market events in the knowledge graph.

```json
// Response
{
  "summary": "Today's key market events include..."
}
```

---

### POST /api/themes

Takes a natural language query. Returns key macro events and themes driving markets.

```json
// Request
{ "query": "What is causing volatility in Nordic markets right now?" }

// Response
{ "themes": "Nordic market volatility is being driven by..." }
```

---

### POST /api/classification

Takes a natural language query. Returns market events classified by asset class, sector, region or any combination. No separate filter fields needed — the semantic layer handles interpretation.

```json
// Request
{ "query": "Show me events affecting Nordic Financials this week" }

// Response
{ "results": "The following events affected Nordic Financials..." }
```

---

### POST /api/qa

Conversational Q&A with persistent session history. Returns an answer and a session ID. Pass the session ID on subsequent calls to maintain context across turns.

```json
// First call — no history_id needed
{ "question": "What happened in Nordic markets today?" }

// Response
{ "answer": "...", "history_id": "abc-123" }

// Follow-up call — pass history_id to maintain context
{ "question": "Which companies were most affected?", "history_id": "abc-123" }

// Response
{ "answer": "...", "history_id": "abc-123" }
```

---

## Module 3 — How Hybrid Retrieval Works

Each query goes through two retrieval steps before the final answer is generated:

**Step 1 — Cypher generation**
The question is sent to the LLM with the full graph schema. The LLM generates a Cypher query that traverses the graph for structured analytical results — sector rankings by sentiment score, events filtered by region and date, person statements linked to company impacts, etc.

**Step 2 — Vector similarity search**
The question is converted into a 384-dimensional embedding using `BAAI/bge-small-en-v1.5` via fastembed (runs locally, no API cost). This embedding is compared against the event vector index in Neo4j. The top 5 most semantically similar events are returned regardless of exact keyword matching.

**Step 3 — Answer generation**
Both results are combined into a single context and passed to the LLM with a financial research assistant system prompt. The LLM synthesises a coherent natural language answer grounded in actual graph data.

---

## Switching to Azure OpenAI

When client credentials arrive, only `pipeline/extractor.py` and `rag_pipeline.py` need updating:

1. Replace the `groq` import with `openai`
2. Point the client at the Azure endpoint
3. Update `GROQ_MODEL` → `AZURE_OPENAI_DEPLOYMENT` in `.env`

Everything else (orchestrator, Neo4j writer, scheduler, tests, API layer) stays identical.

---

## Example Cypher Queries

```cypher
-- All events affecting Nordic markets tagged with Inflation
MATCH (e:Event)-[r:AFFECTS_REGION]->(reg:Region {name: 'Nordic'})
MATCH (e)-[:TAGGED_WITH]->(t:Theme {name: 'Inflation'})
RETURN e.summary, e.event_date, r.sentiment_score, r.impact_reason
ORDER BY e.event_date DESC;

-- Sectors most negatively affected this week
MATCH (e:Event)-[r:AFFECTS_SECTOR]->(s:Sector)
WHERE e.event_date >= date() - duration('P7D')
RETURN s.name, avg(r.sentiment_score) AS avg_impact
ORDER BY avg_impact ASC
LIMIT 10;

-- All events affecting a specific company with sentiment
MATCH (e:Event)-[r:AFFECTS]->(c:Company {name: 'Nordea Bank'})
MATCH (a:Article)-[:CONTAINS]->(e)
RETURN a.title, a.url, e.summary, r.sentiment_score, r.impact_reason
ORDER BY e.event_date DESC;

-- What a person said and what they implied
MATCH (p:Person {name: 'Christine Lagarde'})-[r:STATED]->(e:Event)
RETURN r.quote_summary, r.statement_direction, r.role, r.statement_date
ORDER BY r.statement_date DESC;

-- Most common themes this week
MATCH (e:Event)-[:TAGGED_WITH]->(t:Theme)
WHERE e.event_date >= date() - duration('P7D')
RETURN t.name, count(e) AS event_count
ORDER BY event_count DESC;

-- Events across all asset classes
MATCH (e:Event)-[:CLASSIFIED_AS]->(ac:AssetClass)
RETURN ac.name, count(e) AS count
ORDER BY count DESC;

-- Direct person to company impact chain
MATCH (p:Person)-[r:IMPACTS]->(c:Company)
RETURN p.name, c.name, r.sentiment_score, r.impact_reason, r.via_event_id
ORDER BY r.sentiment_score ASC;
```
