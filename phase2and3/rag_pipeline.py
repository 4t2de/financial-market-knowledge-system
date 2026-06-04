"""
RAG Pipeline
------------
Hybrid retrieval: Cypher graph traversal + vector similarity search.
LLM generates the final answer from combined context.
"""
from typing import Optional, List, Dict, Any
from datetime import date as date_type, timedelta
import json
import re
from loguru import logger
from groq import Groq

from config.settings import GROQ_API_KEY, GROQ_MODEL
from pipeline.embeddings import embedding_service
from graphdb.graph_writer import get_driver, ensure_schema

ensure_schema()

# prompt containing the intended behavior for the LLM.

SYSTEM_PROMPT = """You are a senior investment research assistant.
Use the provided context from the financial news graph database to answer the user's question accurately.
If the answer cannot be found in the context, state that you don't have enough information.
Keep your answer professional, concise, and grounded in the facts provided.
"""

# prompt defining the graphdb schema in order to generate appropriate cypher queries. 

CYPHER_GENERATION_PROMPT = """Generate a Neo4j Cypher query to answer a question about financial market events.

Schema:
- Article (article_id, title, url, source, published_at)
- Event (event_id, summary, event_type, overall_direction, confidence_score, event_date)
- AssetClass (name) | Sector (name) | Region (name) | Theme (name) | Company (name) | Organisation (name) | Person (name)

Relationships:
(Article)-[:CONTAINS]->(Event)
(Event)-[:CLASSIFIED_AS]->(AssetClass)
(Event)-[:AFFECTS_SECTOR {sentiment_score, impact_reason, confidence_score}]->(Sector)
(Event)-[:AFFECTS_REGION {sentiment_score, impact_reason, confidence_score}]->(Region)
(Event)-[:TAGGED_WITH]->(Theme)
(Event)-[:AFFECTS {sentiment_score, impact_reason, confidence_score}]->(Company|Organisation)
(Person)-[:STATED {quote_summary, statement_direction, role, statement_date, confidence_score}]->(Event)
(Person)-[:IMPACTS {sentiment_score, impact_reason, confidence_score, via_event_id}]->(Company)

Key facts:
- event_date is a plain string 'YYYY-MM-DD HH:MM:SS'. Never wrap it with datetime() or date(). Filter with: WHERE e.event_date STARTS WITH 'YYYY-MM-DD'
- Never use $parameters — always hardcode date values as string literals like '2026-03-24'
- sentiment_score: -1.0 (very negative) to +1.0 (very positive)
- overall_direction: positive | negative | neutral
- statement_direction: hawkish | dovish | bullish | bearish | neutral
- event_type: RATE_DECISION, EARNINGS_REPORT, MERGER_ACQUISITION, POLICY_CHANGE, REGULATORY_ACTION, GEOPOLITICAL_EVENT, ECONOMIC_DATA_RELEASE, MARKET_MOVEMENT, LEGISLATIVE_PROPOSAL, SANCTIONS, IPO, LEADERSHIP_CHANGE, OTHER

Rules (strictly follow all):
1. Each MATCH block gets exactly ONE WHERE clause. Combine multiple conditions with AND — never write two WHERE lines in a row.
2. When RETURN contains collect() or any aggregation, alias every field used in ORDER BY inside RETURN and sort by that alias — never ORDER BY e.field after an aggregation.
3. Use toLower(n.name) CONTAINS toLower('value') for name matching.
4. Always end with LIMIT 10 unless the question implies otherwise.
5. Return ONLY the raw Cypher. No markdown, no explanation.
"""

# A safety function implemented to correct common LLM cypher mistakes.
# Processing date properly for correct retrieval, cypher query syntax mistakes are resolved.

def _sanitise_cypher(cypher: str) -> str:
    """
    Runtime safety net for common LLM Cypher mistakes.

    Fixes applied:
    1. datetime() / date() wrapping of event_date or $date parameter
    2. Consecutive WHERE clauses collapsed into AND
    3. ORDER BY using bare node variable after aggregation — aliases the field in RETURN
    4. Unresolved $params and range comparisons on event_date replaced with STARTS WITH today
    """
    today = date_type.today().strftime("%Y-%m-%d")

    # ── Fix 1: datetime() / date() wrapping ────────────────────────────────────
    cypher = re.sub(r"datetime\(\s*\$date\s*\)", f"'{today}'", cypher)
    cypher = re.sub(r"\bdate\(\s*\$date\s*\)", f"'{today}'", cypher)
    cypher = re.sub(r"datetime\(e\.event_date\)", "e.event_date", cypher)
    cypher = re.sub(
        r"datetime\('(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}'\)", r"'\1'", cypher
    )

    # ── Fix 4a: Range comparisons on event_date using $params ──────────────────
    # e.g.  e.event_date >= $startDate AND e.event_date < $endDate
    # →     e.event_date STARTS WITH 'YYYY-MM-DD'
    cypher = re.sub(
        r"e\.event_date\s*>=\s*\$\w+\s+AND\s+e\.event_date\s*<=?\s*\$\w+",
        f"e.event_date STARTS WITH '{today}'",
        cypher,
        flags=re.IGNORECASE,
    )

    # ── Fix 4b: Any remaining unresolved $param on event_date ──────────────────
    # e.g.  e.event_date STARTS WITH $startDate
    cypher = re.sub(
        r"(e\.event_date\s+STARTS\s+WITH\s+)\$\w+",
        rf"\1'{today}'",
        cypher,
        flags=re.IGNORECASE,
    )
    # e.g.  e.event_date = $someDate
    cypher = re.sub(
        r"e\.event_date\s*=\s*\$\w+",
        f"e.event_date STARTS WITH '{today}'",
        cypher,
        flags=re.IGNORECASE,
    )

    # ── Fix 4c: Catch-all — strip any remaining $param conditions ──────────────
    # Remove:  AND <anything containing $param>
    cypher = re.sub(
        r"\s+AND\s+[^\n]*\$\w+[^\n]*",
        "",
        cypher,
        flags=re.IGNORECASE,
    )
    # Remove:  WHERE <sole condition containing $param>  →  WHERE 1=1
    cypher = re.sub(
        r"\bWHERE\s+[^\n]*\$\w+[^\n]*",
        "WHERE 1=1",
        cypher,
        flags=re.IGNORECASE,
    )

    # ── Fix 2: Consecutive WHERE clauses → combine with AND ────────────────────
    consecutive_where = re.compile(
        r"(WHERE\s+.+?)\n(\s*)WHERE\s+",
        re.IGNORECASE | re.DOTALL,
    )
    for _ in range(10):
        new_cypher = consecutive_where.sub(r"\1\n\2  AND ", cypher)
        if new_cypher == cypher:
            break
        cypher = new_cypher

    # ── Fix 3: ORDER BY bare node variable after aggregation ───────────────────
    order_by_pattern = re.compile(
        r"ORDER\s+BY\s+e\.(\w+)(\s+(?:DESC|ASC))?", re.IGNORECASE
    )
    for match in order_by_pattern.finditer(cypher):
        field = match.group(1)
        alias = f"_sort_{field}"

        if "collect(" not in cypher.lower():
            continue

        already_aliased = re.search(
            rf"e\.{field}\s+AS\s+\w+", cypher, re.IGNORECASE
        )
        if already_aliased:
            existing_alias = re.search(
                rf"e\.{field}\s+AS\s+(\w+)", cypher, re.IGNORECASE
            ).group(1)
            cypher = re.sub(
                rf"ORDER\s+BY\s+e\.{field}(\s+(?:DESC|ASC))?",
                f"ORDER BY {existing_alias}\\1",
                cypher,
                flags=re.IGNORECASE,
            )
        else:
            cypher = re.sub(
                r"(RETURN\s+)",
                rf"\1e.{field} AS {alias}, ",
                cypher,
                count=1,
                flags=re.IGNORECASE,
            )
            cypher = re.sub(
                rf"ORDER\s+BY\s+e\.{field}(\s+(?:DESC|ASC))?",
                f"ORDER BY {alias}\\1",
                cypher,
                flags=re.IGNORECASE,
            )

    return cypher

# for the user query, the system_prompt along with the cypher_generation_prompt are concatenated in order to generate
# cypher queries for data traversal in the graph.

def generate_cypher(question: str) -> Optional[str]:
    """Use LLM to generate a Cypher query for the question."""
    client = Groq(api_key=GROQ_API_KEY)
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": CYPHER_GENERATION_PROMPT},
                {"role": "user", "content": question},
            ],
            temperature=0,
        )
        cypher = response.choices[0].message.content.strip()
        if "```cypher" in cypher:
            cypher = cypher.split("```cypher")[1].split("```")[0].strip()
        elif "```" in cypher:
            cypher = cypher.split("```")[1].strip()

        logger.info(f"Generated Cypher: {cypher}")
        return cypher
    except Exception as e:
        logger.error(f"Failed to generate Cypher: {e}")
        return None


# once a proper query is obtained, execute it on the GraphDB instance.

def execute_query(
    cypher: str, params: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Execute a Cypher query and return results."""
    driver = get_driver()
    with driver.session() as session:
        result = session.run(cypher, **(params or {}))
        return [record.data() for record in result]

# Vector similarity search code. The user query is transformed into embeddings and the top_k most similar article/event nodes are extracted
# to form context, which can then be merged with the traversal results in order to answer the user query.

def retrieve_context(
    query: str,
    top_k: int = 5,
    event_date: Optional[str] = None,
) -> str:
    """
    Hybrid search: Cypher graph traversal + vector similarity search.

    event_date: if provided (YYYY-MM-DD), injected into the query so the
    Cypher generator knows exactly which date to filter on.
    """
    context_parts = []

    # Enrich query with explicit date context so Cypher generator uses STARTS WITH correctly
    if event_date:
        enriched_query = (
            f"{query} "
            f"[Date filter: only include events where event_date STARTS WITH '{event_date}']"
        )
    else:
        enriched_query = query

    # ── 1. Structured Cypher query ─────────────────────────────────────────────
    cypher = generate_cypher(enriched_query)
    if cypher and "MATCH" in cypher.upper():
        cypher = _sanitise_cypher(cypher)
        try:
            results = execute_query(cypher)
            if results:
                context_parts.append("### Structured Knowledge (from Graph Query):")
                for res in results:
                    context_parts.append(json.dumps(res, indent=2, default=str))
        except Exception as e:
            logger.error(f"Generated Cypher failed to execute: {e}\nCypher was: {cypher}")

    # ── 2. Vector similarity search ────────────────────────────────────────────
    query_embedding = embedding_service.get_embeddings(query)
    if not query_embedding:
        logger.warning("Empty embedding returned for query, skipping vector search.")
    else:
        # cypher query for simiarity search
        vector_cypher = """
        CALL db.index.vector.queryNodes('event_embeddings', $top_k, $embedding)
        YIELD node AS event, score
        MATCH (a:Article)-[:CONTAINS]->(event)
        OPTIONAL MATCH (event)-[rs:AFFECTS_SECTOR]->(s:Sector)
        OPTIONAL MATCH (event)-[rr:AFFECTS_REGION]->(r:Region)
        RETURN
            event.summary           AS summary,
            event.event_type        AS type,
            event.overall_direction AS direction,
            event.event_date        AS date,
            a.title                 AS article_title,
            a.source                AS source,
            collect(DISTINCT {
                sector:          s.name,
                sentiment_score: rs.sentiment_score,
                impact_reason:   rs.impact_reason
            }) AS sector_impacts,
            collect(DISTINCT {
                region:          r.name,
                sentiment_score: rr.sentiment_score,
                impact_reason:   rr.impact_reason
            }) AS region_impacts,
            score
        ORDER BY score DESC
        """
        try:
            vector_results = execute_query(
                vector_cypher, {"embedding": query_embedding, "top_k": top_k}
            )
            if vector_results:
                context_parts.append("\n### Semantic Knowledge (from Vector Search):")
                for record in vector_results:
                    part = (
                        f"Article: {record['article_title']} ({record['source']}, {record['date']})\n"
                        f"Event: {record['type']} [{record['direction']}] — {record['summary']}\n"
                    )
                    if record.get("sector_impacts") and record["sector_impacts"][0].get("sector"):
                        sectors = ", ".join(
                            f"{s['sector']} ({s['sentiment_score']:+.2f})"
                            for s in record["sector_impacts"] if s.get("sector")
                        )
                        part += f"Sectors: {sectors}\n"
                    if record.get("region_impacts") and record["region_impacts"][0].get("region"):
                        regions = ", ".join(
                            f"{r['region']} ({r['sentiment_score']:+.2f})"
                            for r in record["region_impacts"] if r.get("region")
                        )
                        part += f"Regions: {regions}\n"
                    context_parts.append(part)
        except Exception as e:
            logger.error(f"Vector search failed: {e}")

    logger.debug(f"Context parts assembled: {len(context_parts)}")

    if not context_parts:
        return "No relevant context found in the database."

    return "\n\n".join(context_parts)


# Final API call, where context from similarity search and results from traversal are concatenated
# Structured response for the data is obtained as the result to the user query.

def ask_question(
    question: str,
    conversation_history: Optional[List[Dict]] = None,
    event_date: Optional[str] = None,
) -> str:
    """
    Full hybrid RAG flow.
    conversation_history: list of {"role": "user"|"assistant", "content": str}
    event_date: YYYY-MM-DD string, passed through to retrieve_context for date filtering
    """
    logger.info(f"Answering question: {question}")
    context = retrieve_context(question, event_date=event_date)
    client = Groq(api_key=GROQ_API_KEY)

    user_prompt = f"""Context from knowledge graph:
{context}

Question: {question}

Answer:"""

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_prompt})

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=messages,
        temperature=0.1,
    )

    answer = response.choices[0].message.content
    if not answer:
        logger.warning("Groq returned empty response.")
        return "No response generated. Please try again."

    return answer

# main orchestrator.

def main():
    import argparse
    parser = argparse.ArgumentParser(description="RAG Pipeline")
    parser.add_argument("query", type=str, help="The question to ask.")
    args = parser.parse_args()

    answer = ask_question(args.query)
    print("\n" + "=" * 80)
    print(f"QUESTION: {args.query}")
    print("-" * 80)
    print(f"ANSWER:\n{answer}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()