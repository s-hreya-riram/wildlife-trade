"""
Database models and queries for WildlifeWatch.
Uses asyncpg directly for async Postgres access.
Tables are created on startup via init_db().
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://wildlifewatch:wildlifewatch@postgres:5432/wildlifewatch")

_pool: Optional[asyncpg.Pool] = None


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def init_db():
    """Create tables and indexes on startup if they don't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id              TEXT PRIMARY KEY,
                species_name    TEXT,
                country_code    TEXT,
                year_from       INTEGER,
                year_to         INTEGER,
                report_url      TEXT NOT NULL,
                score_factual   FLOAT,
                score_complete  FLOAT,
                score_action    FLOAT,
                score_reasoning TEXT,
                generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # indexes for the query patterns we care about
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_species
            ON reports (species_name)
            WHERE species_name IS NOT NULL
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_country
            ON reports (country_code)
            WHERE country_code IS NOT NULL
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_year_range
            ON reports (year_from, year_to)
            WHERE year_from IS NOT NULL OR year_to IS NOT NULL
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_generated_at
            ON reports (generated_at DESC)
        """)

    logger.info("Database initialised")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

async def save_report(
    report_id: str,
    species: Optional[str],
    country: Optional[str],
    year_from: Optional[int],
    year_to: Optional[int],
    scores: dict,
    report_url: str,
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO reports (
                id, species_name, country_code, year_from, year_to,
                report_url, score_factual, score_complete, score_action,
                score_reasoning, generated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """,
            report_id,
            species,
            country,
            year_from,
            year_to,
            report_url,
            float(scores.get("factual_grounding", 0)),
            float(scores.get("completeness", 0)),
            float(scores.get("actionability", 0)),
            scores.get("reasoning", ""),
            datetime.utcnow(),
        )
    logger.info("Saved report %s to database", report_id)


async def get_reports(
    species: Optional[str] = None,
    country: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    limit: int = 20,
) -> list[dict]:
    pool = await get_pool()

    conditions = []
    params = []
    idx = 1

    if species:
        conditions.append(f"species_name ILIKE ${idx}")
        params.append(f"%{species}%")
        idx += 1
    if country:
        conditions.append(f"country_code = ${idx}")
        params.append(country.upper())
        idx += 1
    if year_from:
        conditions.append(f"year_from >= ${idx}")
        params.append(year_from)
        idx += 1
    if year_to:
        conditions.append(f"year_to <= ${idx}")
        params.append(year_to)
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    query = f"""
        SELECT
            id, species_name, country_code, year_from, year_to,
            report_url, score_factual, score_complete, score_action,
            score_reasoning, generated_at
        FROM reports
        {where}
        ORDER BY generated_at DESC
        LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [
        {
            "report_id": r["id"],
            "species_name": r["species_name"],
            "country_code": r["country_code"],
            "year_from": r["year_from"],
            "year_to": r["year_to"],
            "report_url": r["report_url"],
            "scores": {
                "factual_grounding": r["score_factual"],
                "completeness": r["score_complete"],
                "actionability": r["score_action"],
                "reasoning": r["score_reasoning"],
            },
            "generated_at": r["generated_at"].isoformat(),
        }
        for r in rows
    ]


async def get_report_by_id(report_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM reports WHERE id = $1", report_id
        )
    if not row:
        return None
    return {
        "report_id": row["id"],
        "species_name": row["species_name"],
        "country_code": row["country_code"],
        "year_from": row["year_from"],
        "year_to": row["year_to"],
        "report_url": row["report_url"],
        "scores": {
            "factual_grounding": row["score_factual"],
            "completeness": row["score_complete"],
            "actionability": row["score_action"],
            "reasoning": row["score_reasoning"],
        },
        "generated_at": row["generated_at"].isoformat(),
    }


async def check_cached_report(
    species: Optional[str],
    country: Optional[str],
    year_from: Optional[int],
    year_to: Optional[int],
    max_age_hours: int = 24,
) -> Optional[dict]:
    """
    Return a recent cached report matching the same parameters,
    if one exists within max_age_hours. Avoids regenerating reports
    for the same query within the cache window.
    """
    pool = await get_pool()

    conditions = ["generated_at > NOW() - INTERVAL '1 hour' * $1"]
    params: list = [max_age_hours]
    idx = 2

    if species:
        conditions.append(f"species_name ILIKE ${idx}")
        params.append(f"%{species}%")
        idx += 1
    else:
        conditions.append("species_name IS NULL")

    if country:
        conditions.append(f"country_code = ${idx}")
        params.append(country.upper())
        idx += 1
    else:
        conditions.append("country_code IS NULL")

    if year_from:
        conditions.append(f"year_from = ${idx}")
        params.append(year_from)
        idx += 1
    else:
        conditions.append("year_from IS NULL")

    if year_to:
        conditions.append(f"year_to = ${idx}")
        params.append(year_to)
        idx += 1
    else:
        conditions.append("year_to IS NULL")

    query = f"""
        SELECT * FROM reports
        WHERE {' AND '.join(conditions)}
        ORDER BY generated_at DESC
        LIMIT 1
    """

    async with pool.acquire() as conn:
        row = await conn.fetchrow(query, *params)

    if not row:
        return None

    return {
        "report_id": row["id"],
        "report_url": row["report_url"],
        "scores": {
            "factual_grounding": row["score_factual"],
            "completeness": row["score_complete"],
            "actionability": row["score_action"],
            "reasoning": row["score_reasoning"],
        },
        "generated_at": row["generated_at"].isoformat(),
        "cached": True,
    }