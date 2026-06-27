"""
IUCN Red List API v4 client.
Fetches species conservation status and assessment history.
Redis is used for caching to avoid redundant API calls.
"""
import json
import logging
import httpx
import redis.asyncio as aioredis
from typing import Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://api.iucnredlist.org/api/v4"
CACHE_TTL_SECONDS = 60 * 60 * 24  # 24 hours


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_assessment(raw: dict) -> dict:
    taxon = raw.get("taxon", {})

    pop_trend = raw.get("population_trend") or {}
    pop_trend_desc = pop_trend.get("description") or {}
    population_trend = pop_trend_desc.get("en", "Unknown") if isinstance(pop_trend_desc, dict) else "Unknown"

    red_list = raw.get("red_list_category") or {}
    red_list_desc = red_list.get("description") or {}
    status_label = red_list_desc.get("en", "Unknown") if isinstance(red_list_desc, dict) else "Unknown"

    # get english common name, prefer main=True
    common_names = taxon.get("common_names") or []
    common_name = next(
        (c["name"] for c in common_names if c.get("main") and c.get("language") == "eng"),
        next((c["name"] for c in common_names if c.get("language") == "eng"), "Unknown")
    )

    return {
        "assessment_id": raw.get("assessment_id"),
        "sis_taxon_id": raw.get("sis_taxon_id"),
        "name": taxon.get("scientific_name", "Unknown"),
        "common_name": common_name,
        "kingdom": taxon.get("kingdom_name", "Unknown"),
        "phylum": taxon.get("phylum_name", "Unknown"),
        "class": taxon.get("class_name", "Unknown"),
        "order": taxon.get("order_name", "Unknown"),
        "family": taxon.get("family_name", "Unknown"),
        "status": red_list.get("code", "Unknown"),
        "status_label": status_label,
        "population_trend": population_trend,
        "year_published": raw.get("year_published"),
        "url": raw.get("url", ""),
        "criteria": raw.get("criteria", ""),
        "threats": [
            {
                "title": (t.get("description") or {}).get("en", ""),
                "timing": t.get("timing", ""),        # plain string
                "severity": t.get("severity", ""),    # plain string
                "scope": t.get("scope", ""),          # plain string
                "score": t.get("score", ""),
            }
            for t in raw.get("threats", [])
        ],
    }


def _parse_taxa_entry(entry: dict) -> dict:
    """Parse a taxa-level summary (from /taxa/scientific_name)."""
    return {
        "sis_id": entry.get("sis_id"),
        "assessment_id": entry.get("latest_assessment", {}).get("assessment_id"),
        "name": f"{entry.get('genus_name', '')} {entry.get('species_name', '')}".strip(),
        "status": entry.get("latest_assessment", {}).get("red_list_category", {}).get("code", "Unknown"),
        "year_published": entry.get("latest_assessment", {}).get("year_published"),
        "historic_assessments": [
            {
                "assessment_id": a.get("assessment_id"),
                "year_published": a.get("year_published"),
                "status": a.get("red_list_category", {}).get("code", "Unknown"),
            }
            for a in entry.get("assessments", [])
        ],
    }


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

async def get_assessment(
    assessment_id: int,
    token: str,
    redis: aioredis.Redis,
) -> Optional[dict]:
    """Fetch full assessment data by assessment ID."""
    cache_key = f"iucn:assessment:{assessment_id}"

    cached = await redis.get(cache_key)
    if cached:
        logger.debug("Cache hit: %s", cache_key)
        return json.loads(cached)

    try:
        async with httpx.AsyncClient(headers=_auth_headers(token), timeout=15.0) as client:
            resp = await client.get(f"{BASE_URL}/assessment/{assessment_id}")
            resp.raise_for_status()
            raw = resp.json()

            # handle double-encoded JSON
            if isinstance(raw, str):
                raw = json.loads(raw)

            # print("ASSESSMENT RAW TYPE:", type(raw))
            # print("ASSESSMENT RAW KEYS:", list(raw.keys()) if isinstance(raw, dict) else raw[:200])
            # print("ASSESSMENT RAW:", json.dumps(raw)[:1000])
            result = _parse_assessment(raw)
            await redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
            return result

    except httpx.TimeoutException:
        logger.warning("Timeout fetching assessment: %s", assessment_id)
        return None
    except httpx.HTTPStatusError as e:
        logger.error("Assessment fetch returned %s for id: %s", e.response.status_code, assessment_id)
        return None
    except Exception as e:
        logger.error("Unexpected error in get_assessment: %s", e)
        return None


async def search_species(
    query: str,
    token: str,
    redis: aioredis.Redis,
) -> list[dict]:
    cache_key = f"iucn:search:{query.lower().strip()}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    parts = query.strip().split()
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token), timeout=15.0) as client:
            params = {"genus_name": parts[0]}
            if len(parts) > 1:
                params["species_name"] = parts[1]

            resp = await client.get(f"{BASE_URL}/taxa/scientific_name", params=params)
            resp.raise_for_status()
            data = resp.json()

            # response is a list at top level
            entries = data if isinstance(data, list) else data.get("assessments", [])

            result = [
                {
                    "sis_taxon_id": e.get("sis_taxon_id"),
                    "assessment_id": e.get("assessment_id"),
                    "name": e.get("taxon_scientific_name", ""),
                    "status": e.get("red_list_category_code", "Unknown"),
                    "year_published": e.get("year_published"),
                    "latest": e.get("latest", False),
                }
                for e in entries[:10]
            ]

            await redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
            return result

    except httpx.TimeoutException:
        logger.warning("Timeout searching IUCN for: %s", query)
        return []
    except httpx.HTTPStatusError as e:
        logger.error("IUCN search returned %s for: %s", e.response.status_code, query)
        return []
    except Exception as e:
        logger.error("Unexpected error in search_species: %s", e)
        return []


async def get_species(
    genus_name: str,
    species_name: str,
    token: str,
    redis: aioredis.Redis,
) -> Optional[dict]:
    cache_key = f"iucn:species:{genus_name.lower()}:{species_name.lower()}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    try:
        async with httpx.AsyncClient(headers=_auth_headers(token), timeout=15.0) as client:
            resp = await client.get(
                f"{BASE_URL}/taxa/scientific_name",
                params={"genus_name": genus_name, "species_name": species_name},
            )
            resp.raise_for_status()
            data = resp.json()
            entries = data if isinstance(data, list) else data.get("assessments", [])

            if not entries:
                return None

            # find the latest assessment
            latest = next((e for e in entries if e.get("latest") is True), entries[0])
            assessment_id = latest.get("assessment_id")

            if not assessment_id:
                return None

            result = await get_assessment(assessment_id, token, redis)
            if result:
                await redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
            return result

    except httpx.TimeoutException:
        logger.warning("Timeout fetching species: %s %s", genus_name, species_name)
        return None
    except httpx.HTTPStatusError as e:
        logger.error("Species fetch returned %s for: %s %s", e.response.status_code, genus_name, species_name)
        return None
    except Exception as e:
        logger.error("Unexpected error in get_species: %s", e)
        return None


async def get_historical_assessments(
    genus_name: str,
    species_name: str,
    token: str,
    redis: aioredis.Redis,
) -> list[dict]:
    cache_key = f"iucn:history:{genus_name.lower()}:{species_name.lower()}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    try:
        async with httpx.AsyncClient(headers=_auth_headers(token), timeout=15.0) as client:
            resp = await client.get(
                f"{BASE_URL}/taxa/scientific_name",
                params={"genus_name": genus_name, "species_name": species_name},
            )
            resp.raise_for_status()
            data = resp.json()
            entries = data if isinstance(data, list) else data.get("assessments", [])

            if not entries:
                return []

            result = sorted(
                [
                    {
                        "assessment_id": e.get("assessment_id"),
                        "year_published": e.get("year_published"),
                        "status": e.get("red_list_category_code", "Unknown"),
                        "latest": e.get("latest", False),
                        "url": e.get("url", ""),
                    }
                    for e in entries
                ],
                key=lambda x: x["year_published"] or "0",
            )

            await redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
            return result

    except httpx.TimeoutException:
        logger.warning("Timeout fetching history for: %s %s", genus_name, species_name)
        return []
    except httpx.HTTPStatusError as e:
        logger.error("History fetch returned %s for: %s %s", e.response.status_code, genus_name, species_name)
        return []
    except Exception as e:
        logger.error("Unexpected error in get_historical_assessments: %s", e)
        return []
    
async def get_countries(token: str, page: int = 1):
    """
    Get threatened species assessments for a given country ISO alpha-2 code.
    Returns latest assessments only, filtered to threatened categories (CR, EN, VU).
    """
    try:
        async with httpx.AsyncClient(headers=_auth_headers(token), timeout=15.0) as client:
            resp = await client.get(
                f"{BASE_URL}/countries",
                params={"latest": "true", "page": page},
            )
            print("Response: {}", resp)
            resp.raise_for_status()
            data = resp.json()
            print("Data: {}", data)
            return data
    except Exception as e:
        logger.error("Unexpected error in get_countries: %s", e)
        return []
    
async def get_species_by_country(
    country_code: str,
    token: str,
    redis: aioredis.Redis,
    page: int = 1,
) -> list[dict]:
    """
    Get threatened species assessments for a given country ISO alpha-2 code.
    Returns latest assessments only, filtered to threatened categories (CR, EN, VU).
    """
    cache_key = f"iucn:country:{country_code.upper()}:{page}"

    cached = await redis.get(cache_key)
    if cached:
        logger.debug("Cache hit: %s", cache_key)
        return json.loads(cached)

    try:
        async with httpx.AsyncClient(headers=_auth_headers(token), timeout=15.0) as client:
            resp = await client.get(
                f"{BASE_URL}/countries/{country_code.upper()}",
                params={"latest": "true", "page": page},
            )
            print("Response: {}", resp)
            resp.raise_for_status()
            data = resp.json()
            print("Data: {}", data)
            # response is a list of assessment summaries
            entries = data if isinstance(data, list) else data.get("assessments", [])

            # filter to threatened categories only — CR, EN, VU
            threatened = {"CR", "EN", "VU"}
            result = [
                {
                    "assessment_id": e.get("assessment_id"),
                    "name": e.get("taxon_scientific_name", ""),
                    "status": e.get("red_list_category_code", "Unknown"),
                    "year_published": e.get("year_published"),
                    "url": e.get("url", ""),
                }
                for e in entries
                if e.get("red_list_category_code") in threatened
            ]

            await redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
            return result

    except httpx.TimeoutException:
        logger.warning("Timeout fetching species for country: %s", country_code)
        return []
    except httpx.HTTPStatusError as e:
        logger.error("Country fetch returned %s for: %s", e.response.status_code, country_code)
        return []
    except Exception as e:
        logger.error("Unexpected error in get_species_by_country: %s", e)
        return []


async def get_threat_details(
    threat_code: str,
    token: str,
    redis: aioredis.Redis,
) -> Optional[dict]:
    """
    Get details and affected assessments for a specific IUCN threat code.
    Useful for contextualising threat codes returned in species assessments.
    e.g. threat code "5_1_1" = Intentional use (species is the target)
    """
    cache_key = f"iucn:threat:{threat_code}"

    cached = await redis.get(cache_key)
    if cached:
        logger.debug("Cache hit: %s", cache_key)
        return json.loads(cached)

    try:
        async with httpx.AsyncClient(headers=_auth_headers(token), timeout=15.0) as client:
            resp = await client.get(
                f"{BASE_URL}/threats/{threat_code}",
                params={"latest": "true"},
            )
            resp.raise_for_status()
            data = resp.json()

            entries = data if isinstance(data, list) else data.get("assessments", [])

            result = {
                "threat_code": threat_code,
                "affected_species_count": len(entries),
                "sample_affected": [
                    {
                        "name": e.get("taxon_scientific_name", ""),
                        "status": e.get("red_list_category_code", "Unknown"),
                    }
                    for e in entries[:5]  # just a sample for context
                ],
            }

            await redis.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))
            return result

    except httpx.TimeoutException:
        logger.warning("Timeout fetching threat details: %s", threat_code)
        return None
    except httpx.HTTPStatusError as e:
        logger.error("Threat fetch returned %s for code: %s", e.response.status_code, threat_code)
        return None
    except Exception as e:
        logger.error("Unexpected error in get_threat_details: %s", e)
        return None