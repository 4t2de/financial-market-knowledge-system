"""
Central settings — loaded once from environment / .env file.
All other modules import from here. Never read os.getenv directly elsewhere.
"""
# this file contains the settings regarding the PostgreSQL database connection, 
# Neo4j GraphDB connection and other config settings required for functioning properly,
# and are subject to change as per use.
# Most of the config data is loaded from .env file itself. 

import os
import json
from pathlib import Path
from dotenv import load_dotenv

# Calculate path to root .env (Up two levels: config -> phase2&3 -> root)
root_path = Path(__file__).resolve().parent.parent.parent
env_path = root_path / '.env'

load_dotenv(dotenv_path=env_path)

# ── PostgreSQL ────────────────────────────────────────────────────────────────
POSTGRES_HOST     = os.getenv("DB_HOST", "localhost")
POSTGRES_PORT     = int(os.getenv("DB_PORT", 5432))
POSTGRES_DB       = os.getenv("DB_NAME", "scraped_data")
POSTGRES_USER     = os.getenv("DB_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("DB_PASSWORD", "root")

# ── Neo4j ─────────────────────────────────────────────────────────────────────
NEO4J_URI      = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

# ── Groq ──────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "gpt-oss-120b")

# ── Embedding ─────────────────────────────────────────────────────────────────
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")

# ── Pipeline ──────────────────────────────────────────────────────────────────
BATCH_SIZE       = int(os.getenv("BATCH_SIZE", 20))
SCHEDULER_HOUR   = int(os.getenv("SCHEDULER_HOUR", 6))
SCHEDULER_MINUTE = int(os.getenv("SCHEDULER_MINUTE", 0))
LLM_MAX_RETRIES  = int(os.getenv("LLM_MAX_RETRIES", 3))