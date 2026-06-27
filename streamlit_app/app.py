"""
WildlifeWatch Trade Intel — Streamlit App
Three views:
  1. Threat Landscape  — most endangered species in SEA (live IUCN data)
  2. Analyse a Listing — submit listing/field sighting for AI analysis
  3. Tip Report        — structured report ready for enforcement submission
"""

import os
import httpx
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────

API_URL = os.getenv("API_URL", "http://localhost:8000")
if not API_URL.startswith("http"):
    API_URL = f"https://{API_URL}"

# SEA country codes for the threat landscape view
SEA_COUNTRIES = {
    "ID": "Indonesia",
    "MY": "Malaysia",
    "TH": "Thailand",
    "VN": "Vietnam",
    "PH": "Philippines",
    "SG": "Singapore",
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
  /* Base */
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap');

  html, body, [class*="css"] {
    font-family: 'DM Sans', sans-serif;
  }

  /* Sidebar */
  section[data-testid="stSidebar"] {
    background-color: #0d1f0f;
    border-right: 1px solid #1a3d1e;
  }
  section[data-testid="stSidebar"] * {
    color: #c8e6c9 !important;
  }

  /* Stat cards */
  .stat-card {
    background: #0d1f0f;
    border: 1px solid #1a3d1e;
    border-radius: 8px;
    padding: 20px 24px;
    text-align: center;
  }
  .stat-number {
    font-family: 'DM Mono', monospace;
    font-size: 2.4rem;
    font-weight: 500;
    color: #4ade80;
    line-height: 1;
  }
  .stat-label {
    font-size: 0.78rem;
    color: #6b7280;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 6px;
  }

  /* Species cards */
  .species-card {
    background: #f8faf8;
    border: 1px solid #e2e8f0;
    border-left: 4px solid #16a34a;
    border-radius: 6px;
    padding: 14px 18px;
    margin-bottom: 10px;
  }
  .species-name {
    font-style: italic;
    font-size: 0.95rem;
    color: #1a202c;
    font-weight: 500;
  }
  .species-status {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    font-weight: 500;
    padding: 2px 8px;
    border-radius: 4px;
    display: inline-block;
    margin-top: 4px;
  }

  /* Severity badges */
  .badge-high   { background: #fee2e2; color: #991b1b; }
  .badge-medium { background: #ffedd5; color: #9a3412; }
  .badge-low    { background: #dcfce7; color: #166534; }

  /* Section headers */
  .view-header {
    font-size: 1.6rem;
    font-weight: 600;
    color: #0d1f0f;
    margin-bottom: 4px;
  }
  .view-subheader {
    font-size: 0.9rem;
    color: #6b7280;
    margin-bottom: 28px;
  }

  /* Tool trace */
  .tool-step {
    font-family: 'DM Mono', monospace;
    font-size: 0.82rem;
    color: #374151;
    padding: 6px 0;
    border-bottom: 1px dashed #e5e7eb;
  }

  /* Report */
  .report-box {
    background: #f8faf8;
    border: 1px solid #d1fae5;
    border-radius: 8px;
    padding: 28px 32px;
    font-size: 0.9rem;
    line-height: 1.8;
  }
  .report-id {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    color: #6b7280;
    margin-bottom: 16px;
  }

  /* Hide Streamlit chrome */
  #MainMenu { visibility: hidden; }
  footer { visibility: hidden; }
  .stDeployButton { display: none; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────

if "tip_report" not in st.session_state:
    st.session_state.tip_report = None
if "analysis_result" not in st.session_state:
    st.session_state.analysis_result = None
if "active_view" not in st.session_state:
    st.session_state.active_view = "🌏 Threat Landscape"

# ── Sidebar nav ───────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("### 🦅 WildlifeWatch")
    st.markdown("**Trade Intelligence Platform**")
    st.markdown("---")

    view = st.radio(
        "Navigate",
        ["🌏 Threat Landscape", "🔍 Analyse a Listing", "📋 Tip Report"],
        index=["🌏 Threat Landscape", "🔍 Analyse a Listing", "📋 Tip Report"].index(
            st.session_state.active_view
        ),
        label_visibility="collapsed",
    )
    st.session_state.active_view = view

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
    """Fetch threatened species for a SEA country via the FastAPI."""
    try:
        resp = httpx.get(
            f"{API_URL}/api/species/country/{country_code}",
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json().get("species", [])
    except Exception:
        pass
    return []


def call_analyse_listing(
    title: str,
    description: str,
    platform: str,
    image_url: str | None,
) -> dict:
    """Call the trade analysis endpoint."""
    try:
        resp = httpx.post(
            f"{API_URL}/api/trade/analyse",
            json={
                "title": title,
                "description": description,
                "platform": platform,
                "image_url": image_url or None,
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}

# ── View 1: Threat Landscape ──────────────────────────────────────────────────

if view == "🌏 Threat Landscape":
    # Hero stat — sets the stakes immediately
    st.markdown("""
    <div style="background:#0d1f0f; border-radius:10px; padding:32px 40px; margin-bottom:32px;">
      <div style="font-family:'DM Mono',monospace; font-size:0.75rem; color:#4ade80;
                  letter-spacing:0.12em; text-transform:uppercase; margin-bottom:8px;">
        Wildlife trafficking
      </div>
      <div style="font-size:2.8rem; font-weight:600; color:#ffffff; line-height:1.1;">
        World's 4th largest criminal enterprise
      </div>
      <div style="font-size:1rem; color:#6b7280; margin-top:10px;">
        Worth an estimated <span style="color:#4ade80; font-weight:500;">$23 billion annually</span>
        — and most of it has moved online.
      </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="view-header">Threat Landscape — Southeast Asia</div>', unsafe_allow_html=True)
    st.markdown('<div class="view-subheader">Live IUCN Red List data · Critically Endangered, Endangered & Vulnerable species</div>', unsafe_allow_html=True)

    # Country selector
    selected_name = st.selectbox(
        "Country",
        list(SEA_COUNTRIES.values()),
        index=5,  # Singapore default
        label_visibility="collapsed",
    )
    country_code = {v: k for k, v in SEA_COUNTRIES.items()}[selected_name]

    with st.spinner(f"Loading threatened species in {selected_name}…"):
        species_list = fetch_sea_endangered(country_code)

    if not species_list:
        st.info(
            f"No data returned for {selected_name} yet. "
            "The species endpoint will be wired up shortly — "
            "check back once the API is deployed."
        )
    else:
        # Summary counts
        cr = sum(1 for s in species_list if s.get("status") == "CR")
        en = sum(1 for s in species_list if s.get("status") == "EN")
        vu = sum(1 for s in species_list if s.get("status") == "VU")

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f'<div class="stat-card"><div class="stat-number">{len(species_list)}</div><div class="stat-label">Threatened species</div></div>', unsafe_allow_html=True)
        with c2:
            st.markdown(f'<div class="stat-card"><div class="stat-number" style="color:#e53e3e">{cr}</div><div class="stat-label">Critically Endangered</div></div>', unsafe_allow_html=True)
        with c3:
            st.markdown(f'<div class="stat-card"><div class="stat-number" style="color:#dd6b20">{en}</div><div class="stat-label">Endangered</div></div>', unsafe_allow_html=True)
        with c4:
            st.markdown(f'<div class="stat-card"><div class="stat-number" style="color:#d69e2e">{vu}</div><div class="stat-label">Vulnerable</div></div>', unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # Filter to CR only by default, let user expand
        show_all = st.toggle("Show all threatened (EN + VU)", value=False)
        filtered = species_list if show_all else [s for s in species_list if s.get("status") == "CR"]

        for s in filtered[:40]:
            status_code = s.get("status", "DD")
            label, color = IUCN_LABELS.get(status_code, ("Unknown", "#718096"))
            st.markdown(f"""
            <div class="species-card" style="border-left-color:{color}">
              <div class="species-name">{s.get('name', 'Unknown')}</div>
              <span class="species-status" style="background:{color}22; color:{color}">
                {status_code} · {label}
              </span>
            </div>
            """, unsafe_allow_html=True)

# ── View 2: Analyse a Listing ─────────────────────────────────────────────────

elif view == "🔍 Analyse a Listing":
    st.markdown('<div class="view-header">Analyse a Listing</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="view-subheader">'
        'Submit a suspicious marketplace listing or field sighting. '
        'The agent will identify the species, check CITES and IUCN status, '
        'and flag coded trafficking language.'
        '</div>',
        unsafe_allow_html=True,
    )

    with st.form("listing_form"):
        platform = st.selectbox(
            "Platform",
            ["Carousell", "Facebook Marketplace", "Shopee", "Instagram DM", "Field sighting (no URL)", "Other"],
        )
        title = st.text_input(
            "Listing title or item description",
            placeholder="e.g. Rare exotic skin wallet, very special material",
        )
        description = st.text_area(
            "Full listing description",
            placeholder="Paste the full listing text here…",
            height=140,
        )
        image_url = st.text_input(
            "Image URL (optional)",
            placeholder="https://…",
        )
        submitted = st.form_submit_button("🔍 Analyse", use_container_width=True)

    if submitted:
        if not title and not description:
            st.warning("Please enter at least a title or description.")
        else:
            with st.status("Running trade intelligence analysis…", expanded=True) as status:
                st.write("🔍 Classifying species and material from listing…")
                result = call_analyse_listing(title, description, platform, image_url)

                if "error" in result:
                    status.update(label="Analysis failed", state="error")
                    st.error(
                        f"Could not reach the trade analysis API: {result['error']}. "
                        "Make sure the trade MCP server is running."
                    )
                else:
                    st.write(f"✅ Species identified: **{result.get('species_common', 'Unknown')}** "
                             f"(*{result.get('species_latin', '')}*) — "
                             f"{result.get('confidence', 0):.0%} confidence")
                    st.write(f"📋 CITES status: **Appendix {result.get('cites_appendix', 'Unknown')}**")
                    st.write(f"🔴 IUCN status: **{result.get('iucn_label', 'Unknown')}**")

                    if result.get("matched_patterns"):
                        st.write(f"⚠️ Coded language detected: `{'`, `'.join(result['matched_patterns'])}`")
                    else:
                        st.write("✅ No coded trafficking language detected")

                    severity = result.get("severity", "LOW")
                    severity_color = result.get("severity_color", "#38a169")
                    st.write(f"**Severity: :{severity.lower()}[{severity}]** — {result.get('severity_reason', '')}")

                    status.update(label="Analysis complete", state="complete")
                    st.session_state.analysis_result = result
                    st.session_state.tip_report = result.get("report_markdown")

            if st.session_state.tip_report:
                st.success("Tip report ready.")
                if st.button("📋 View Tip Report →", use_container_width=True):
                    st.session_state.active_view = "📋 Tip Report"
                    st.rerun()

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
            "Go to **Analyse a Listing** and submit a suspicious listing first."
        )
        if st.button("← Analyse a listing"):
            st.session_state.active_view = "🔍 Analyse a Listing"
            st.rerun()
    else:
        result = st.session_state.analysis_result or {}
        severity = result.get("severity", "")
        report_id = result.get("report_id", "—")

        # Severity banner
        color_map = {"HIGH": "#e53e3e", "MEDIUM": "#dd6b20", "LOW": "#38a169"}
        banner_color = color_map.get(severity, "#718096")
        if severity:
            st.markdown(f"""
            <div style="background:{banner_color}18; border:1px solid {banner_color}44;
                        border-radius:8px; padding:14px 20px; margin-bottom:20px;
                        display:flex; align-items:center; gap:12px;">
              <span style="font-size:1.4rem;">{"🔴" if severity=="HIGH" else "🟠" if severity=="MEDIUM" else "🟢"}</span>
              <div>
                <div style="font-weight:600; color:{banner_color}; font-size:1rem;">
                  {severity} SEVERITY · {report_id}
                </div>
                <div style="font-size:0.85rem; color:#4b5563; margin-top:2px;">
                  {result.get('severity_reason', '')}
                </div>
              </div>
            </div>
            """, unsafe_allow_html=True)

        # Report body
        st.markdown(
            f'<div class="report-box">{st.session_state.tip_report}</div>',
            unsafe_allow_html=True,
        )

        st.markdown("<br>", unsafe_allow_html=True)

        # Actions
        col1, col2, col3 = st.columns(3)
        with col1:
            st.download_button(
                "⬇️ Download report",
                data=st.session_state.tip_report,
                file_name=f"wildlifewatch-{report_id}.md",
                mime="text/markdown",
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

        st.markdown("---")
        if st.button("← Analyse another listing"):
            st.session_state.tip_report = None
            st.session_state.analysis_result = None
            st.session_state.active_view = "🔍 Analyse a Listing"
            st.rerun()