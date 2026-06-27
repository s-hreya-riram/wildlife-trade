"""
WildlifeWatch Trade MCP Server.
Exposes wildlife trade intelligence as LLM-callable tools via MCP protocol.
Tools: classify_listing, lookup_cites, detect_coded_language, score_severity, generate_tip_report
"""
import json
import logging
import os
import re
import httpx
from openai import OpenAI
import anthropic

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SPECIES_PLUS_API_KEY = os.getenv("SPECIES_PLUS_API_KEY")
MCP_API_KEY = os.getenv("MCP_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not set")
if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY not set")
if not SPECIES_PLUS_API_KEY:
    raise ValueError("SPECIES_PLUS_API_KEY not set")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SPECIES_PLUS_BASE = "https://api.speciesplus.net/api/v1"

mcp = FastMCP(
    "wildlifewatch-trade",
    host="0.0.0.0",
    port=8002,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
    ),
)

# ---------------------------------------------------------------------------
# Coded language patterns — known trafficking euphemisms
# ---------------------------------------------------------------------------

CODED_PATTERNS = {
    "wet goods": "Known euphemism for live animals smuggled in liquid containers",
    "dry goods": "Euphemism for animal parts (skins, bones, scales)",
    "special medicine": "Traditional medicine code for protected species parts",
    "special turtle": "Code for protected freshwater/marine turtles",
    "special material": "Vague material descriptor hiding exotic animal origin",
    "premium exotic": "Often paired with illegal exotic animal products",
    "rare specimen": "Collector code for illegally sourced specimens",
    "farm raised": "Common false claim to legitimise CITES-protected species",
    "captive bred": "Often falsely claimed for wild-caught CITES species",
    "agarwood": "Aquilaria wood, CITES Appendix II, frequently smuggled",
    "oud": "Agarwood by another name, same trafficking concern",
    "rhino horn": "Critically Endangered, CITES Appendix I, all trade illegal",
    "ivory": "Elephant ivory, CITES Appendix I, banned international trade",
    "tiger bone": "CITES Appendix I, used in traditional medicine",
    "bear bile": "Moon bear / sun bear, CITES Appendix I",
    "pangolin scale": "CITES Appendix I, highest trafficked mammal globally",
    "shark fin": "Multiple shark species CITES listed",
    "turtle shell": "Hawksbill turtle CITES Appendix I",
    "manta ray gill": "CITES Appendix II, frequently trafficked",
    "dm for price": "Signals willingness to negotiate off-platform — trafficking red flag",
    "dm for details": "Same off-platform evasion signal",
    "no questions asked": "Explicit signal of illegitimate provenance",
    "discreet shipping": "Trafficking logistics code",
    "special delivery": "Off-channel shipping to evade detection",
    "live delivery guaranteed": "Code for live animal smuggling",
    "freshly imported": "May signal recent illegal importation",
    "direct from source": "Signals unregulated supply chain",
    "limited stock": "Scarcity signal common in illegal wildlife markets",
}

# ---------------------------------------------------------------------------
# Tool 1: classify_listing
# ---------------------------------------------------------------------------

@mcp.tool()
async def classify_listing(
    title: str,
    description: str,
    image_url: str = "",
    image_b64: str = "",
) -> str:
    """
    Classify a marketplace listing to identify potential wildlife species or
    protected materials using GPT-4o vision. Returns species identification,
    confidence score, and material category.

    Args:
        title: listing title or item name
        description: full listing text/description
        image_url: optional URL to listing image for visual analysis
    """
    system_prompt = """You are a wildlife trade expert and taxonomist. 
Analyse marketplace listings to identify whether they involve protected wildlife species or materials.

Return ONLY valid JSON with this exact structure:
{
  "species_common": "common name or null",
  "species_latin": "scientific name or null",
  "material_type": "one of: live_animal, animal_part, plant, timber, medicine, unknown",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation of identification",
  "high_risk_indicators": ["list", "of", "red", "flags"]
}

If no wildlife concern is detected, set species to null, confidence below 0.3, and explain."""

    text_content = f"Listing title: {title}\n\nListing description: {description}"

    messages_content = []

    if image_b64:
        messages_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}", "detail": "high"},
        })
    elif image_url and image_url.startswith("http"):
        messages_content.append({
            "type": "image_url",
            "image_url": {"url": image_url, "detail": "high"},
        })

    messages_content.append({"type": "text", "text": text_content})

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            max_tokens=600,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": messages_content},
            ],
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        result = json.loads(raw)
        return json.dumps({"success": True, "classification": result})
    except Exception as e:
        logger.error("classify_listing error: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Tool 2: lookup_cites
# ---------------------------------------------------------------------------

@mcp.tool()
async def lookup_cites(species_name: str) -> str:
    """
    Look up CITES listing status for a species using the Species+ API.
    Returns appendix status, listing annotations, and trade restrictions.

    Args:
        species_name: common or scientific name e.g. "Pangolin", "Manis javanica"
    """
    headers = {"X-Authentication-Token": SPECIES_PLUS_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{SPECIES_PLUS_BASE}/taxon_concepts",
                headers=headers,
                params={"name": species_name, "language": "en"},
            )
            resp.raise_for_status()
            data = resp.json()

        taxa = data.get("taxon_concepts", [])
        if not taxa:
            return json.dumps({
                "found": False,
                "species": species_name,
                "message": "Not found in Species+ database — may not be CITES listed",
            })

        # Take the first (best) match
        taxon = taxa[0]
        cites_listings = taxon.get("cites_listings", [])

        # Find the current (most recent) listing
        current = None
        for listing in cites_listings:
            if listing.get("is_current"):
                current = listing
                break
        if not current and cites_listings:
            current = cites_listings[0]

        appendix = current.get("appendix") if current else None
        annotation = current.get("annotation") if current else None

        # Fetch full taxon details for trade restrictions
        taxon_id = taxon.get("id")
        trade_restrictions = []
        if taxon_id:
            try:
                detail_resp = await client.get(
                    f"{SPECIES_PLUS_BASE}/taxon_concepts/{taxon_id}/eu_legislation",
                    headers=headers,
                )
                if detail_resp.status_code == 200:
                    eu_data = detail_resp.json()
                    eu_listings = eu_data.get("eu_listings", [])
                    for eu in eu_listings[:2]:
                        if eu.get("is_current"):
                            trade_restrictions.append(
                                f"EU Annex {eu.get('annex', '?')}: {eu.get('change_type_name', '')}"
                            )
            except Exception:
                pass

        result = {
            "found": True,
            "species_name": taxon.get("full_name", species_name),
            "common_names": [
                cn.get("name") for cn in taxon.get("common_names", [])
                if cn.get("language") == "English"
            ][:3],
            "cites_appendix": appendix,
            "is_listed": appendix is not None,
            "annotation": annotation,
            "trade_restrictions": trade_restrictions,
            "listing_history": [
                {
                    "appendix": l.get("appendix"),
                    "effective_at": l.get("effective_at"),
                    "is_current": l.get("is_current", False),
                }
                for l in cites_listings[:5]
            ],
            "trade_illegal": appendix == "I",
            "trade_restricted": appendix in ("I", "II"),
        }

        return json.dumps({"success": True, "cites": result})

    except httpx.HTTPStatusError as e:
        logger.error("Species+ API error %s for: %s", e.response.status_code, species_name)
        return json.dumps({"success": False, "error": f"API error {e.response.status_code}"})
    except Exception as e:
        logger.error("lookup_cites error: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Tool 3: detect_coded_language
# ---------------------------------------------------------------------------

@mcp.tool()
async def detect_coded_language(text: str) -> str:
    """
    Scan listing text for known wildlife trafficking coded language and euphemisms.
    Returns matched patterns with explanations and a composite flag score.

    Args:
        text: the full listing text (title + description combined)
    """
    text_lower = text.lower()
    matches = []

    for pattern, explanation in CODED_PATTERNS.items():
        if pattern.lower() in text_lower:
            matches.append({
                "pattern": pattern,
                "explanation": explanation,
            })

    # Score: 0.0–1.0 based on number and severity of matches
    if not matches:
        score = 0.0
    elif len(matches) == 1:
        score = 0.35
    elif len(matches) == 2:
        score = 0.60
    else:
        score = min(0.9, 0.60 + (len(matches) - 2) * 0.1)

    # High-severity single-match override (explicit contraband terms)
    explicit = {"rhino horn", "ivory", "tiger bone", "bear bile", "pangolin scale"}
    if any(m["pattern"] in explicit for m in matches):
        score = max(score, 0.85)

    return json.dumps({
        "matched_patterns": [m["pattern"] for m in matches],
        "match_details": matches,
        "match_count": len(matches),
        "language_flag_score": round(score, 2),
        "flagged": score >= 0.35,
    })


# ---------------------------------------------------------------------------
# Tool 4: score_severity
# ---------------------------------------------------------------------------

@mcp.tool()
async def score_severity(
    classification_confidence: float,
    cites_appendix: str,
    iucn_status: str,
    language_flag_score: float,
    material_type: str = "unknown",
) -> str:
    """
    Synthesise classifier confidence, CITES status, IUCN status, and coded
    language score into a single HIGH/MEDIUM/LOW severity rating with reasoning.

    Args:
        classification_confidence: 0.0-1.0 from classify_listing
        cites_appendix: "I", "II", "III", or "none"
        iucn_status: IUCN Red List code e.g. "CR", "EN", "VU", "NT", "LC", "DD"
        language_flag_score: 0.0-1.0 from detect_coded_language
        material_type: live_animal | animal_part | plant | timber | medicine | unknown
    """
    score = 0.0
    reasons = []

    # CITES weight (40%)
    cites_scores = {"I": 1.0, "II": 0.6, "III": 0.3, "none": 0.0}
    cites_score = cites_scores.get(cites_appendix.upper() if cites_appendix else "none", 0.0)
    score += cites_score * 0.40
    if cites_appendix and cites_appendix.upper() == "I":
        reasons.append(f"CITES Appendix I — all commercial trade is illegal")
    elif cites_appendix and cites_appendix.upper() == "II":
        reasons.append(f"CITES Appendix II — trade requires permits and is strictly regulated")
    elif cites_appendix and cites_appendix.upper() == "III":
        reasons.append(f"CITES Appendix III — trade regulated in listing countries")

    # IUCN weight (25%)
    iucn_scores = {"CR": 1.0, "EN": 0.8, "VU": 0.6, "NT": 0.3, "LC": 0.1, "DD": 0.2, "EX": 0.0, "EW": 0.5}
    iucn_score = iucn_scores.get(iucn_status.upper() if iucn_status else "DD", 0.2)
    score += iucn_score * 0.25
    iucn_labels = {"CR": "Critically Endangered", "EN": "Endangered", "VU": "Vulnerable", "NT": "Near Threatened", "LC": "Least Concern", "DD": "Data Deficient"}
    if iucn_status and iucn_status.upper() in ("CR", "EN", "VU"):
        reasons.append(f"IUCN {iucn_status.upper()} ({iucn_labels.get(iucn_status.upper(), '')})")

    # Classifier confidence weight (25%)
    score += classification_confidence * 0.25
    if classification_confidence >= 0.7:
        reasons.append(f"High-confidence species identification ({classification_confidence:.0%})")
    elif classification_confidence >= 0.4:
        reasons.append(f"Moderate species identification confidence ({classification_confidence:.0%})")

    # Language flag weight (10%)
    score += language_flag_score * 0.10
    if language_flag_score >= 0.35:
        reasons.append("Coded trafficking language detected in listing text")

    # Material type modifier
    if material_type == "live_animal":
        score = min(1.0, score * 1.15)
        reasons.append("Live animal — heightened welfare and trafficking concern")
    elif material_type == "animal_part":
        score = min(1.0, score * 1.10)

    # Thresholds
    if score >= 0.65:
        severity = "HIGH"
        color = "#e53e3e"
    elif score >= 0.35:
        severity = "MEDIUM"
        color = "#dd6b20"
    else:
        severity = "LOW"
        color = "#38a169"

    reason_str = "; ".join(reasons) if reasons else "Low risk indicators across all signals"

    return json.dumps({
        "severity": severity,
        "severity_color": color,
        "composite_score": round(score, 3),
        "severity_reason": reason_str,
        "iucn_status": iucn_status,
        "signal_breakdown": {
            "cites_contribution": round(cites_score * 0.40, 3),
            "iucn_contribution": round(iucn_score * 0.25, 3),
            "classifier_contribution": round(classification_confidence * 0.25, 3),
            "language_contribution": round(language_flag_score * 0.10, 3),
        },
    })


# ---------------------------------------------------------------------------
# Tool 5: generate_tip_report
# ---------------------------------------------------------------------------

@mcp.tool()
async def generate_tip_report(
    title: str,
    description: str,
    platform: str,
    species_common: str,
    species_latin: str,
    cites_appendix: str,
    iucn_status: str,
    severity: str,
    severity_reason: str,
    matched_patterns: str,
    classifier_confidence: float,
) -> str:
    """
    Generate a structured tip report formatted for submission to wildlife
    enforcement agencies (TRAFFIC, NParks, AVS). Uses Claude to produce
    professional, actionable intelligence.

    Args:
        title: original listing title
        description: original listing description
        platform: marketplace platform
        species_common: common name of identified species
        species_latin: scientific name of identified species
        cites_appendix: CITES appendix status
        iucn_status: IUCN Red List category code
        severity: HIGH / MEDIUM / LOW
        severity_reason: one-line reason from severity scorer
        matched_patterns: comma-separated list of coded language matches
        classifier_confidence: 0.0-1.0 species ID confidence
    """
    iucn_labels = {
        "CR": "Critically Endangered", "EN": "Endangered", "VU": "Vulnerable",
        "NT": "Near Threatened", "LC": "Least Concern", "DD": "Data Deficient",
    }
    iucn_label = iucn_labels.get(iucn_status.upper(), iucn_status)
    patterns_list = matched_patterns if matched_patterns else "None detected"

    prompt = f"""Generate a concise, professional wildlife trade intelligence tip report for submission to enforcement agencies.

LISTING DATA:
- Platform: {platform}
- Title: {title}
- Description: {description}

ANALYSIS RESULTS:
- Species Identified: {species_common} ({species_latin}) — {classifier_confidence:.0%} confidence
- CITES Status: Appendix {cites_appendix} {"(all commercial trade ILLEGAL)" if cites_appendix == "I" else "(regulated trade)"}
- IUCN Status: {iucn_status} — {iucn_label}
- Severity: {severity}
- Primary Concern: {severity_reason}
- Coded Language Detected: {patterns_list}

Write the report in markdown with these sections:
## Intelligence Summary
## Species & Conservation Status
## Listing Analysis
## Why This Is Suspicious
## Recommended Action

Keep each section to 2-4 sentences. Be factual and specific. End with a clear recommended action for the receiving agency."""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        report_text = response.content[0].text
        return json.dumps({"success": True, "report_markdown": report_text})
    except Exception as e:
        logger.error("generate_tip_report error: %s", e)
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

from starlette.routing import Route
from starlette.responses import JSONResponse

async def health(request):
    return JSONResponse({"status": "ok"})

if __name__ == "__main__":
    import uvicorn
    app = mcp.streamable_http_app()
    app.routes.append(Route("/health", health))
    uvicorn.run(app, host="0.0.0.0", port=8002,
                server_header=False, forwarded_allow_ips="*")