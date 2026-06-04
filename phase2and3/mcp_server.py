"""
Exposes the four market knowledge APIs as MCP tools so any MCP-compatible
LLM client (Claude Desktop, Cursor, etc.) can call them directly.

Tools:
  - daily_market_summary
  - market_themes
  - market_classification
  - conversational_qa

Run:
  python mcp_server.py

The MCP client connects via stdio transport (default).
No JWT auth needed — MCP handles its own transport security.

IMPORTANT: Logs go to stderr, not stdout.
MCP uses stdout to communicate with the client — logging to stdout
would corrupt the protocol. All terminal logs appear in stderr which
is visible in the terminal but ignored by the MCP client.

Install:
  pip install mcp loguru
"""
import sys
import uuid
from datetime import date

from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from rag_pipeline import ask_question

# ── Logging — stderr only, never stdout ───────────────────────────────────────
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="DEBUG",
    colorize=True,
)

# ── Session store for conversational QA ───────────────────────────────────────
SESSION_HISTORY: dict = {}

# ── MCP Server ────────────────────────────────────────────────────────────────
server = Server("market-knowledge")
logger.info("MCP Server initialised — waiting for client connection.")


@server.list_tools()
async def list_tools() -> list[Tool]:
    logger.debug("Client requested tool list.")
    return [
        Tool(
            name="daily_market_summary",
            description=(
                "Summarise key financial market events for a given date. "
                "If no date is provided, summarises today's events. "
                "Returns a plain English summary of what happened in markets, "
                "which sectors and regions were affected, and the overall direction."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": (
                            "Date to summarise in YYYY-MM-DD format. "
                            "Optional — defaults to today if not provided."
                        ),
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="market_themes",
            description=(
                "Identify macro market events and themes driving financial markets. "
                "Use this for broad thematic questions like 'what is causing volatility', "
                "'which geopolitical events are affecting Nordic markets', "
                "or 'what measures are being taken against inflation'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language question about market themes or macro events.",
                    }
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="market_classification",
            description=(
                "Classify and retrieve market events by asset class, sector, region, "
                "company, or any combination. Use this for specific questions like "
                "'show me events affecting Nordic Financials', "
                "'which companies were most negatively impacted by rate decisions', "
                "or 'what happened in Fixed Income in Europe last week'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language question about specific market events or classifications.",
                    }
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="conversational_qa",
            description=(
                "Conversational market Q&A with session memory. "
                "Maintains context across multiple turns so follow-up questions work naturally. "
                "On the first call, omit history_id — a new session ID is returned. "
                "Pass that history_id on subsequent calls to maintain conversation context."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The market question to ask.",
                    },
                    "history_id": {
                        "type": "string",
                        "description": (
                            "Session ID from a previous response to maintain conversation context. "
                            "Omit on the first message in a new conversation."
                        ),
                    },
                },
                "required": ["question"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    logger.info(f"Tool called: {name} | Arguments: {arguments}")

    if name == "daily_market_summary":
        raw_date = arguments.get("date")
        if raw_date:
            try:
                from dateutil import parser as dateutil_parser
                parsed_date = dateutil_parser.parse(raw_date).date()
                date_str = parsed_date.strftime("%Y-%m-%d")
            except Exception:
                logger.warning(f"Failed to parse date: {raw_date}")
                return [TextContent(
                    type="text",
                    text="Invalid date format. Please use YYYY-MM-DD."
                )]
        else:
            date_str = date.today().strftime("%Y-%m-%d")

        logger.info(f"Daily summary requested for date: {date_str}")
        query = (
            f"Summarise key market events from {date_str} in simple language. "
            "Include the most significant events, which sectors and regions were affected, "
            "and the overall market direction."
        )
        answer = ask_question(query, event_date=date_str)
        logger.info(f"Daily summary generated for {date_str}.")
        return [TextContent(type="text", text=f"Market Summary ({date_str}):\n\n{answer}")]

    elif name == "market_themes":
        query = arguments.get("query", "").strip()
        if not query:
            return [TextContent(type="text", text="Please provide a query.")]
        logger.info(f"Market themes query: {query}")
        answer = ask_question(query)
        logger.info("Market themes response generated.")
        return [TextContent(type="text", text=answer)]

    elif name == "market_classification":
        query = arguments.get("query", "").strip()
        if not query:
            return [TextContent(type="text", text="Please provide a query.")]
        logger.info(f"Classification query: {query}")
        answer = ask_question(query)
        logger.info("Classification response generated.")
        return [TextContent(type="text", text=answer)]

    elif name == "conversational_qa":
        question = arguments.get("question", "").strip()
        if not question:
            return [TextContent(type="text", text="Please provide a question.")]

        history_id = arguments.get("history_id") or str(uuid.uuid4())
        history = SESSION_HISTORY.get(history_id, [])[-10:]
        logger.info(f"CQA | history_id={history_id} | turns={len(history)} | question={question}")

        answer = ask_question(question, conversation_history=history)

        if history_id not in SESSION_HISTORY:
            SESSION_HISTORY[history_id] = []
        SESSION_HISTORY[history_id].append({"role": "user",      "content": question})
        SESSION_HISTORY[history_id].append({"role": "assistant", "content": answer})

        logger.info(f"CQA response generated | history_id={history_id}")
        return [TextContent(
            type="text",
            text=f"{answer}\n\n[history_id: {history_id}]"
        )]

    else:
        logger.error(f"Unknown tool called: {name}")
        return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    logger.info("Starting MCP stdio transport...")
    async with stdio_server() as (read_stream, write_stream):
        logger.info("MCP client connected.")
        await server.run(read_stream, write_stream, server.create_initialization_options())
    logger.info("MCP server shut down.")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())