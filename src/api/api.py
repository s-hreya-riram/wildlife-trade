"""
WildlifeWatch API.

Architecture:
  React → POST /api/report
        → FastAPI (this file)
        → MCP Client (StreamableHTTP → species MCP server)
        → Claude agent loop (tool calls via MCP)
        → Reflection pass (Claude improves draft)
        → Eval agent (LLM-as-judge)
        → PDF generation (WeasyPrint)
        → S3 upload → presigned URL
        → Postgres (report metadata + scores)
"""
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import anthropic
from openai import OpenAI
import boto3
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession
from contextlib import AsyncExitStack, asynccontextmanager
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel
from weasyprint import HTML
from dotenv import load_dotenv

from models import init_db, save_report, check_cached_report

load_dotenv()
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MCP_API_KEY = os.getenv("MCP_API_KEY")

# Build MCP URL from base host env var
MCP_SPECIES_BASE = os.getenv("MCP_SPECIES_SERVER_URL", "http://species-mcp-server:8001")
# Render gives bare hostname, add https:// if missing
if not MCP_SPECIES_BASE.startswith("http"):
    MCP_SPECIES_BASE = f"https://{MCP_SPECIES_BASE}"
MCP_SPECIES_SERVER_URL = f"{MCP_SPECIES_BASE}/mcp"

MCP_TRADE_BASE = os.getenv("MCP_TRADE_SERVER_URL", "http://trade-mcp-server:8002")
if not MCP_TRADE_BASE.startswith("http"):
    MCP_TRADE_BASE = f"https://{MCP_TRADE_BASE}"
MCP_TRADE_SERVER_URL = f"{MCP_TRADE_BASE}/mcp"

S3_BUCKET = os.getenv("S3_BUCKET")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")
REPORT_CACHE_TTL_HOURS = 24

if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY not set")
if not MCP_API_KEY:
    raise ValueError("MCP_API_KEY not set")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
def get_s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


# ---------------------------------------------------------------------------
# App state — MCP session and tools
# ---------------------------------------------------------------------------

class AppState:
    session: Optional[ClientSession] = None          # species MCP
    trade_session: Optional[ClientSession] = None    # trade MCP  ← ADD
    tools: list = []                                 # species tools
    trade_tools: list = []                           # trade tools  ← ADD
    sessions: dict[str, ClientSession] = {}


state = AppState()


def _to_anthropic_tool(mcp_tool) -> dict:
    return {
        "name": mcp_tool.name,
        "description": mcp_tool.description or "",
        "input_schema": mcp_tool.inputSchema,
    }


# ---------------------------------------------------------------------------
# Lifespan — connect to MCP server on startup
# ---------------------------------------------------------------------------

import asyncio

@asynccontextmanager
async def lifespan(app: FastAPI):
    exit_stack = AsyncExitStack()
    await exit_stack.__aenter__()

    # Retry MCP connections — services may not be ready immediately on Render
    for attempt in range(10):
        try:
            read, write, _ = await exit_stack.enter_async_context(
                streamable_http_client(MCP_SPECIES_SERVER_URL)
            )
            session = await exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                state.sessions[tool.name] = session
                state.tools.append(_to_anthropic_tool(tool))
            state.session = session
            logger.info("Connected to Species MCP. Tools: %s", [t["name"] for t in state.tools])
            break
        except Exception as e:
            logger.warning("Species MCP connection attempt %d failed: %s", attempt + 1, e)
            if attempt < 9:
                await asyncio.sleep(10)
            else:
                logger.error("Could not connect to Species MCP after 10 attempts — continuing without it")

    for attempt in range(10):
        try:
            read2, write2, _ = await exit_stack.enter_async_context(
                streamable_http_client(MCP_TRADE_SERVER_URL)
            )
            trade_session = await exit_stack.enter_async_context(ClientSession(read2, write2))
            await trade_session.initialize()
            trade_tools_result = await trade_session.list_tools()
            for tool in trade_tools_result.tools:
                state.sessions[tool.name] = trade_session
                state.trade_tools.append(_to_anthropic_tool(tool))
            state.trade_session = trade_session
            logger.info("Connected to Trade MCP. Tools: %s", [t["name"] for t in state.trade_tools])
            break
        except Exception as e:
            logger.warning("Trade MCP connection attempt %d failed: %s", attempt + 1, e)
            if attempt < 9:
                await asyncio.sleep(10)
            else:
                logger.error("Could not connect to Trade MCP after 10 attempts — continuing without it")

    await init_db()
    yield
    await exit_stack.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

AGENT_SYSTEM_PROMPT = """You are WildlifeWatch, a conservation intelligence agent.
Your job is to generate comprehensive, data-driven conservation reports using
real IUCN Red List data fetched via your tools.

When generating a report:
1. Always fetch current species status and threats using lookup_species
2. Always fetch historical assessment trend using get_status_history
3. For the top 2-3 threats, call get_threat_context to understand their broader impact
4. If a country is specified or relevant, call get_species_by_country for geographic context
5. If only a common name is given, use find_species first to get the scientific name

Structure your final report with these sections:
- Executive Summary
- Current Conservation Status
- Population Trend & Historical Assessments
- Key Threats
- Geographic Distribution
- Conservation Recommendations

Be specific. Every claim must be grounded in the tool data returned.
Never fabricate statistics or status codes."""

REFLECTION_PROMPT = """Review the conservation report below and improve it.

Check for:
1. Any claims not grounded in the tool data — remove or caveat them
2. Missing sections or thin coverage — identify gaps
3. Vague recommendations — make them specific and actionable
4. Status codes explained — ensure CR/EN/VU etc. are defined for a non-expert reader

Return the improved report only, no meta-commentary."""

EVAL_SYSTEM_PROMPT = """You are an expert conservation report evaluator.
Score the following report on three dimensions, each out of 10:

1. factual_grounding: Are all claims traceable to IUCN data? Penalise vague or unsupported statements.
2. completeness: Are status, trend, threats, and geography all meaningfully covered?
3. actionability: Are recommendations specific enough to be acted upon?

Respond ONLY with valid JSON in this exact format:
{
  "factual_grounding": <0-10>,
  "completeness": <0-10>,
  "actionability": <0-10>,
  "reasoning": "<one sentence per dimension>"
}"""

async def run_agent(
    species: Optional[str],
    country: Optional[str],
    year_from: Optional[int],
    year_to: Optional[int],
) -> str:
    """Run the agentic tool loop and return a draft report."""
    # Build the user prompt
    parts = ["Generate a conservation report"]
    if species:
        parts.append(f"for the species: {species}")
    if country:
        parts.append(f"focusing on country code: {country}")
    if year_from or year_to:
        range_str = f"from {year_from or 'earliest'} to {year_to or 'latest'}"
        parts.append(f"covering the time range {range_str}")

    user_prompt = " ".join(parts) + "."
    messages = [{"role": "user", "content": user_prompt}]

    while True:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=AGENT_SYSTEM_PROMPT,
            tools=state.tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            return "\n".join(
                block.text for block in response.content if hasattr(block, "text")
            )

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
                        else json.dumps({"error": "empty response"})
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })

            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return "Unable to generate report."


async def run_reflection(draft: str) -> str:
    """Ask Claude to review and improve the draft report."""
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": f"{REFLECTION_PROMPT}\n\n---\n\n{draft}"
        }],
    )
    return "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    )

openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def run_eval(report: str) -> dict:
    """Score the report using GPT-4o as an independent judge."""
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        max_tokens=512,
        messages=[
            {"role": "system", "content": EVAL_SYSTEM_PROMPT},
            {"role": "user", "content": report},
        ],
        response_format={"type": "json_object"},  # forces valid JSON output
    )
    raw = response.choices[0].message.content
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Eval response was not valid JSON: %s", raw)
        return {
            "factual_grounding": 0,
            "completeness": 0,
            "actionability": 0,
            "reasoning": "Eval failed to parse.",
        }

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


# ---------------------------------------------------------------------------
# PDF generation + S3 upload
# ---------------------------------------------------------------------------

REPORT_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: Georgia, serif; max-width: 800px; margin: 40px auto; color: #222; line-height: 1.7; }}
  h1 {{ color: #1a5c2a; border-bottom: 2px solid #1a5c2a; padding-bottom: 8px; }}
  h2 {{ color: #2d7a3e; margin-top: 2em; }}
  .scores {{ background: #f4f9f5; border-left: 4px solid #1a5c2a; padding: 12px 20px; margin: 20px 0; }}
  .score-item {{ display: inline-block; margin-right: 24px; }}
  .score-label {{ font-size: 0.85em; color: #555; }}
  .score-value {{ font-size: 1.4em; font-weight: bold; color: #1a5c2a; }}
  .footer {{ margin-top: 3em; font-size: 0.8em; color: #888; border-top: 1px solid #ddd; padding-top: 12px; }}
  p {{ margin: 0.8em 0; }}
</style>
</head>
<body>
  <h1>WildlifeWatch Conservation Report</h1>
  <p style="color:#888; font-size:0.9em;">Generated: {generated_at}</p>

  <div class="scores">
    <div class="score-item">
      <div class="score-label">Factual Grounding</div>
      <div class="score-value">{factual_grounding}/10</div>
    </div>
    <div class="score-item">
      <div class="score-label">Completeness</div>
      <div class="score-value">{completeness}/10</div>
    </div>
    <div class="score-item">
      <div class="score-label">Actionability</div>
      <div class="score-value">{actionability}/10</div>
    </div>
  </div>

  {report_html}

  <div class="footer">
    Data sourced from IUCN Red List of Threatened Species v4 API.<br>
    IUCN 2025. IUCN Red List of Threatened Species. Version 2025-2 &lt;www.iucnredlist.org&gt;
  </div>
</body>
</html>
"""


def _markdown_to_html(text: str) -> str:
    """Basic markdown to HTML conversion."""
    import re
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    paragraphs = text.split('\n\n')
    return '\n'.join(f'<p>{p.strip()}</p>' if not p.strip().startswith('<h') else p.strip()
                     for p in paragraphs if p.strip())


def generate_pdf(report_text: str, scores: dict) -> bytes:
    html = REPORT_HTML_TEMPLATE.format(
        generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        factual_grounding=scores.get("factual_grounding", 0),
        completeness=scores.get("completeness", 0),
        actionability=scores.get("actionability", 0),
        report_html=_markdown_to_html(report_text),
    )
    return HTML(string=html).write_pdf()


def upload_to_s3(pdf_bytes: bytes, report_id: str) -> str:
    """Upload PDF to S3 and return a presigned URL valid for 24 hours."""
    key = f"reports/{report_id}.pdf"
    s3_client = get_s3_client()
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=pdf_bytes,
        ContentType="application/pdf",
    )
    url = s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=60 * 60 * 24,  # 24 hours
    )
    return url


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="WildlifeWatch API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReportRequest(BaseModel):
    species: Optional[str] = None
    country: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None


class ReportResponse(BaseModel):
    report_id: str
    report_url: str
    scores: dict
    generated_at: str


@app.post("/api/report", response_model=ReportResponse)
async def generate_report(request: ReportRequest):
    # check cache first
    cached = await check_cached_report(
        species=request.species,
        country=request.country,
        year_from=request.year_from,
        year_to=request.year_to,
    )
    if cached:
        logger.info("Returning cached report %s", cached["report_id"])
        return ReportResponse(
            report_id=cached["report_id"],
            report_url=cached["report_url"],
            scores=cached["scores"],
            generated_at=cached["generated_at"],
        )
    if not request.species and not request.country:
        raise HTTPException(
            status_code=400,
            detail="At least one of 'species' or 'country' must be provided"
        )

    report_id = str(uuid.uuid4())

    try:
        # 1. Agent loop — gather data and draft report
        draft = await run_agent(
            species=request.species,
            country=request.country,
            year_from=request.year_from,
            year_to=request.year_to,
        )

        # 2. Reflection — improve the draft
        refined = await run_reflection(draft)

        # 3. Eval — score the refined report
        scores = await run_eval(refined)

        # 4. Generate PDF
        pdf_bytes = generate_pdf(refined, scores)

        # 5. Upload to S3
        report_url = upload_to_s3(pdf_bytes, report_id)

        # 6. Persist metadata to Postgres
        await save_report(
            report_id=report_id,
            species=request.species,
            country=request.country,
            year_from=request.year_from,
            year_to=request.year_to,
            scores=scores,
            report_url=report_url,
        )

        return ReportResponse(
            report_id=report_id,
            report_url=report_url,
            scores=scores,
            generated_at=datetime.utcnow().isoformat(),
        )

    except Exception as e:
        logger.error("Report generation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/reports")
async def list_reports(species: Optional[str] = None, country: Optional[str] = None):
    """List previously generated reports, optionally filtered."""
    from models import get_reports
    reports = await get_reports(species=species, country=country)
    return {"reports": reports}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "species_mcp": state.session is not None,
        "trade_mcp": state.trade_session is not None,
        "species_tools": [t["name"] for t in state.tools],
        "trade_tools": [t["name"] for t in state.trade_tools],
    }

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
    "Proxy to the species MCP get_species_by_country tool."
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
