# Pipeline Operations Guide

This guide provides the step-by-step commands to run the complete financial market knowledge pipeline, from data scraping to knowledge graph migration and API querying.

---

## Prerequisites

Ensure you are in the project root directory and the virtual environment is activated.

```powershell
# Navigate to project root
# Activate virtual environment
.\.venv\Scripts\Activate.ps1
```

---

## Step 1: Phase 1 - Data Extraction (PostgreSQL)

This step scrapes news articles from defined sources (BBC, Al Jazeera, AP News, Nordnet, Morningstar) and stores them in the PostgreSQL database.

```powershell
# Navigate to phase1 directory
# Run the consolidated scraper
# Note: Ensure .env and config.yaml are correctly configured
python main.py
```

**Verification:** You should see logs indicating articles are being fetched and inserted/skipped.

---

## Step 2: Phase 2 - Knowledge Pre-Processing & Neo4j Migration

This step extracts structured financial events from the scraped articles using LLMs and migrates them to the Neo4j Graph database.

```powershell
# Navigate to phase2 directory
# Run the orchestrator pipeline
# --run-now processes any unprocessed articles immediately
python main.py --run-now
```

**Verification:** The logs will show article extraction results and successful writes to Neo4j.

---

## Step 3: Module 3 - Previously FastAPI, now replaced with a MCP Server

1. Run `mcp_server.py` from `\phase2and3`.
2. Once the server is running, install Claude, or any other MCP client and set the client to use the MCP tools developed.
3. While usage, explicitly specify to use the given connector through which the MCP server is connected.

---

## Configuration Reminders

- **Phase 1 `.env`**: Contains PostgreSQL credentials.
- **Phase 1 `config.yaml`**: Controls enabled scrapers and article limits.
- **Phase 2 `.env`**: Contains Neo4j and Groq API keys.
