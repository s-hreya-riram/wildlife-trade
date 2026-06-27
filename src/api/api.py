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
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel
from weasyprint import HTML
from dotenv import load_dotenv

from models import init_db, save_report, check_cached_report

load_dotenv()
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
MCP_API_KEY = os.getenv("MCP_API_KEY")
MCP_SPECIES_SERVER_URL = os.getenv("MCP_SPECIES_SERVER_URL", "http://species-mcp-server:8001/mcp")
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
    session: Optional[ClientSession] = None
    tools: list = []
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

from contextlib import AsyncExitStack
from mcp.client.streamable_http import streamablehttp_client  # note: no underscore before http

@asynccontextmanager
async def lifespan(app: FastAPI):
    exit_stack = AsyncExitStack()
    await exit_stack.__aenter__()
    
    try:
        read, write, _ = await exit_stack.enter_async_context(
            streamablehttp_client(MCP_SPECIES_SERVER_URL)  # no headers arg — middleware is commented out anyway
        )
        session = await exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        tools_result = await session.list_tools()
        for tool in tools_result.tools:
            state.sessions[tool.name] = session
            state.tools.append(_to_anthropic_tool(tool))

        state.session = session
        logger.info("Connected to MCP server. Tools: %s", [t["name"] for t in state.tools])
    except Exception as e:
        logger.error("Failed to connect to MCP server: %s", e)

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
        "mcp_tools": list(state.sessions.keys()),
    }