# 🦅 WildlifeWatch Trade Intelligence Platform

An AI agent system that detects illegal wildlife trade listings on online marketplaces, cross-references conservation databases, and generates structured tip reports for enforcement agencies.

---

## The Problem

Wildlife trafficking is the world's 4th largest criminal enterprise, worth an estimated **$23 billion annually** — and most of it has moved online. Traffickers list pangolin scales on Carousell, ivory on Facebook Marketplace, and live protected animals on Shopee, using coded language specifically designed to evade keyword filters. Conservation organisations and enforcement agencies lack the tooling to monitor this at scale.

## The Solution

WildlifeWatch is a trade intelligence platform that combines vision AI, conservation databases, and linguistic pattern analysis into a single agentic pipeline. It identifies protected species in listings, cross-references CITES and IUCN status, detects trafficking coded language, scores severity, and produces structured tip reports ready to submit to TRAFFIC or NParks/AVS.

---

## Architecture

```
Streamlit UI
    │
    ▼
FastAPI  ──────────────────────────────────────────────┐
    │                                                   │
    ▼                                                   ▼
Trade MCP Server (port 8002)              Species MCP Server (port 8001)
    │                                                   │
    ├── classify_listing (GPT-4o vision)                ├── lookup_species
    ├── lookup_cites (Species+ API)                     ├── get_status_history
    ├── detect_coded_language (pattern dict)            ├── find_species
    ├── score_severity (weighted composite)             ├── get_species_by_country
    └── generate_tip_report (Claude)                    └── get_threat_context
                                                                │
                                                         IUCN Red List API v4
                                                         Redis (caching)
                                                         PostgreSQL (report history)
```

**Agent orchestration:** Claude (`claude-sonnet-4-6`) drives both agent loops via the Anthropic API. The trade agent calls all five MCP tools in sequence for every listing analysis. The species agent calls IUCN tools to generate conservation reports.

**Key design decisions:**
- MCP (Model Context Protocol) over StreamableHTTP for tool serving — each server is independently deployable
- Lazy MCP connection in FastAPI — connects on first request rather than startup, avoids cold-start race conditions on Render
- GPT-4o vision for listing classification (multimodal, handles image + text together)
- Claude for tip report generation (better long-form structured prose)
- Redis caching on all IUCN API calls (24h TTL) to stay within rate limits

---

## Features

### 🌏 Threat Landscape
- Live IUCN Red List data for Southeast Asian countries
- Critically Endangered, Endangered, and Vulnerable species counts
- Synthetic intelligence feed of 18 pre-analysed listings across HIGH/MEDIUM/LOW severity

### 🔍 Analyse a Listing
- Paste any marketplace listing title + description (or upload an image)
- Real-time agent trace showing all 5 tool calls firing in sequence
- Species identification via GPT-4o vision
- CITES appendix lookup via Species+ API
- 30-pattern coded language dictionary (wet goods, dry goods, special medicine, etc.)
- Weighted severity score: 40% CITES + 25% IUCN + 25% classifier confidence + 10% language

### 📋 Tip Report
- Structured markdown report with species ID, conservation status, listing analysis, and recommended action
- Downloadable as PDF
- Direct links to TRAFFIC and NParks/AVS reporting channels

---

## Project Structure

```
wildlife-trade/
├── src/
│   ├── api/
│   │   ├── api.py              # FastAPI app, agent loops, trade + species endpoints
│   │   ├── models.py           # PostgreSQL schema, asyncpg queries
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   └── mcp-servers/
│       ├── species/
│       │   ├── server.py       # FastMCP server — IUCN Red List tools
│       │   ├── iucn_client.py  # IUCN API v4 client with Redis caching
│       │   ├── requirements.txt
│       │   └── Dockerfile
│       └── trade/
│           ├── server.py       # FastMCP server — trade intelligence tools
│           ├── requirements.txt
│           └── Dockerfile
├── streamlit_app/
│   ├── app.py                  # Streamlit UI
│   ├── demo_listings.json      # 18 synthetic listings for the intelligence feed
│   ├── requirements.txt
│   └── Dockerfile
├── render.yml                  # One-click Render blueprint (5 services)
└── docker-compose.yml          # Local development
```

---

## Local Development

### Prerequisites
- Python 3.11+
- Docker + Docker Compose
- API keys: Anthropic, OpenAI, IUCN Red List v4, Species+ (CITES)

### Setup

```bash
git clone https://github.com/your-username/wildlife-trade
cd wildlife-trade
cp .env.example .env
# Fill in your API keys in .env
```

### Run with Docker Compose

```bash
docker-compose up --build
```

Services start on:
- Streamlit: http://localhost:8501
- FastAPI: http://localhost:8000
- Species MCP: http://localhost:8001
- Trade MCP: http://localhost:8002

### Run services individually (development)

```bash
# Terminal 1 — Species MCP server
cd src/mcp-servers/species
pip install -r requirements.txt
IUCN_TOKEN=... MCP_API_KEY=test python server.py

# Terminal 2 — Trade MCP server
cd src/mcp-servers/trade
pip install -r requirements.txt
OPENAI_API_KEY=... ANTHROPIC_API_KEY=... SPECIES_PLUS_API_KEY=... MCP_API_KEY=test python server.py

# Terminal 3 — FastAPI
cd src/api
pip install -r requirements.txt
MCP_SPECIES_SERVER_URL=http://localhost:8001 MCP_TRADE_SERVER_URL=http://localhost:8002 \
ANTHROPIC_API_KEY=... OPENAI_API_KEY=... MCP_API_KEY=test uvicorn api:app --reload --port 8000

# Terminal 4 — Streamlit
cd streamlit_app
pip install -r requirements.txt
API_URL=http://localhost:8000 streamlit run app.py
```

### Test MCP tools directly

```bash
cd src/mcp-servers/trade
python test_mcp.py
# Expected: Tools: ['classify_listing', 'lookup_cites', 'detect_coded_language', 'score_severity', 'generate_tip_report']
```

---

## Deployment (Render)

The `render.yml` blueprint provisions all five services in one click:
- `wildlifewatch-postgres` — managed Postgres (report history)
- `wildlifewatch-redis` — managed Redis (IUCN API cache)
- `wildlifewatch-species-mcp` — Species MCP server
- `wildlifewatch-trade-mcp` — Trade MCP server
- `wildlifewatch-api` — FastAPI orchestrator
- `wildlifewatch-streamlit` — Streamlit frontend (https://wildlife-trade.streamlit.app/)

### Manual env vars to set in Render dashboard

| Service | Key |
|---|---|
| species-mcp | `IUCN_TOKEN` |
| species-mcp, trade-mcp, api | `MCP_API_KEY` (same value) |
| trade-mcp, api | `OPENAI_API_KEY` |
| trade-mcp, api | `ANTHROPIC_API_KEY` |
| trade-mcp | `SPECIES_PLUS_API_KEY` |

Deploy via: Render Dashboard → New → Blueprint → connect your repo.

---

## Agent Concepts Demonstrated

| Concept | Where |
|---|---|
| **MCP Server** | Two FastMCP servers (species + trade), each with 5–6 tools over StreamableHTTP |
| **Agent / tool use loop** | `run_trade_agent()` and `run_agent()` in `api.py` — Claude drives multi-turn tool call loops |
| **Multi-model pipeline** | GPT-4o for vision classification, Claude for report generation, GPT-4o as eval judge |
| **Deployability** | Full Render blueprint, Docker Compose for local, health endpoints on all services |
| **Security** | MCP_API_KEY middleware on MCP servers, no secrets in code, `.env.example` pattern |

---

## API Reference

### `POST /api/trade/analyse`
Runs the full 5-tool trade intelligence pipeline on a listing.

```json
{
  "title": "Pangolin scales for sale",
  "description": "Freshly imported dry goods, DM for price, discreet shipping",
  "platform": "Carousell",
  "image_url": "https://..."
}
```

### `GET /api/species/country/{country_code}`
Returns threatened species (CR/EN/VU) for an ISO alpha-2 country code via IUCN.

### `POST /api/report`
Generates a full conservation report for a species or country (PDF, scored).

### `GET /health`
Returns MCP connection status and available tools.

---

## Data Sources

- **IUCN Red List API v4** — species conservation status, population trends, threats
- **Species+ API (CITES)** — CITES appendix listings and trade restrictions
- **Synthetic demo dataset** — 18 curated listings with realistic trafficking language, sourced for demonstration purposes

---

## ⚠️ Important Notes

- This tool is built for conservation intelligence and law enforcement support only
- Demo listings are synthetic and do not represent real marketplace content
- No API keys or secrets are stored in this repository — see `.env.example`