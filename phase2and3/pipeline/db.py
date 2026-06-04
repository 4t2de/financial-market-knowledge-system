"""
PostgreSQL connection pool.
All Module 2 code uses get_conn() as a context manager — never raw psycopg2.
"""
import psycopg2
import psycopg2.pool
from contextlib import contextmanager
from loguru import logger
from config.settings import (
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
    POSTGRES_USER, POSTGRES_PASSWORD
)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


# estabiishes database connection
def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=POSTGRES_HOST,
            port=POSTGRES_PORT,
            dbname=POSTGRES_DB,
            user=POSTGRES_USER,
            password=POSTGRES_PASSWORD,
        )
        logger.info("PostgreSQL connection pool created.")
    return _pool


# this function is required to yield a psycopg2 connection for the database. 
@contextmanager
def get_conn():
    """Yields a psycopg2 connection. Auto-commits on success, rolls back on error."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

# this function is called when you first run the main.py file, during setup:
# as python main.py --migrate.
# creates the processing_state table in the PostgreSQL database.
def run_migration(sql_path: str) -> None:
    """Execute a raw SQL migration file."""
    with open(sql_path, "r") as f:
        sql = f.read()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    logger.info(f"Migration applied: {sql_path}")
