"""
WildlifeWatch Species MCP Server.
Exposes IUCN Red List data as LLM-callable tools via the MCP protocol.
Runs as a standalone service over StreamableHTTP.
"""
import json
import logging
import os

import redis.asyncio as aioredis
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from iucn_client import (
    get_species,
    get_historical_assessments,
    search_species,
    get_threat_details,
    get_species_by_country as _get_species_by_country,
    get_countries as _get_countries,
)

logger = logging.getLogger(__name__)

IUCN_TOKEN = os.getenv("IUCN_TOKEN")
MCP_API_KEY = os.getenv("MCP_API_KEY")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

if not IUCN_TOKEN:
    raise ValueError("IUCN_TOKEN not set")
if not MCP_API_KEY:
    raise ValueError("MCP_API_KEY not set")

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

mcp = FastMCP(
    "wildlifewatch-species",
    host="0.0.0.0",
    port=8001,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
    ),
)
redis_client: aioredis.Redis = aioredis.from_url(REDIS_URL, decode_responses=True)


# ---------------------------------------------------------------------------
# API key middleware
# ---------------------------------------------------------------------------

class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.headers.get("X-API-Key") != MCP_API_KEY:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def lookup_species(genus_name: str, species_name: str) -> str:
    """
    Look up current IUCN conservation status for a species.
    Returns status category (CR/EN/VU/NT/LC/EX), population trend, threats,
    and taxonomic classification.

    Args:
        genus_name: genus part of scientific name e.g. "Panthera", "Manis"
        species_name: species part of scientific name e.g. "tigris", "javanica"
    """
    result = await get_species(genus_name, species_name, IUCN_TOKEN, redis_client)
    if not result:
        return json.dumps({
            "found": False,
            "message": f"No IUCN data found for '{genus_name} {species_name}'"
        })
    return json.dumps({"found": True, "species": result})


@mcp.tool()
async def get_status_history(genus_name: str, species_name: str) -> str:
    """
    Get historical IUCN assessment history for a species.
    Useful for identifying conservation status trends over time.
    Returns assessments sorted by year ascending.

    Args:
        genus_name: genus part of scientific name e.g. "Panthera"
        species_name: species part of scientific name e.g. "tigris"
    """
    results = await get_historical_assessments(genus_name, species_name, IUCN_TOKEN, redis_client)
    if not results:
        return json.dumps({
            "found": False,
            "message": f"No historical assessments found for '{genus_name} {species_name}'"
        })
    return json.dumps({"found": True, "history": results})

@mcp.tool()
async def find_species(query: str) -> str:
    """
    Search for species by common or scientific name.
    Returns up to 10 matching species with their current status.
    Use this when you have a common name and need the scientific name.

    Args:
        query: common or scientific name e.g. "pangolin", "snow leopard"
    """
    results = await search_species(query, IUCN_TOKEN, redis_client)
    if not results:
        return json.dumps({
            "found": False,
            "message": f"No species found matching '{query}'"
        })
    return json.dumps({"found": True, "count": len(results), "species": results})

@mcp.tool()
async def get_species_by_country(country_code: str) -> str:
    """
    Get threatened species (CR/EN/VU) with latest assessments for a country.
    Useful for understanding which species are at risk in a specific country.

    Args:
        country_code: ISO alpha-2 country code e.g. "IN" for India, "ID" for Indonesia
    """
    results = await _get_species_by_country(country_code, IUCN_TOKEN, redis_client)
    if not results:
        return json.dumps({
            "found": False,
            "message": f"No threatened species data found for country code '{country_code}'"
        })
    return json.dumps({
        "found": True,
        "country_code": country_code,
        "threatened_species_count": len(results),
        "species": results,
    })


@mcp.tool()
async def get_threat_context(threat_code: str) -> str:
    """
    Get context for a specific IUCN threat code — how many species it affects
    and a sample of affected species. Use this to enrich threat analysis from
    a species assessment.

    Args:
        threat_code: IUCN threat code e.g. "5_1_1" for intentional use/hunting,
                     "2_1_2" for small-holder farming
    """
    result = await get_threat_details(threat_code, IUCN_TOKEN, redis_client)
    if not result:
        return json.dumps({
            "found": False,
            "message": f"No data found for threat code '{threat_code}'"
        })
    return json.dumps({"found": True, "threat": result})

@mcp.tool()
async def get_countries() -> str:
    """
    Get list of all countries
    """
    result = await _get_countries(IUCN_TOKEN)
    if not result:
        return json.dumps({
            "found": False,
            "message": "No data found"
        })
    return json.dumps({"result": result})

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

from starlette.routing import Route

async def health(request):
    return JSONResponse({"status": "ok"})

if __name__ == "__main__":
    import uvicorn
    app = mcp.streamable_http_app()
    app.routes.append(Route("/health", health))
    uvicorn.run(app, host="0.0.0.0", port=8001,
                server_header=False,
                forwarded_allow_ips="*")