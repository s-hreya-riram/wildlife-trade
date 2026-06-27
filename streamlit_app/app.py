"""
WildlifeWatch Trade Intel — Streamlit App
Three views:
  1. Threat Landscape  — most endangered species in SEA (live IUCN data)
  2. Analyse a Listing — submit listing/field sighting for AI analysis
  3. Tip Report        — structured report ready for enforcement submission
"""

import json
import os
from pathlib import Path

import httpx
import streamlit as st

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
    "Panthera tigris": "https://upload.wikimedia.org/wikipedia/commons/6/66/Adult_male_Royal_Bengal_tiger.jpg",
    "Loxodonta africana": "https://upload.wikimedia.org/wikipedia/commons/9/94/178_Male_African_bush_elephant_in_Etosha_National_Park_Photo_by_Giles_Laurent.jpg",
    # TODO add more species later!!!
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
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap');
  html, body, [class*="css"] { font-family: 'DM Sans', sans-serif; }

  section[data-testid="stSidebar"] { background-color: #0d1f0f; border-right: 1px solid #1a3d1e; }
  section[data-testid="stSidebar"] * { color: #c8e6c9 !important; }

  .stat-card { background: #0d1f0f; border: 1px solid #1a3d1e; border-radius: 8px; padding: 20px 24px; text-align: center; }
  .stat-number { font-family: 'DM Mono', monospace; font-size: 2.4rem; font-weight: 500; color: #4ade80; line-height: 1; }
  .stat-label { font-size: 0.78rem; color: #6b7280; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 6px; }

  .listing-card {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 10px;
    border-left: 4px solid #e5e7eb;
    cursor: pointer;
    transition: box-shadow 0.15s;
  }
  .listing-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .listing-title { font-weight: 600; font-size: 0.95rem; color: #111827; margin-bottom: 4px; }
  .listing-meta { font-size: 0.8rem; color: #6b7280; }
  .listing-species { font-style: italic; font-size: 0.82rem; color: #374151; margin-top: 4px; }

  .badge {
    font-family: 'DM Mono', monospace;
    font-size: 0.72rem;
    font-weight: 600;
    padding: 2px 9px;
    border-radius: 4px;
    display: inline-block;
    letter-spacing: 0.05em;
  }
  .badge-HIGH   { background: #fee2e2; color: #991b1b; }
  .badge-MEDIUM { background: #ffedd5; color: #9a3412; }
  .badge-LOW    { background: #dcfce7; color: #166534; }

  .species-card { background: #f8faf8; border: 1px solid #e2e8f0; border-left: 4px solid #16a34a; border-radius: 6px; padding: 14px 18px; margin-bottom: 8px; }
  .species-name { font-style: italic; font-size: 0.9rem; color: #1a202c; font-weight: 500; }

  .view-header { font-size: 1.5rem; font-weight: 600; color: #0d1f0f; margin-bottom: 4px; }
  .view-subheader { font-size: 0.88rem; color: #6b7280; margin-bottom: 24px; }

  .report-box { background: #f8faf8; border: 1px solid #d1fae5; border-radius: 8px; padding: 28px 32px; font-size: 0.88rem; line-height: 1.8; }

  .signal-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid #f3f4f6; font-size: 0.85rem; }
  .signal-label { color: #6b7280; width: 180px; flex-shrink: 0; }
  .signal-bar-wrap { flex: 1; background: #f3f4f6; border-radius: 4px; height: 8px; }
  .signal-bar { height: 8px; border-radius: 4px; background: #16a34a; }
  .signal-value { font-family: 'DM Mono', monospace; font-size: 0.78rem; color: #374151; width: 50px; text-align: right; }

  #MainMenu { visibility: hidden; }
  footer { visibility: hidden; }
  .stDeployButton { display: none; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────

for key, default in [
    ("tip_report", None),
    ("analysis_result", None),
    ("active_view", "🌏 Threat Landscape"),
    ("selected_listing", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ── Sidebar ───────────────────────────────────────────────────────────────────

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
            json={"title": title, "description": description,
                "platform": platform, "image_url": image_url or None,
                "image_b64": image_b64 or None},
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def severity_badge(severity: str) -> str:
    cls = f"badge badge-{severity}"
    return f'<span class="{cls}">{severity}</span>'


def bar(score: float, color: str = "#16a34a") -> str:
    pct = int(score * 100)
    return (
        f'<div class="signal-bar-wrap">'
        f'<div class="signal-bar" style="width:{pct}%; background:{color};"></div>'
        f'</div>'
    )

def _build_synthetic_report(listing: dict) -> str:
        """Build a markdown tip report from a synthetic listing for demo purposes."""
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
        trade_legal = "**All commercial trade is ILLEGAL**" if cites == "I" else "Trade requires CITES permits" if cites == "II" else "Trade not regulated under CITES"

        return f"""## Intelligence Summary
            A **{sev} severity** wildlife trade concern has been identified on {platform}. The listing \"{title}\" has been flagged based on species identification, conservation status, and linguistic analysis of the listing text.

            ## Species & Conservation Status
            - **Species Identified:** {species} (*{latin}*) — {confidence:.0%} confidence
            - **CITES Status:** Appendix {cites} — {trade_legal}
            - **IUCN Red List:** {iucn} ({iucn_label})

            ## Listing Analysis
            The listing was detected on **{platform}** and contains language consistent with known wildlife trafficking patterns. {f'The following coded terms were identified: **{", ".join(patterns)}**.' if patterns else 'No specific coded language was detected, but species identity and conservation status alone trigger a medium concern flag.'}

            ## Why This Is Suspicious
            {f'The combination of a CITES Appendix {cites}-listed species with ' + str(len(patterns)) + ' coded trafficking terms in the listing text represents a high-probability trade violation.' if patterns else f'This species ({species}) is {iucn_label} and subject to CITES restrictions. Even without explicit coded language, the listing warrants investigation to verify legal provenance.'}

            ## Recommended Action
            {'Immediate referral to AVS (Animal & Veterinary Service) and NParks CITES enforcement unit. CITES Appendix I species — any trade is presumptively illegal without exceptional documentation.' if cites == 'I' else 'Refer to AVS for permit verification. Request seller documentation of CITES export/import permits and chain of custody.'}"""

# ── View 1: Threat Landscape ──────────────────────────────────────────────────

if view == "🌏 Threat Landscape":
    # Hero banner
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

    # Two columns: left = IUCN live data, right = demo feed
    col_left, col_right = st.columns([1, 1], gap="large")

    with col_left:
        st.markdown('<div class="view-header">Most At Risk — Southeast Asia</div>', unsafe_allow_html=True)
        st.markdown('<div class="view-subheader">Live IUCN Red List data</div>', unsafe_allow_html=True)

        selected_name = st.selectbox(
            "Country",
            list(SEA_COUNTRIES.values()),
            index=0,
            label_visibility="collapsed",
        )
        country_code = {v: k for k, v in SEA_COUNTRIES.items()}[selected_name]

        with st.spinner(f"Loading data for {selected_name}…"):
            species_list = fetch_sea_endangered(country_code)

        if not species_list:
            st.info("Awaiting live data — species endpoint loading. Check API connection.")
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
                  <span style="font-size:0.75rem; color:{color}; font-weight:600;">
                    {status_code} · {label}
                  </span>
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
                "Filter",
                ["All", "HIGH", "MEDIUM", "LOW"],
                default="All",
                label_visibility="collapsed",
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

                if st.button(f"View report →", key=f"btn_{listing['id']}"):
                    # Pre-populate the tip report view with this synthetic listing
                    st.session_state.tip_report = _build_synthetic_report(listing)
                    st.session_state.analysis_result = listing
                    st.session_state.active_view = "📋 Tip Report"
                    st.rerun()


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
        uploaded = st.file_uploader("OPtional - Upload listing image", type=["jpg","jpeg","png"])
        image_b64 = None
        if uploaded:
            import base64
            image_b64 = base64.b64encode(uploaded.read()).decode()
            st.image(uploaded, caption="Uploaded image", width=300)
        submitted = st.form_submit_button("🔍 Run Analysis", use_container_width=True)

    if submitted:
        if not title and not description:
            st.warning("Please enter at least a title or description to analyse.")
        else:
            with st.status("Running trade intelligence analysis…", expanded=True) as status_box:
                st.write("🔍 **Step 1/5** — Classifying species and material from listing text" +
                         (" + image" if image_url else "") + "…")
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
                    st.write(f"📋 **Step 2** — CITES: **Appendix {cites}**" +
                             (" ⛔ Trade ILLEGAL" if cites_illegal else ""))

                    patterns = result.get("matched_patterns", [])
                    if patterns:
                        st.write(f"⚠️ **Step 3** — Coded language: `{'`, `'.join(patterns)}`")
                    else:
                        st.write("✅ **Step 3** — No coded trafficking language detected")

                    iucn_raw = result.get("iucn_status", "")
                    iucn_label_str = IUCN_LABELS.get(iucn_raw, ("Unknown", ""))[0]
                    st.write(f"🌿 **Step 4** — IUCN status: **{iucn_raw}** ({iucn_label_str})")

                    sev = result.get("severity", "LOW")
                    sev_reason = result.get("severity_reason", "")
                    emoji, color, _ = SEVERITY_META.get(sev, ("🟢", "#166534", "#dcfce7"))
                    st.write(f"**Step 5** — Severity: {emoji} **{sev}** — {sev_reason}")

                    status_box.update(label="Analysis complete ✅", state="complete")
                    st.session_state.analysis_result = result
                    st.session_state.tip_report = result.get("report_markdown")

            if st.session_state.tip_report:
                st.success("Tip report generated.")
                if st.button("📋 View Tip Report →", use_container_width=True):
                    st.session_state.active_view = "📋 Tip Report"
                    st.rerun()
            elif st.session_state.analysis_result and "error" not in st.session_state.analysis_result:
                # Show signal breakdown even if report generation failed
                result = st.session_state.analysis_result
                breakdown = result.get("signal_breakdown", {})
                if breakdown:
                    st.markdown("**Signal breakdown:**")
                    for label, key, col in [
                        ("CITES status", "cites_contribution", "#e53e3e"),
                        ("IUCN category", "iucn_contribution", "#dd6b20"),
                        ("Classifier confidence", "classifier_contribution", "#3b82f6"),
                        ("Language flags", "language_contribution", "#8b5cf6"),
                    ]:
                        val = breakdown.get(key, 0)
                        st.markdown(
                            f'<div class="signal-row">'
                            f'<span class="signal-label">{label}</span>'
                            f'{bar(val / 0.4, col)}'
                            f'<span class="signal-value">{val:.2f}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

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
            st.session_state.active_view = "🔍 Analyse a Listing"
            st.rerun()
    else:
        result = st.session_state.analysis_result or {}

        latin = result.get("species_latin", "")
        img_url = SPECIES_IMAGES.get(latin)
        if img_url:
            st.image(img_url, width=280, caption=f"{result.get('species_common')} · {latin}")
        severity = result.get("severity", "")
        report_id = result.get("report_id") or result.get("id") or "DEMO"

        emoji, color, bg = SEVERITY_META.get(severity, ("", "#718096", "#f9fafb"))
        if severity:
            st.markdown(f"""
            <div style="background:{bg}; border:1px solid {color}44;
                        border-radius:8px; padding:14px 20px; margin-bottom:20px;">
              <div style="font-weight:600; color:{color}; font-size:1rem;">
                {emoji} {severity} SEVERITY · {report_id}
              </div>
              <div style="font-size:0.84rem; color:#4b5563; margin-top:4px;">
                {result.get('severity_reason', '')}
              </div>
            </div>
            """, unsafe_allow_html=True)

        # Species + CITES summary strip
        species_c = result.get("species_common") or result.get("species_common")
        cites = result.get("cites_appendix")
        iucn_raw = result.get("iucn_status", "")
        if species_c or cites:
            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("Species", species_c or "—")
            with c2:
                st.metric("CITES Appendix", cites or "Not listed")
            with c3:
                iucn_label_str = IUCN_LABELS.get(iucn_raw, ("Unknown", ""))[0]
                st.metric("IUCN Status", f"{iucn_raw} – {iucn_label_str}" if iucn_raw else "—")

        st.markdown("<br>", unsafe_allow_html=True)
        with st.container(border=True):
            st.markdown(st.session_state.tip_report)

        # Coded language callout
        patterns = result.get("matched_patterns", [])
        if patterns:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("**⚠️ Coded language detected:**")
            cols = st.columns(min(len(patterns), 4))
            for i, p in enumerate(patterns):
                with cols[i % 4]:
                    st.code(p, language=None)

        st.markdown("<br>", unsafe_allow_html=True)
        col1, col2, col3 = st.columns(3)
        with col1:
            st.download_button(
                "⬇️ Download report (.md)",
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