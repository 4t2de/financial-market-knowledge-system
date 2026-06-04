"""
Neo4j Graph Writer
------------------
Manages the connection to Neo4j and writes extracted events as a property graph.

Graph model:
  (Article)-[:CONTAINS]->(Event)
  (Event)-[:CLASSIFIED_AS]->(AssetClass)
  (Event)-[:AFFECTS_SECTOR {sentiment_score, impact_reason, confidence_score}]->(Sector)
  (Event)-[:AFFECTS_REGION {sentiment_score, impact_reason, confidence_score}]->(Region)
  (Event)-[:TAGGED_WITH]->(Theme)
  (Event)-[:AFFECTS {sentiment_score, impact_reason, confidence_score}]->(Company)
  (Event)-[:AFFECTS {sentiment_score, impact_reason, confidence_score}]->(Organisation)
  (Person)-[:STATED {quote_summary, statement_direction, role, statement_date, confidence_score}]->(Event)
  (Person)-[:IMPACTS {sentiment_score, impact_reason, confidence_score, via_event_id}]->(Company)

Every relationship that carries analytical meaning has sentiment_score,
impact_reason and confidence_score. Bare relationships (CONTAINS, CLASSIFIED_AS,
TAGGED_WITH) are structural only.
"""
# the above comment contains the description for the schema for GraphDB.


import uuid
from typing import Optional
from neo4j import GraphDatabase, Driver
from loguru import logger

from config.settings import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
from pipeline.models import ArticleExtractionResult, ExtractedEvent

_driver: Optional[Driver] = None


# connection init
def get_driver() -> Driver:
    global _driver
    if _driver is None:
        uri = NEO4J_URI.replace("neo4j+s://", "neo4j+ssc://")
        _driver = GraphDatabase.driver(
            uri,
            auth=(NEO4J_USER, NEO4J_PASSWORD),
        )
        logger.info("Neo4j driver initialised.")
    return _driver


def close_driver() -> None:
    global _driver
    if _driver:
        _driver.close()
        _driver = None


# constraints for data, ex. imposing primary key characteristics for nodes & their properties.
# ── Schema / constraints ──────────────────────────────────────────────────────
CONSTRAINT_QUERIES = [
    "CREATE CONSTRAINT article_id IF NOT EXISTS FOR (a:Article) REQUIRE a.article_id IS UNIQUE",
    "CREATE CONSTRAINT event_id IF NOT EXISTS FOR (e:Event) REQUIRE e.event_id IS UNIQUE",
    "CREATE CONSTRAINT asset_class_name IF NOT EXISTS FOR (ac:AssetClass) REQUIRE ac.name IS UNIQUE",
    "CREATE CONSTRAINT sector_name IF NOT EXISTS FOR (s:Sector) REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT region_name IF NOT EXISTS FOR (r:Region) REQUIRE r.name IS UNIQUE",
    "CREATE CONSTRAINT theme_name IF NOT EXISTS FOR (t:Theme) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT company_name IF NOT EXISTS FOR (c:Company) REQUIRE c.name IS UNIQUE",
    "CREATE CONSTRAINT person_name IF NOT EXISTS FOR (p:Person) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT organisation_name IF NOT EXISTS FOR (o:Organisation) REQUIRE o.name IS UNIQUE",
]


# used for similarity search, the DB is indexed for the same.
VECTOR_INDEX_QUERIES = [
    # embedding size 384 for BAAI/bge-small-en-v1.5
    "CREATE VECTOR INDEX event_embeddings IF NOT EXISTS FOR (e:Event) ON (e.embedding) "
    "OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}",
    "CREATE VECTOR INDEX article_embeddings IF NOT EXISTS FOR (a:Article) ON (a.embedding) "
    "OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}",
]


# constraints and schema are verified
def ensure_schema() -> None:
    """Idempotently create Neo4j constraints. Safe to run on every startup."""
    driver = get_driver()
    with driver.session() as session:
        for q in CONSTRAINT_QUERIES:
            try:
                session.run(q)
            except Exception as e:
                logger.warning(f"Constraint query warning: {e}")

        for q in VECTOR_INDEX_QUERIES:
            try:
                session.run(q)
            except Exception as e:
                logger.warning(f"Vector index query warning: {e}")

    logger.info("Neo4j schema constraints and vector indexes verified.")


# ── Write helpers ─────────────────────────────────────────────────────────────


# all of the queries below are plug-and-play cypher queries written to insert data,
# depending on which node is inserted.
# each different type of node gets a different query,
# since each of those nodes & relationships have different properties.

def _write_article_node(tx, article: dict) -> None:
    tx.run(
        """
        MERGE (a:Article {article_id: $article_id})
        SET a.title        = $title,
            a.url          = $url,
            a.source       = $source,
            a.published_at = $published_at,
            a.embedding    = $embedding
        """,
        article_id=str(article["id"]),
        title=article["title"],
        url=article["url"],
        source=article.get("source_name", "unknown"),
        published_at=str(article.get("published_at", "")),
        embedding=article.get("embedding", []),
    )


def _write_event_node(
    tx,
    event_id: str,
    event: ExtractedEvent,
    article_id: int,
    published_at: str,
) -> None:
    tx.run(
        """
        MERGE (e:Event {event_id: $event_id})
        SET e.summary           = $summary,
            e.event_type        = $event_type,
            e.overall_direction = $overall_direction,
            e.confidence_score  = $confidence_score,
            e.event_date        = $event_date,
            e.embedding         = $embedding
        WITH e
        MATCH (a:Article {article_id: $article_id})
        MERGE (a)-[:CONTAINS]->(e)
        """,
        event_id=event_id,
        summary=event.summary,
        event_type=event.event_type or "OTHER",
        overall_direction=event.overall_direction or "neutral",
        confidence_score=event.confidence_score,
        event_date=published_at,
        article_id=str(article_id),
        embedding=event.embedding,
    )

# these are a group of queries written to imbue properties in the relationships between the nodes.
# this helps us to maintain state, and collect state and context relevant information according to occurred events
# and obtain better response quality in user questions.

def _write_taxonomy_relationships(
    tx,
    event_id: str,
    event: ExtractedEvent,
    published_at: str,
) -> None:
    # Asset classes — structural only, no sentiment needed
    for name in event.asset_classes:
        tx.run(
            """
            MATCH (e:Event {event_id: $event_id})
            MERGE (n:AssetClass {name: $name})
            MERGE (e)-[:CLASSIFIED_AS]->(n)
            """,
            event_id=event_id,
            name=name,
        )

    # Sectors — now carry sentiment_score, impact_reason, confidence_score
    for sector in event.sectors:
        tx.run(
            """
            MATCH (e:Event {event_id: $event_id})
            MERGE (s:Sector {name: $name})
            MERGE (e)-[r:AFFECTS_SECTOR]->(s)
            SET r.sentiment_score  = $sentiment_score,
                r.impact_reason    = $impact_reason,
                r.confidence_score = $confidence_score
            """,
            event_id=event_id,
            name=sector.name,
            sentiment_score=sector.sentiment_score,
            impact_reason=sector.impact_reason,
            confidence_score=sector.confidence_score,
        )

    # Regions — now carry sentiment_score, impact_reason, confidence_score
    for region in event.regions:
        tx.run(
            """
            MATCH (e:Event {event_id: $event_id})
            MERGE (r:Region {name: $name})
            MERGE (e)-[rel:AFFECTS_REGION]->(r)
            SET rel.sentiment_score  = $sentiment_score,
                rel.impact_reason    = $impact_reason,
                rel.confidence_score = $confidence_score
            """,
            event_id=event_id,
            name=region.name,
            sentiment_score=region.sentiment_score,
            impact_reason=region.impact_reason,
            confidence_score=region.confidence_score,
        )

    # Themes — structural only, no sentiment needed
    for name in event.themes:
        tx.run(
            """
            MATCH (e:Event {event_id: $event_id})
            MERGE (n:Theme {name: $name})
            MERGE (e)-[:TAGGED_WITH]->(n)
            """,
            event_id=event_id,
            name=name,
        )

    # Companies
    for company in event.companies:
        tx.run(
            """
            MATCH (e:Event {event_id: $event_id})
            MERGE (c:Company {name: $name})
            MERGE (e)-[r:AFFECTS]->(c)
            SET r.sentiment_score  = $sentiment_score,
                r.impact_reason    = $impact_reason,
                r.confidence_score = $confidence_score
            """,
            event_id=event_id,
            name=company.name,
            sentiment_score=company.sentiment_score,
            impact_reason=company.impact_reason,
            confidence_score=company.confidence_score,
        )

    # Organisations
    for org in event.organisations:
        tx.run(
            """
            MATCH (e:Event {event_id: $event_id})
            MERGE (o:Organisation {name: $name})
            MERGE (e)-[r:AFFECTS]->(o)
            SET r.sentiment_score  = $sentiment_score,
                r.impact_reason    = $impact_reason,
                r.confidence_score = $confidence_score
            """,
            event_id=event_id,
            name=org.name,
            sentiment_score=org.sentiment_score,
            impact_reason=org.impact_reason,
            confidence_score=org.confidence_score,
        )

    # Persons — STATED → Event + direct IMPACTS → Company
    for person in event.persons:
        tx.run(
            """
            MATCH (e:Event {event_id: $event_id})
            MERGE (p:Person {name: $name})
            MERGE (p)-[r:STATED]->(e)
            SET r.quote_summary       = $quote_summary,
                r.statement_direction = $statement_direction,
                r.role                = $role,
                r.statement_date      = $statement_date,
                r.confidence_score    = $confidence_score
            """,
            event_id=event_id,
            name=person.name,
            quote_summary=person.quote_summary,
            statement_direction=person.statement_direction,
            role=person.role,
            statement_date=published_at,
            confidence_score=person.confidence_score,
        )
        for ci in person.company_impacts:
            tx.run(
                """
                MERGE (p:Person {name: $person_name})
                MERGE (c:Company {name: $company_name})
                MERGE (p)-[r:IMPACTS]->(c)
                SET r.sentiment_score  = $sentiment_score,
                    r.impact_reason    = $impact_reason,
                    r.confidence_score = $confidence_score,
                    r.via_event_id     = $via_event_id
                """,
                person_name=person.name,
                company_name=ci.company_name,
                sentiment_score=ci.sentiment_score,
                impact_reason=ci.impact_reason,
                confidence_score=ci.confidence_score,
                via_event_id=event_id,
            )


# once the structured data from each article is obtained from the LLM call in json format,
# it is validated using Pydantic to ensure the schema is maintained in the response.
# once everything is correct, the data is plugged into the cypher queries (above) to insert into the graph database,
# starting from this function.

def write_extraction_result(
    result: ArticleExtractionResult,
    article: dict,
) -> list[str]:
    """
    Write a full ArticleExtractionResult into Neo4j.
    Returns list of event_ids written (used to store in processing_state).
    """
    if not result.events:
        logger.info(
            f"Article {result.article_id}: no events to write — marking skipped."
        )
        return []

    driver = get_driver()
    event_ids: list[str] = []
    published_at = str(article.get("published_at", ""))

    with driver.session() as session:
        session.execute_write(_write_article_node, article)

        for event in result.events:
            event_id = str(uuid.uuid4())
            event_ids.append(event_id)
            session.execute_write(
                _write_event_node,
                event_id,
                event,
                result.article_id,
                published_at,
            )
            session.execute_write(
                _write_taxonomy_relationships,
                event_id,
                event,
                published_at,
            )

    logger.info(
        f"Article {result.article_id}: wrote {len(event_ids)} event node(s) to Neo4j."
    )
    return event_ids
