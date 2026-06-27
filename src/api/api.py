"""
WildlifeWatch API.

Architecture:
  Streamlit → POST /api/trade/analyse
            → FastAPI (this file)
            → MCP Client (StreamableHTTP → trade MCP server)
            → Claude agent loop (tool calls via MCP)
            → Tip report generated

  Streamlit → GET /api/species/country/{code}
            → FastAPI
            → MCP Client (StreamableHTTP → species MCP server)

  Streamlit → POST /api/report
            → FastAPI
            → MCP Client (StreamableHTTP → species MCP server)
            → Claude agent loop
            → Reflection pass
            → Eval agent (GPT-4o)
            → PDF generation (WeasyPrint)
            → S3 upload → presigned URL
            → Postgres (report metadata + scores)
"""
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import anthropic
from openai import OpenAI
import boto3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel
from weasyprint import HTML
from dotenv import load_dotenv

from models import init_db, save_report, check_cached_report

load_dotenv()
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MCP_API_KEY = os.getenv("MCP_API_KEY")

MCP_SPECIES_BASE = os.getenv("MCP_SPECIES_SERVER_URL", "http://species-mcp-server:8001")
if not MCP_SPECIES_BASE.startswith("http"):
    MCP_SPECIES_BASE = f"https://{MCP_SPECIES_BASE}"
MCP_SPECIES_SERVER_URL = f"{MCP_SPECIES_BASE}/mcp"

MCP_TRADE_BASE = os.getenv("MCP_TRADE_SERVER_URL", "http://trade-mcp-server:8002")
if not MCP_TRADE_BASE.startswith("http"):
    MCP_TRADE_BASE = f"https://{MCP_TRADE_BASE}"
MCP_TRADE_SERVER_URL = f"{MCP_TRADE_BASE}/mcp"

S3_BUCKET = os.getenv("S3_BUCKET")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")

if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY not set")
if not MCP_API_KEY:
    raise ValueError("MCP_API_KEY not set")

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def get_s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

class AppState:
    session: Optional[ClientSession] = None
    trade_session: Optional[ClientSession] = None
    tools: list = []
    trade_tools: list = []
    sessions: dict[str, ClientSession] = {}


state = AppState()


def _to_anthropic_tool(mcp_tool) -> dict:
    return {
        "name": mcp_tool.name,
        "description": mcp_tool.description or "",
        "input_schema": mcp_tool.inputSchema,
    }


# ---------------------------------------------------------------------------
# Lifespan — just init DB; MCP connects lazily on first request
# ---------------------------------------------------------------------------

import asyncio
from contextlib import asynccontextmanager

_background_tasks = set()

async def _species_connection_loop():
    """Owns the species MCP connection for the app lifetime."""
    for attempt in range(10):
        try:
            print(f"[MCP] Species attempt {attempt+1}: {MCP_SPECIES_SERVER_URL}", flush=True)
            async with streamable_http_client(MCP_SPECIES_SERVER_URL) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    state.session = session
                    state.tools.clear()
                    for tool in tools_result.tools:
                        state.sessions[tool.name] = session
                        state.tools.append(_to_anthropic_tool(tool))
                    print(f"[MCP] Species connected. Tools: {[t['name'] for t in state.tools]}", flush=True)
                    # Hold the connection open indefinitely
                    await asyncio.Future()
        except asyncio.CancelledError:
            print("[MCP] Species connection loop cancelled", flush=True)
            state.session = None
            return
        except Exception as e:
            print(f"[MCP] Species attempt {attempt+1} failed: {e}", flush=True)
            state.session = None
            await asyncio.sleep(5)
    print("[MCP] Species MCP failed after 10 attempts", flush=True)


async def _trade_connection_loop():
    """Owns the trade MCP connection for the app lifetime."""
    for attempt in range(10):
        try:
            print(f"[MCP] Trade attempt {attempt+1}: {MCP_TRADE_SERVER_URL}", flush=True)
            async with streamable_http_client(MCP_TRADE_SERVER_URL) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools_result = await session.list_tools()
                    state.trade_session = session
                    state.trade_tools.clear()
                    for tool in tools_result.tools:
                        state.sessions[tool.name] = session
                        state.trade_tools.append(_to_anthropic_tool(tool))
                    print(f"[MCP] Trade connected. Tools: {[t['name'] for t in state.trade_tools]}", flush=True)
                    await asyncio.Future()
        except asyncio.CancelledError:
            print("[MCP] Trade connection loop cancelled", flush=True)
            state.trade_session = None
            return
        except Exception as e:
            print(f"[MCP] Trade attempt {attempt+1} failed: {e}", flush=True)
            state.trade_session = None
            await asyncio.sleep(5)
    print("[MCP] Trade MCP failed after 10 attempts", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[STARTUP] lifespan started", flush=True)
    await init_db()

    t1 = asyncio.create_task(_species_connection_loop())
    t2 = asyncio.create_task(_trade_connection_loop())
    _background_tasks.update({t1, t2})

    yield

    # Shutdown: cancel the loops, which triggers CancelledError inside the
    # async with blocks, cleanly closing the sessions and transports
    print("[SHUTDOWN] cancelling MCP loops", flush=True)
    t1.cancel()
    t2.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)


# ---------------------------------------------------------------------------
# Lazy MCP connection helpers
# ---------------------------------------------------------------------------

async def get_species_session() -> Optional[ClientSession]:
    """Return existing species MCP session, or connect on first call."""
    if state.session is not None:
        return state.session
    try:
        async with streamable_http_client(MCP_SPECIES_SERVER_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    state.sessions[tool.name] = session
                    if not any(t["name"] == tool.name for t in state.tools):
                        state.tools.append(_to_anthropic_tool(tool))
                state.session = session
                logger.info("Connected to Species MCP. Tools: %s", [t["name"] for t in state.tools])
                return session
    except Exception as e:
        logger.error("Species MCP connection failed: %s", e)
        return None


async def get_trade_session() -> Optional[ClientSession]:
    """Return existing trade MCP session, or connect on first call."""
    if state.trade_session is not None:
        return state.trade_session
    try:
        async with streamable_http_client(MCP_TRADE_SERVER_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                for tool in tools_result.tools:
                    state.sessions[tool.name] = session
                    if not any(t["name"] == tool.name for t in state.trade_tools):
                        state.trade_tools.append(_to_anthropic_tool(tool))
                state.trade_session = session
                logger.info("Connected to Trade MCP. Tools: %s", [t["name"] for t in state.trade_tools])
                return session
    except Exception as e:
        logger.error("Trade MCP connection failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Conservation report agent (species MCP)
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
    parts = ["Generate a conservation report"]
    if species:
        parts.append(f"for the species: {species}")
    if country:
        parts.append(f"focusing on country code: {country}")
    if year_from or year_to:
        range_str = f"from {year_from or 'earliest'} to {year_to or 'latest'}"
        parts.append(f"covering the time range {range_str}")

    messages = [{"role": "user", "content": " ".join(parts) + "."}]

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
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": f"{REFLECTION_PROMPT}\n\n---\n\n{draft}"}],
    )
    return "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    )


async def run_eval(report: str) -> dict:
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        max_tokens=512,
        messages=[
            {"role": "system", "content": EVAL_SYSTEM_PROMPT},
            {"role": "user", "content": report},
        ],
        response_format={"type": "json_object"},
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


# ---------------------------------------------------------------------------
# Trade intelligence agent (trade MCP)
# ---------------------------------------------------------------------------

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
    image_url: Optional[str],
) -> dict:
    combined_text = f"Title: {title}\nPlatform: {platform}\nDescription: {description}"
    messages = [{
        "role": "user",
        "content": (
            f"Analyse this marketplace listing for potential wildlife trade violations:\n\n"
            f"{combined_text}"
            + (f"\n\nImage: {image_url}" if image_url else "")
        ),
    }]

    collected: dict = {}

    while True:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=TRADE_AGENT_SYSTEM_PROMPT,
            tools=state.trade_tools,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            collected["agent_summary"] = "\n".join(
                block.text for block in response.content if hasattr(block, "text")
            )
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
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    paragraphs = text.split('\n\n')
    return '\n'.join(
        f'<p>{p.strip()}</p>' if not p.strip().startswith('<h') else p.strip()
        for p in paragraphs if p.strip()
    )


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
    key = f"reports/{report_id}.pdf"
    s3 = get_s3_client()
    s3.put_object(Bucket=S3_BUCKET, Key=key, Body=pdf_bytes, ContentType="application/pdf")
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=60 * 60 * 24,
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="WildlifeWatch API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Models ────────────────────────────────────────────────────────────────────

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


class TradeAnalysisRequest(BaseModel):
    title: str = ""
    description: str = ""
    platform: str = "Unknown"
    image_url: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "species_mcp": state.session is not None,
        "trade_mcp": state.trade_session is not None,
        "species_tools": [t["name"] for t in state.tools],
        "trade_tools": [t["name"] for t in state.trade_tools],
    }


@app.post("/api/trade/analyse")
async def analyse_listing(request: TradeAnalysisRequest):
    if not state.trade_session:
        raise HTTPException(status_code=503, detail="Trade MCP server not connected")

    await get_trade_session()
    if not state.trade_session:
        raise HTTPException(status_code=503, detail="Trade MCP server not connected")

    report_id = str(uuid.uuid4())[:8].upper()
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


@app.post("/api/report", response_model=ReportResponse)
async def generate_report(request: ReportRequest):
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
            detail="At least one of 'species' or 'country' must be provided",
        )

    await get_species_session()
    if not state.session:
        raise HTTPException(status_code=503, detail="Species MCP not connected")

    report_id = str(uuid.uuid4())
    try:
        draft = await run_agent(
            species=request.species,
            country=request.country,
            year_from=request.year_from,
            year_to=request.year_to,
        )
        refined = await run_reflection(draft)
        scores = await run_eval(refined)
        pdf_bytes = generate_pdf(refined, scores)
        report_url = upload_to_s3(pdf_bytes, report_id)
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
    from models import get_reports
    reports = await get_reports(species=species, country=country)
    return {"reports": reports}
