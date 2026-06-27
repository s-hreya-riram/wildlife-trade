"""
Trade endpoints for WildlifeWatch FastAPI.

ADD these to api.py:

1. At the top, extend AppState and lifespan to also connect to the trade MCP server.
2. Add the two new route handlers below.

--- STEP 1: Extend AppState ---

class AppState:
    session: Optional[ClientSession] = None          # species MCP
    trade_session: Optional[ClientSession] = None    # trade MCP  ← ADD
    tools: list = []                                 # species tools
    trade_tools: list = []                           # trade tools  ← ADD
    sessions: dict[str, ClientSession] = {}

--- STEP 2: Extend lifespan to connect trade MCP ---

Add this block INSIDE the lifespan() function, after the species MCP connection block:

    MCP_TRADE_BASE = os.getenv("MCP_TRADE_SERVER_URL", "http://trade-mcp-server:8002")
    if not MCP_TRADE_BASE.startswith("http"):
        MCP_TRADE_BASE = f"https://{MCP_TRADE_BASE}"
    MCP_TRADE_SERVER_URL = f"{MCP_TRADE_BASE}/mcp"

    try:
        read2, write2, _ = await exit_stack.enter_async_context(
            streamablehttp_client(MCP_TRADE_SERVER_URL)
        )
        trade_session = await exit_stack.enter_async_context(ClientSession(read2, write2))
        await trade_session.initialize()

        trade_tools_result = await trade_session.list_tools()
        for tool in trade_tools_result.tools:
            state.sessions[tool.name] = trade_session
            state.trade_tools.append(_to_anthropic_tool(tool))

        state.trade_session = trade_session
        logger.info("Connected to Trade MCP. Tools: %s", [t["name"] for t in state.trade_tools])
    except Exception as e:
        logger.error("Failed to connect to Trade MCP server: %s", e)

--- STEP 3: Add these imports to api.py ---
from typing import Optional  # already there

--- STEP 4: Add these two route handlers ---
"""

# ── Paste these two handlers into api.py ──────────────────────────────────────

TRADE_AGENT_SYSTEM_PROMPT = """You are WildlifeWatch Trade Intelligence, an expert wildlife crime analyst.

Analyse marketplace listings to detect potential illegal wildlife trade.
You have five tools available — use them ALL in sequence for every listing:

1. classify_listing — identify species/material from text and image
2. lookup_cites — check CITES appendix status for the identified species
3. detect_coded_language — scan for trafficking euphemisms
4. score_severity — combine all signals into HIGH/MEDIUM/LOW
5. generate_tip_report — produce the final enforcement tip report

ALWAYS call all five tools in order. Never skip a tool. Never fabricate species names or conservation data.
If species identification confidence is below 0.3, still complete the full pipeline with the best guess and note uncertainty."""


async def run_trade_agent(
    title: str,
    description: str,
    platform: str,
    image_url: str | None,
) -> dict:
    """Run the trade analysis agent loop. Returns structured analysis result."""
    combined_text = f"Title: {title}\nPlatform: {platform}\nDescription: {description}"

    messages = [{
        "role": "user",
        "content": (
            f"Analyse this marketplace listing for potential wildlife trade violations:\n\n"
            f"{combined_text}"
            + (f"\n\nImage: {image_url}" if image_url else "")
        ),
    }]

    all_tools = state.trade_tools  # only trade tools for this agent
    collected: dict = {}

    while True:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=TRADE_AGENT_SYSTEM_PROMPT,
            tools=all_tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            # Extract final text
            final_text = "\n".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            collected["agent_summary"] = final_text
            return collected

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    session = state.sessions.get(block.name)
                    if not session:
                        logger.error("No session for tool: %s", block.name)
                        continue

                    mcp_result = await session.call_tool(block.name, block.input)
                    result_text = (
                        mcp_result.content[0].text
                        if mcp_result.content
                        else json.dumps({"error": "empty"})
                    )

                    # Collect key results for structured response
                    try:
                        parsed = json.loads(result_text)
                        if block.name == "classify_listing" and parsed.get("success"):
                            c = parsed["classification"]
                            collected.update({
                                "species_common": c.get("species_common"),
                                "species_latin": c.get("species_latin"),
                                "material_type": c.get("material_type"),
                                "confidence": c.get("confidence", 0),
                                "classification_reasoning": c.get("reasoning"),
                            })
                        elif block.name == "lookup_cites" and parsed.get("success"):
                            c = parsed["cites"]
                            collected.update({
                                "cites_appendix": c.get("cites_appendix"),
                                "cites_trade_illegal": c.get("trade_illegal"),
                                "cites_annotation": c.get("annotation"),
                            })
                        elif block.name == "detect_coded_language":
                            collected.update({
                                "matched_patterns": parsed.get("matched_patterns", []),
                                "language_flag_score": parsed.get("language_flag_score", 0),
                            })
                        elif block.name == "score_severity":
                            collected.update({
                                "severity": parsed.get("severity", "LOW"),
                                "severity_color": parsed.get("severity_color", "#38a169"),
                                "severity_reason": parsed.get("severity_reason", ""),
                                "composite_score": parsed.get("composite_score", 0),
                                "signal_breakdown": parsed.get("signal_breakdown", {}),
                            })
                        elif block.name == "generate_tip_report" and parsed.get("success"):
                            collected["report_markdown"] = parsed.get("report_markdown", "")
                    except Exception:
                        pass

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return collected


# ── Route handlers — paste these into api.py ─────────────────────────────────

"""
class TradeAnalysisRequest(BaseModel):
    title: str = ""
    description: str = ""
    platform: str = "Unknown"
    image_url: Optional[str] = None


@app.post("/api/trade/analyse")
async def analyse_listing(request: TradeAnalysisRequest):
    if not request.title and not request.description:
        raise HTTPException(status_code=400, detail="title or description required")

    if not state.trade_session:
        raise HTTPException(
            status_code=503,
            detail="Trade MCP server not connected. Check MCP_TRADE_SERVER_URL env var."
        )

    import uuid as _uuid
    report_id = str(_uuid.uuid4())[:8].upper()

    try:
        result = await run_trade_agent(
            title=request.title,
            description=request.description,
            platform=request.platform,
            image_url=request.image_url,
        )
        result["report_id"] = report_id
        result["platform"] = request.platform
        return result
    except Exception as e:
        logger.error("Trade analysis failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/species/country/{country_code}")
async def species_by_country(country_code: str):
    \"\"\"Proxy to the species MCP get_species_by_country tool.\"\"\"
    if not state.session:
        raise HTTPException(status_code=503, detail="Species MCP not connected")
    try:
        mcp_result = await state.session.call_tool(
            "get_species_by_country", {"country_code": country_code.upper()}
        )
        raw = mcp_result.content[0].text if mcp_result.content else "{}"
        data = json.loads(raw)
        return {
            "country_code": country_code.upper(),
            "species": data.get("species", []),
            "count": data.get("threatened_species_count", 0),
        }
    except Exception as e:
        logger.error("Species by country failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
"""