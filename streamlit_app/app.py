"""
WildlifeWatch Trade Intel — Streamlit App
Three views:
  1. Threat Landscape  — most endangered species in SEA (live IUCN data)
  2. Analyse a Listing — submit listing/field sighting for AI analysis
  3. Tip Report        — structured report ready for enforcement submission
"""

import base64
import json
import os
from pathlib import Path
from typing import Optional

import httpx
import streamlit as st
from fpdf import FPDF

# ── Config ────────────────────────────────────────────────────────────────────

API_URL = os.getenv("API_URL", "http://localhost:8000")
if not API_URL.startswith("http"):
    API_URL = f"https://{API_URL}"

DEMO_DATA_PATH = Path(__file__).parent / "demo_listings.json"

SEA_COUNTRIES = {
    "SG": "Singapore",
    "ID": "Indonesia",
    "MY": "Malaysia",
    "TH": "Thailand",
    "VN": "Vietnam",
    "PH": "Philippines",
    "MM": "Myanmar",
    "KH": "Cambodia",
}

IUCN_LABELS = {
    "CR": ("Critically Endangered", "#e53e3e"),
    "EN": ("Endangered", "#dd6b20"),
    "VU": ("Vulnerable", "#d69e2e"),
    "NT": ("Near Threatened", "#38a169"),
    "LC": ("Least Concern", "#718096"),
    "EX": ("Extinct", "#1a202c"),
    "EW": ("Extinct in the Wild", "#2d3748"),
    "DD": ("Data Deficient", "#a0aec0"),
}

SEVERITY_META = {
    "HIGH":   ("🔴", "#e53e3e", "#fee2e2"),
    "MEDIUM": ("🟠", "#dd6b20", "#ffedd5"),
    "LOW":    ("🟢", "#166534", "#dcfce7"),
}

SPECIES_IMAGES = {
    "Manis javanica": "https://upload.wikimedia.org/wikipedia/commons/a/a3/Pangolin.jpg",
    "Manis spp": "https://upload.wikimedia.org/wikipedia/commons/a/a3/Pangolin.jpg",
    "Panthera tigris": "https://upload.wikimedia.org/wikipedia/commons/6/66/Adult_male_Royal_Bengal_tiger.jpg",
    "Loxodonta africana": "https://upload.wikimedia.org/wikipedia/commons/9/94/178_Male_African_bush_elephant_in_Etosha_National_Park_Photo_by_Giles_Laurent.jpg",
}


def get_species_image(latin: str) -> Optional[str]:
    if not latin:
        return None
    if latin in SPECIES_IMAGES:
        return SPECIES_IMAGES[latin]
    genus = latin.split()[0]
    for key, url in SPECIES_IMAGES.items():
        if key.startswith(genus):
            return url
    return None

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="WildlifeWatch Trade Intel",
    page_icon="🦅",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styles ────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap');
  html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }

  section[data-testid="stSidebar"] { background-color: #0d1f0f; border-right: 1px solid #1a3d1e; }
  section[data-testid="stSidebar"] * { color: #c8e6c9 !important; }

  .stat-card { background: #0d1f0f; border: 1px solid #1a3d1e; border-radius: 8px; padding: 20px 24px; text-align: center; }
  .stat-number { font-family: 'DM Mono', monospace; font-size: 2.4rem; font-weight: 500; color: #4ade80; line-height: 1; }
  .stat-label { font-size: 0.78rem; color: #6b7280; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 6px; }

  .listing-card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 16px 20px; margin-bottom: 10px; border-left: 4px solid #e5e7eb; }
  .listing-title { font-weight: 600; font-size: 0.95rem; color: #111827; margin-bottom: 4px; }
  .listing-meta { font-size: 0.8rem; color: #6b7280; }
  .listing-species { font-style: italic; font-size: 0.82rem; color: #374151; margin-top: 4px; }

  .badge { font-family: 'DM Mono', monospace; font-size: 0.72rem; font-weight: 600; padding: 2px 9px; border-radius: 4px; display: inline-block; letter-spacing: 0.05em; }
  .badge-HIGH   { background: #fee2e2; color: #991b1b; }
  .badge-MEDIUM { background: #ffedd5; color: #9a3412; }
  .badge-LOW    { background: #dcfce7; color: #166534; }

  .species-card { background: #f8faf8; border: 1px solid #e2e8f0; border-left: 4px solid #16a34a; border-radius: 6px; padding: 14px 18px; margin-bottom: 8px; }
  .species-name { font-style: italic; font-size: 0.9rem; color: #1a202c; font-weight: 500; }

  .view-header { font-size: 1.5rem; font-weight: 600; color: #0d1f0f; margin-bottom: 4px; }
  .view-subheader { font-size: 0.88rem; color: #6b7280; margin-bottom: 24px; }

  #MainMenu { visibility: hidden; }
  footer { visibility: hidden; }
  .stDeployButton { display: none; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────

for key, default in [
    ("tip_report", None),
    ("analysis_result", None),
    ("nav", 0),  # 0=Landscape, 1=Analyse, 2=TipReport
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ── Sidebar nav — uses index, NOT key= on radio ───────────────────────────────

VIEWS = ["🌏 Threat Landscape", "🔍 Analyse a Listing", "📋 Tip Report"]

with st.sidebar:
    st.markdown("### 🦅 WildlifeWatch")
    st.markdown("**Trade Intelligence Platform**")
    st.markdown("---")

    selected = st.radio(
        "Navigate",
        VIEWS,
        index=st.session_state.nav,
        label_visibility="collapsed",
    )
    # keep nav index in sync with radio selection
    st.session_state.nav = VIEWS.index(selected)
    view = selected

    st.markdown("---")
    st.markdown(
        "<div style='font-size:0.75rem; color:#4b7a52;'>"
        "Data: IUCN Red List v4 · CITES Species+<br>"
        "Built for Build2026 🇸🇬"
        "</div>",
        unsafe_allow_html=True,
    )

# ── Helpers ───────────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sea_endangered(country_code: str) -> list:
    try:
        resp = httpx.get(f"{API_URL}/api/species/country/{country_code}", timeout=20)
        if resp.status_code == 200:
            return resp.json().get("species", [])
    except Exception:
        pass
    return []


def load_demo_listings() -> list:
    try:
        if DEMO_DATA_PATH.exists():
            return json.loads(DEMO_DATA_PATH.read_text())
    except Exception:
        pass
    return []


def call_analyse_listing(title, description, platform, image_url, image_b64=None) -> dict:
    try:
        resp = httpx.post(
            f"{API_URL}/api/trade/analyse",
            json={
                "title": title,
                "description": description,
                "platform": platform,
                "image_url": image_url or None,
                "image_b64": image_b64 or None,
            },
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def severity_badge(severity: str) -> str:
    return f'<span class="badge badge-{severity}">{severity}</span>'


def go_to(view_index: int):
    """Navigate to a view by index and rerun."""
    st.session_state.nav = view_index
    st.rerun()


def _build_synthetic_report(listing: dict) -> str:
    """Build a markdown tip report from a synthetic listing."""
    sev = listing.get("severity", "LOW")
    species = listing.get("species_common") or "Unknown"
    latin = listing.get("species_latin") or "Unknown"
    cites = listing.get("cites_appendix") or "Not listed"
    iucn = listing.get("iucn_status") or "Unknown"
    iucn_label = IUCN_LABELS.get(iucn, ("Unknown", "#718096"))[0]
    patterns = listing.get("matched_patterns", [])
    platform = listing.get("platform", "Unknown")
    title = listing.get("title", "Unknown")
    confidence = listing.get("confidence", 0)
    trade_legal = (
        "**All commercial trade is ILLEGAL**" if cites == "I"
        else "Trade requires CITES permits" if cites == "II"
        else "Trade not regulated under CITES"
    )
    pattern_sentence = (
        f'The following coded terms were identified: **{", ".join(patterns)}**.'
        if patterns else
        "No specific coded language was detected, but species identity and conservation status alone trigger concern."
    )
    why_suspicious = (
        f'The combination of a CITES Appendix {cites}-listed species with {len(patterns)} coded trafficking terms represents a high-probability trade violation.'
        if patterns else
        f'This species ({species}) is {iucn_label} and subject to CITES restrictions. The listing warrants investigation to verify legal provenance.'
    )
    action = (
        "Immediate referral to AVS (Animal & Veterinary Service) and NParks CITES enforcement unit. CITES Appendix I species — any trade is presumptively illegal without exceptional documentation."
        if cites == "I" else
        "Refer to AVS for permit verification. Request seller documentation of CITES export/import permits and chain of custody."
    )

    return f"""## Intelligence Summary
A **{sev} severity** wildlife trade concern has been identified on {platform}. The listing "{title}" has been flagged based on species identification, conservation status, and linguistic analysis of the listing text.

## Species & Conservation Status
- **Species Identified:** {species} (*{latin}*) — {confidence:.0%} confidence
- **CITES Status:** Appendix {cites} — {trade_legal}
- **IUCN Red List:** {iucn} ({iucn_label})

## Listing Analysis
The listing was detected on **{platform}** and contains language consistent with known wildlife trafficking patterns. {pattern_sentence}

## Why This Is Suspicious
{why_suspicious}

## Recommended Action
{action}"""


def _pdf_safe(text: str) -> str:
    replacements = {
        "\u2022": "-", "\u2013": "-", "\u2014": "-",
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2026": "...",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def _tip_report_to_pdf(markdown_text: str, report_id: str, species_common: str,
                        species_latin: str, severity: str) -> bytes:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(18, 18, 18)

    def line(text: str, size: float, style: str = "", gap_before: float = 0):
        if gap_before:
            pdf.ln(gap_before)
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", style, size)
        pdf.multi_cell(pdf.epw, size / 2, _pdf_safe(text))
        pdf.set_x(pdf.l_margin)

    line("WildlifeWatch Tip Report", 16, "B")
    pdf.set_text_color(90, 90, 90)
    subtitle = f"Report ID: {report_id}"
    if severity:
        subtitle += f"   |   Severity: {severity}"
    line(subtitle, 10)
    if species_common or species_latin:
        line(f"{species_common or ''} ({species_latin or ''})".strip(), 10, "I")
    pdf.set_text_color(0, 0, 0)

    for raw_line in markdown_text.splitlines():
        text = raw_line.strip()
        if not text:
            pdf.ln(2)
            continue
        clean = text.replace("**", "")
        if clean.startswith("## "):
            line(clean[3:], 13, "B", gap_before=3)
        elif clean.startswith("- "):
            line(f"  -  {clean[2:]}", 10.5)
        else:
            line(clean, 10.5)

    return bytes(pdf.output())


# ── View 1: Threat Landscape ──────────────────────────────────────────────────

if view == "🌏 Threat Landscape":
    st.markdown("""
    <div style="background:#0d1f0f; border-radius:10px; padding:28px 36px; margin-bottom:28px;">
      <div style="font-family:'DM Mono',monospace; font-size:0.72rem; color:#4ade80;
                  letter-spacing:0.14em; text-transform:uppercase; margin-bottom:8px;">
        Wildlife Trafficking
      </div>
      <div style="font-size:2.4rem; font-weight:600; color:#ffffff; line-height:1.15;">
        World's 4th largest criminal enterprise
      </div>
      <div style="font-size:0.95rem; color:#6b7280; margin-top:10px;">
        Estimated <span style="color:#4ade80; font-weight:500;">$23 billion annually</span>
        — and most of it has moved online.
      </div>
    </div>
    """, unsafe_allow_html=True)

    col_left, col_right = st.columns([1, 1], gap="large")

    with col_left:
        st.markdown('<div class="view-header">Most At Risk — Southeast Asia</div>', unsafe_allow_html=True)
        st.markdown('<div class="view-subheader">Live IUCN Red List data</div>', unsafe_allow_html=True)

        selected_name = st.selectbox(
            "Country", list(SEA_COUNTRIES.values()), index=0, label_visibility="collapsed",
        )
        country_code = {v: k for k, v in SEA_COUNTRIES.items()}[selected_name]

        with st.spinner(f"Loading data for {selected_name}…"):
            species_list = fetch_sea_endangered(country_code)

        if not species_list:
            st.info("Awaiting live data — species endpoint loading.")
        else:
            cr = sum(1 for s in species_list if s.get("status") == "CR")
            en = sum(1 for s in species_list if s.get("status") == "EN")
            vu = sum(1 for s in species_list if s.get("status") == "VU")
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown(f'<div class="stat-card"><div class="stat-number" style="color:#e53e3e">{cr}</div><div class="stat-label">Critical</div></div>', unsafe_allow_html=True)
            with c2:
                st.markdown(f'<div class="stat-card"><div class="stat-number" style="color:#dd6b20">{en}</div><div class="stat-label">Endangered</div></div>', unsafe_allow_html=True)
            with c3:
                st.markdown(f'<div class="stat-card"><div class="stat-number" style="color:#d69e2e">{vu}</div><div class="stat-label">Vulnerable</div></div>', unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            show_all = st.toggle("Show EN + VU", value=False)
            filtered = species_list if show_all else [s for s in species_list if s.get("status") == "CR"]
            for s in filtered[:30]:
                status_code = s.get("status", "DD")
                label, color = IUCN_LABELS.get(status_code, ("Unknown", "#718096"))
                st.markdown(f"""
                <div class="species-card" style="border-left-color:{color}">
                  <div class="species-name">{s.get('name', 'Unknown')}</div>
                  <span style="font-size:0.75rem; color:{color}; font-weight:600;">{status_code} · {label}</span>
                </div>
                """, unsafe_allow_html=True)

    with col_right:
        st.markdown('<div class="view-header">Recent Intelligence Feed</div>', unsafe_allow_html=True)
        st.markdown('<div class="view-subheader">Flagged listings from monitored platforms</div>', unsafe_allow_html=True)

        listings = load_demo_listings()
        if not listings:
            st.info("Demo dataset not found. Place demo_listings.json in the streamlit_app folder.")
        else:
            high = sum(1 for l in listings if l.get("severity") == "HIGH")
            med  = sum(1 for l in listings if l.get("severity") == "MEDIUM")
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown(f'<div class="stat-card"><div class="stat-number">{len(listings)}</div><div class="stat-label">Scanned</div></div>', unsafe_allow_html=True)
            with c2:
                st.markdown(f'<div class="stat-card"><div class="stat-number" style="color:#e53e3e">{high}</div><div class="stat-label">High Severity</div></div>', unsafe_allow_html=True)
            with c3:
                st.markdown(f'<div class="stat-card"><div class="stat-number" style="color:#dd6b20">{med}</div><div class="stat-label">Medium</div></div>', unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)

            sev_filter = st.segmented_control(
                "Filter", ["All", "HIGH", "MEDIUM", "LOW"],
                default="All", label_visibility="collapsed",
            )
            show_listings = listings if sev_filter == "All" else [l for l in listings if l.get("severity") == sev_filter]

            border_colors = {"HIGH": "#e53e3e", "MEDIUM": "#dd6b20", "LOW": "#16a34a"}
            for listing in show_listings:
                sev = listing.get("severity", "LOW")
                bc = border_colors.get(sev, "#e5e7eb")
                species = listing.get("species_common") or "Species unidentified"
                latin = listing.get("species_latin") or ""
                patterns = listing.get("matched_patterns", [])
                pattern_str = f" · ⚠️ {', '.join(patterns[:2])}" if patterns else ""

                st.markdown(f"""
                <div class="listing-card" style="border-left-color:{bc}">
                  <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                    <div class="listing-title">{listing.get('title','')}</div>
                    {severity_badge(sev)}
                  </div>
                  <div class="listing-meta">{listing.get('platform','')} · {listing.get('id','')}{pattern_str}</div>
                  <div class="listing-species">{species}{f' · <em>{latin}</em>' if latin else ''}</div>
                </div>
                """, unsafe_allow_html=True)

                if st.button("View report →", key=f"btn_{listing['id']}"):
                    st.session_state.tip_report = _build_synthetic_report(listing)
                    st.session_state.analysis_result = listing
                    go_to(2)  # navigate to Tip Report


# ── View 2: Analyse a Listing ─────────────────────────────────────────────────

elif view == "🔍 Analyse a Listing":
    st.markdown('<div class="view-header">Analyse a Listing</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="view-subheader">'
        'Submit a suspicious marketplace listing or field sighting. '
        'The agent identifies the species, checks CITES and IUCN status, '
        'detects coded trafficking language, and scores severity.'
        '</div>',
        unsafe_allow_html=True,
    )

    with st.form("listing_form"):
        platform = st.selectbox(
            "Platform",
            ["Carousell", "Facebook Marketplace", "Shopee", "Instagram DM",
             "Field sighting (no URL)", "WhatsApp", "Other"],
        )
        title = st.text_input(
            "Listing title or item description",
            placeholder="e.g. Rare exotic skin wallet, very special material",
        )
        description = st.text_area(
            "Full listing description",
            placeholder="Paste the full listing text here…",
            height=130,
        )
        image_url = st.text_input(
            "Image URL (optional — used for visual species identification)",
            placeholder="https://…",
        )
        uploaded = st.file_uploader("Or upload listing image", type=["jpg", "jpeg", "png"])
        submitted = st.form_submit_button("🔍 Run Analysis", use_container_width=True)

    if submitted:
        if not title and not description:
            st.warning("Please enter at least a title or description to analyse.")
        else:
            image_b64 = None
            if uploaded:
                image_b64 = base64.b64encode(uploaded.read()).decode()
                st.image(uploaded, caption="Uploaded image", width=300)

            with st.status("Running trade intelligence analysis…", expanded=True) as status_box:
                st.write("🔍 **Step 1/5** — Classifying species and material…")
                result = call_analyse_listing(title, description, platform, image_url, image_b64)

                if "error" in result:
                    status_box.update(label="Analysis failed ❌", state="error")
                    st.error(
                        f"Could not reach the trade analysis API: `{result['error']}`\n\n"
                        "Make sure the Trade MCP server is running and `API_URL` is set correctly."
                    )
                else:
                    species_c = result.get("species_common") or "Unknown"
                    species_l = result.get("species_latin") or ""
                    conf = result.get("confidence", 0)
                    st.write(f"✅ **Step 1** — Species: **{species_c}**{f' (*{species_l}*)' if species_l else ''} · {conf:.0%} confidence")

                    cites = result.get("cites_appendix") or "Not found"
                    cites_illegal = result.get("cites_trade_illegal", False)
                    st.write(f"📋 **Step 2** — CITES: **Appendix {cites}**" + (" ⛔ Trade ILLEGAL" if cites_illegal else ""))

                    patterns = result.get("matched_patterns", [])
                    if patterns:
                        st.write(f"⚠️ **Step 3** — Coded language: `{'`, `'.join(patterns)}`")
                    else:
                        st.write("✅ **Step 3** — No coded trafficking language detected")

                    iucn_raw = result.get("iucn_status", "")
                    iucn_label_str = IUCN_LABELS.get(iucn_raw, ("Unknown", ""))[0]
                    st.write(f"🌿 **Step 4** — IUCN: **{iucn_raw}** ({iucn_label_str})")

                    sev = result.get("severity", "LOW")
                    sev_reason = result.get("severity_reason", "")
                    emoji = SEVERITY_META.get(sev, ("🟢", "", ""))[0]
                    st.write(f"🎯 **Step 5** — Severity: {emoji} **{sev}** — {sev_reason}")

                    status_box.update(label="Analysis complete ✅", state="complete")
                    st.session_state.analysis_result = result
                    st.session_state.tip_report = result.get("report_markdown")

            if st.session_state.tip_report:
                st.success("Tip report generated.")
                if st.button("📋 View Tip Report →", use_container_width=True):
                    go_to(2)


# ── View 3: Tip Report ────────────────────────────────────────────────────────

elif view == "📋 Tip Report":
    st.markdown('<div class="view-header">Tip Report</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="view-subheader">'
        'Structured intelligence report for submission to TRAFFIC, NParks, or AVS.'
        '</div>',
        unsafe_allow_html=True,
    )

    if not st.session_state.tip_report:
        st.info(
            "No report generated yet. "
            "Go to **Analyse a Listing** and submit a suspicious listing, "
            "or click a listing card in the **Threat Landscape** feed."
        )
        if st.button("← Analyse a listing"):
            go_to(1)
    else:
        result = st.session_state.analysis_result or {}
        severity = result.get("severity", "")
        report_id = result.get("report_id") or result.get("id") or "DEMO"

        # Species image
        img_url = get_species_image(result.get("species_latin", ""))
        if img_url:
            col_img, col_meta = st.columns([1, 3])
            with col_img:
                st.image(img_url, width=160)
            with col_meta:
                if result.get("species_common"):
                    st.markdown(f"**{result['species_common']}**")
                if result.get("species_latin"):
                    st.markdown(f"*{result['species_latin']}*")
                if result.get("classification_reasoning"):
                    st.caption(result["classification_reasoning"])

        # Severity banner
        emoji, color, bg = SEVERITY_META.get(severity, ("", "#718096", "#f9fafb"))
        if severity:
            st.markdown(f"""
            <div style="background:{bg}; border:1px solid {color}44;
                        border-radius:8px; padding:14px 20px; margin-bottom:20px; margin-top:12px;">
              <div style="font-weight:600; color:{color}; font-size:1rem;">
                {emoji} {severity} SEVERITY · {report_id}
              </div>
              <div style="font-size:0.84rem; color:#4b5563; margin-top:4px;">
                {result.get('severity_reason', '')}
              </div>
            </div>
            """, unsafe_allow_html=True)

        # Metrics strip
        cites = result.get("cites_appendix")
        iucn_raw = result.get("iucn_status", "")
        iucn_label_str = IUCN_LABELS.get(iucn_raw, ("—", ""))[0]
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Species", result.get("species_common") or "—")
        with c2:
            st.metric("CITES", f"Appendix {cites}" if cites else "Not listed")
        with c3:
            st.metric("IUCN", iucn_raw or "—", delta=iucn_label_str, delta_color="off")

        st.markdown("<br>", unsafe_allow_html=True)

        # Report body — use st.markdown directly (not inside st.container)
        st.markdown(st.session_state.tip_report)

        # Coded language chips
        patterns = result.get("matched_patterns", [])
        if patterns:
            st.markdown("---")
            st.markdown("**⚠️ Coded language detected:**")
            cols = st.columns(min(len(patterns), 4))
            for i, p in enumerate(patterns):
                with cols[i % 4]:
                    st.code(p, language=None)

        st.markdown("---")
        col1, col2, col3 = st.columns(3)
        with col1:
            pdf_bytes = _tip_report_to_pdf(
                st.session_state.tip_report,
                report_id,
                result.get("species_common", ""),
                result.get("species_latin", ""),
                severity,
            )
            st.download_button(
                "⬇️ Download PDF",
                data=pdf_bytes,
                file_name=f"wildlifewatch-{report_id}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        with col2:
            st.link_button(
                "📨 Report to TRAFFIC",
                "https://www.traffic.org/about-us/contact/",
                use_container_width=True,
            )
        with col3:
            st.link_button(
                "📨 Report to NParks/AVS",
                "https://www.nparks.gov.sg/avs/animals/feedback-and-complaints",
                use_container_width=True,
            )

        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("← Analyse another listing"):
            st.session_state.tip_report = None
            st.session_state.analysis_result = None
            go_to(1)