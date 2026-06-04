"""
Module 2 Entrypoint
--------------------
Usage:
    # Apply the DB migration first (run once before first use)
    python main.py --migrate

    # Run the pipeline once right now (useful for testing / manual backfill)
    python main.py --run-now

    # Start the daily scheduler (production mode)
    python main.py --schedule
"""
import argparse
import os
import sys

from loguru import logger

# ── Logging setup ─────────────────────────────────────────────────────────────
# Configure the logger
logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    level="DEBUG",
    colorize=True,
)
logger.add(
    "logs/module2_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="14 days",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
)


# Run the pipeline according to the command line argument received.
def main():
    parser = argparse.ArgumentParser(
        description="Phase 2 — Knowledge Pre-Processing Core"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run-now",  action="store_true", help="Run the pipeline once immediately.")
    group.add_argument("--schedule", action="store_true", help="Start the daily scheduler.")
    group.add_argument("--migrate",  action="store_true", help="Apply DB migration and Neo4j schema (run once).")
    args = parser.parse_args()

    os.makedirs("logs", exist_ok=True)

    if args.migrate:
        from pipeline.db import run_migration
        from graphdb.graph_writer import ensure_schema
        logger.info("Applying PostgreSQL migration...")
        migration_path = os.path.join(os.path.dirname(__file__), "migrations", "001_processing_state.sql")
        run_migration(migration_path)
        logger.info("Applying Neo4j schema constraints...")
        ensure_schema()
        logger.success("Migration complete. You are ready to run the pipeline.")

    elif args.run_now:
        from graphdb.graph_writer import ensure_schema, close_driver
        from pipeline.orchestrator import run_pipeline
        ensure_schema()
        try:
            stats = run_pipeline()
            logger.success(f"Pipeline finished: {stats}")
        finally:
            close_driver()

    elif args.schedule:
        from graphdb.graph_writer import ensure_schema
        from scheduler.daily_scheduler import start
        ensure_schema()
        start()


if __name__ == "__main__":
    main()
