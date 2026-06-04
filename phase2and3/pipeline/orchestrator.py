"""
Pipeline Orchestrator
---------------------
Coordinates the full Module 2 flow for a single pipeline run:

  1. Fetch unprocessed articles from PostgreSQL (via processing_state)
  2. For each article:
      a. Mark as 'processing' in processing_state
      b. Call Groq LLM to extract events
      c. Write events to Neo4j
      d. Mark as 'completed' (or 'failed'/'skipped') in processing_state
  3. Log a summary

This is called by the scheduler once per day, or can be run manually.
"""

import traceback
from datetime import datetime, timezone

import psycopg2.extras
from loguru import logger

from config.settings import BATCH_SIZE
from pipeline.db import get_conn
from pipeline.extractor import extract_events
from pipeline.models import ArticleExtractionResult
from pipeline.embeddings import embedding_service
from graphdb.graph_writer import write_extraction_result


# ── State helpers ─────────────────────────────────────────────────────────────

# Function used to extract only those articles from the database which are not processed.
# A maximum number of 'x' articles can be seeded at once, where x can be defined as per use.

def _seed_processing_state(conn) -> int:
    """
    Insert rows into processing_state for any articles that have no row yet.
    This is the bridge between Phase 1 (articles table) and Module 2.
    Returns number of new rows inserted.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO processing_state (article_id, status)
            SELECT a.id, 'pending'
            FROM   articles a
            LEFT JOIN processing_state ps ON ps.article_id = a.id
            WHERE  ps.id IS NULL
            ON CONFLICT (article_id) DO NOTHING
            """
        )
        return cur.rowcount


# Fetches only pending articles from the database.

def _fetch_pending_articles(conn, batch_size: int) -> list[dict]:
    """
    Fetch the next batch of articles to process.
    Priority: pending first, then failed (for retry), ordered by published_at DESC.
    Excludes articles that have failed more than 3 times.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                a.id,
                a.title,
                a.content,
                a.url,
                a.published_at,
                s.name AS source_name,
                ps.id  AS ps_id,
                ps.attempts
            FROM   processing_state ps
            JOIN   articles a ON a.id = ps.article_id
            JOIN   sources  s ON s.id = a.source_id
            WHERE  ps.status IN ('pending', 'failed')
              AND  ps.attempts < 3
            ORDER BY a.published_at DESC NULLS LAST
            LIMIT  %s
            FOR UPDATE OF ps SKIP LOCKED
            """,
            (batch_size,),
        )
        return [dict(row) for row in cur.fetchall()]

# While the given article is being processed, temporarily mark its state as 'processing'.

def _mark_processing(conn, ps_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE processing_state
            SET    status = 'processing',
                   attempts = attempts + 1,
                   last_attempted = NOW()
            WHERE  id = %s
            """,
            (ps_id,),
        )

# Once the article is done processing, mark its state.
# Whether completed successfully, if some error was occurred, as per the states explained in the migration sql file.

def _mark_completed(conn, ps_id: int, event_ids: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE processing_state
            SET    status = %s,
                   completed_at = NOW(),
                   neo4j_event_ids = %s,
                   error_message = NULL
            WHERE  id = %s
            """,
            (
                "skipped" if not event_ids else "completed",
                event_ids or [],
                ps_id,
            ),
        )

# For any articles that could not be processed even after multiple retries, mark them as failed.

def _mark_failed(conn, ps_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE processing_state
            SET    status = 'failed',
                   error_message = %s
            WHERE  id = %s
            """,
            (error[:2000], ps_id),  # cap error length
        )


# ── Main pipeline run ─────────────────────────────────────────────────────────

# Run the main pipeline to fetch, process articles and write data in the GraphDB.

def run_pipeline() -> dict:
    """
    Execute one full pipeline run.
    Returns a summary dict: {processed, completed, skipped, failed, seeded}
    """
    run_start = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info(f"Module 2 pipeline run started at {run_start.isoformat()}")

    stats = {"seeded": 0, "processed": 0, "completed": 0, "skipped": 0, "failed": 0}

    # ── Step 1: Seed processing_state with any new articles ───────────────────
    with get_conn() as conn:
        seeded = _seed_processing_state(conn)
        stats["seeded"] = seeded
        if seeded:
            logger.info(f"Seeded {seeded} new article(s) into processing_state.")

    # ── Step 2: Process in batches ────────────────────────────────────────────
    while True:
        with get_conn() as conn:
            articles = _fetch_pending_articles(conn, BATCH_SIZE)
            if not articles:
                break

            logger.info(f"Processing batch of {len(articles)} article(s)...")

            for article in articles:
                ps_id = article["ps_id"]
                article_id = article["id"]

                _mark_processing(conn, ps_id)
                conn.commit()  # release lock early so other workers can proceed

                try:
                    # ── LLM extraction ────────────────────────────────────────
                    result: ArticleExtractionResult = extract_events(
                        article_id=article_id,
                        title=article["title"],
                        content=article["content"],
                        source=article["source_name"],
                    )

                    # ── Generate Embeddings ───────────────────────────────────
                    # 1. Full article embedding
                    article_text = f"{article['title']}\n\n{article['content']}"
                    result.embedding = embedding_service.get_embeddings(article_text)
                    article["embedding"] = result.embedding  # for _write_article_node

                    # 2. Per-event embeddings
                    for event in result.events:
                        event.embedding = embedding_service.get_embeddings(
                            event.summary
                        )

                    # ── Neo4j write ───────────────────────────────────────────
                    event_ids = write_extraction_result(result, article)

                    # ── Update state ──────────────────────────────────────────
                    with get_conn() as inner_conn:
                        _mark_completed(inner_conn, ps_id, event_ids)

                    stats["processed"] += 1
                    if event_ids:
                        stats["completed"] += 1
                        logger.success(
                            f"  ✓ Article {article_id} → {len(event_ids)} event(s) written."
                        )
                    else:
                        stats["skipped"] += 1
                        logger.info(
                            f"  ○ Article {article_id} → no events found (skipped)."
                        )

                except Exception as e:
                    err_msg = f"{type(e).__name__}: {e}"
                    logger.error(f"  ✗ Article {article_id} failed — {err_msg}")
                    logger.debug(traceback.format_exc())
                    with get_conn() as inner_conn:
                        _mark_failed(inner_conn, ps_id, err_msg)
                    stats["failed"] += 1

        # If this batch was smaller than BATCH_SIZE, we're done
        if len(articles) < BATCH_SIZE:
            break

    elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
    logger.info(
        f"Pipeline run complete in {elapsed:.1f}s — "
        f"seeded={stats['seeded']}, processed={stats['processed']}, "
        f"completed={stats['completed']}, skipped={stats['skipped']}, "
        f"failed={stats['failed']}"
    )
    logger.info("=" * 60)
    return stats
