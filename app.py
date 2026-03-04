import os
import time
import streamlit as st
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from datetime import datetime, timedelta
import math
from urllib.parse import urljoin, quote
try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except ImportError:
    BERLIN = None

# Force a dark Plotly theme with explicit colors
pio.templates.default = "plotly_dark"

# --- SYSTEM CONFIG (Varel: 53.396°N, 8.136°E) ---
LAT, LON = 53.396, 8.136
LAT_RAD = math.radians(LAT)


def _to_float(val) -> float:
    """Coerce value to float; return 0.0 on failure."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


# --- Home Assistant API (primary data source) ---
def _ha_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def ha_get_state(base_url: str, token: str, entity_id: str):
    """GET /api/states/<entity_id>. Returns dict with state, attributes, or None on error."""
    url = urljoin(base_url.rstrip("/") + "/", f"api/states/{entity_id}")
    try:
        r = requests.get(url, headers=_ha_headers(token), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def ha_get_history(base_url: str, token: str, entity_id: str, start_iso: str, end_iso: str):
    """GET /api/history/period with filter_entity_id. Returns list of state-change lists or []."""
    base = base_url.rstrip("/") + "/"
    url = urljoin(base, "api/history/period/" + quote(start_iso, safe=""))
    try:
        r = requests.get(
            url,
            headers=_ha_headers(token),
            params={"filter_entity_id": entity_id, "end_time": end_iso, "minimal_response": "1"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def ha_live_power_kw(state: dict) -> float:
    """Extract current power in kW from HA state (state may be in W or kW)."""
    if not state or not isinstance(state, dict):
        return 0.0
    raw = state.get("state")
    if raw in (None, "", "unknown", "unavailable"):
        return 0.0
    try:
        val = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0
    unit = (state.get("attributes") or {}).get("unit_of_measurement", "")
    if "kW" in unit or "kWh" in unit:
        return max(0.0, val)
    return max(0.0, val / 1000.0)


def ha_today_kwh_from_state(state: dict) -> float | None:
    """Extract today's energy (kWh) from state attributes if present."""
    if not state or not isinstance(state, dict):
        return None
    attrs = state.get("attributes") or {}
    for key in ("today", "today_energy", "energy_today", "daily_energy", "state_class"):
        v = attrs.get(key)
        if v is not None and isinstance(v, (int, float)):
            return float(v)
    return None


def ha_total_kwh_from_state(state: dict) -> float | None:
    """Extract total/lifetime energy (kWh) from state attributes if present."""
    if not state or not isinstance(state, dict):
        return None
    attrs = state.get("attributes") or {}
    for key in ("total", "total_increasing", "lifetime", "total_energy", "energy_total"):
        v = attrs.get(key)
        if v is not None and isinstance(v, (int, float)):
            return float(v)
    raw = state.get("state")
    if raw not in (None, "", "unknown", "unavailable"):
        try:
            return float(str(raw).replace(",", "."))
        except (TypeError, ValueError):
            pass
    return None


def _history_to_5min_kwh(history: list, date_str: str) -> list:
    """
    Convert HA history (list of state-change lists) for a power sensor into
    5-min kWh buckets for the given date. Power is in W; each bucket = avg power * (5/60) kWh.
    """
    from datetime import date as date_type
    parts = date_str.split("-")
    if len(parts) != 3:
        return []
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    day_start = datetime(y, m, d, 0, 0, 0)
    day_end = day_start + timedelta(days=1)
    n_slots = int((day_end - day_start).total_seconds() / (5 * 60))
    slots = [0.0] * n_slots

    def parse_ts(s: str):
        if not s:
            return None
        try:
            if "T" in s:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(s)
            return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt
        except Exception:
            return None

    # Flatten: history is list of lists (one per entity), each list = chronologically ordered state changes.
    events = []
    for chunk in (history or []):
        if not isinstance(chunk, list):
            continue
        for ev in chunk:
            if not isinstance(ev, dict):
                continue
            ts = parse_ts(ev.get("last_updated") or ev.get("last_changed"))
            if ts is None:
                continue
            try:
                p = float(str(ev.get("state", 0)).replace(",", "."))
            except (TypeError, ValueError):
                p = 0.0
            if ev.get("state") in ("unknown", "unavailable", ""):
                p = 0.0
            events.append((ts, p))

    events.sort(key=lambda x: x[0])
    if not events:
        return slots

    # Assign power (W) to 5-min slots: use last known power in each slot.
    slot_duration = timedelta(minutes=5)
    for i in range(n_slots):
        t0 = day_start + i * slot_duration
        t1 = t0 + slot_duration
        # Use last event before t1 that is >= t0
        p_w = 0.0
        for ts, p in events:
            if ts < t0:
                continue
            if ts < t1:
                p_w = p
            else:
                break
        # p_w is in W (HA power sensors usually W). Convert to kWh for 5 min.
        slots[i] = max(0.0, p_w / 1000.0 * (5.0 / 60.0))
    return slots


# --- JAM54D41-430/LB Bifacial (hardcoded specs) ---
PMAX_W = 430                    # Front Pmax
REAR_PMAX_W = 464               # Rear Pmax / Bifacial Max (10% irradiation ratio)
BIFACIALITY = 0.80               # Bifaciality factor
TEMP_COEFF_PCT = -0.300          # Temperature coefficient (Pmax), %/°C
VMP, VOC = 32.11, 38.50         # Electricals: Vmp | Voc (low-light wake-up)
ALBEDO_SPECULAR = 0.38          # Specular albedo (water-surface → rear glass, 60° SW array)
N_MODULES = 20
SYSTEM_KWP = (N_MODULES * PMAX_W) / 1000.0  # 8.6 kWp
TILT_DEG = 60.0
AZIMUTH_PANEL = 225.0            # SW

# --- High Contrast Sunlight Theme ---
PREDICTED_COLOR = "#39FF14"  # Electric Green (real-time & predicted)
ACTUAL_COLOR = "#FFFF00"    # Neon Yellow

st.set_page_config(page_title="Varel Solar Truth", layout="wide", page_icon="⚓")

# Session state: Home Assistant (primary) and physics baseline
if "ha_online" not in st.session_state:
    st.session_state["ha_online"] = False

# Auto-refresh every 10 minutes to avoid hitting Growatt rate limits
st_autorefresh_ms = 10 * 60 * 1000
try:
    # Only import if available; avoid hard dependency breakage
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=st_autorefresh_ms, key="abamu_autorefresh")
except Exception:
    # If the helper isn't installed, skip auto-refresh gracefully
    pass

# --- 1. REAL-TIME SOLAR GEOMETRY (no hourly API) ---
def _solar_elevation_azimuth(dt: datetime) -> tuple[float, float]:
    """Sun elevation (deg) and azimuth (deg from N, 0=N, 90=E, 180=S) for LAT, LON. Naive dt = Berlin local."""
    # Use dt as local (Berlin); day of year and hour from dt
    n = dt.timetuple().tm_yday
    hour_dec = dt.hour + dt.minute / 60.0 + getattr(dt, "second", 0) / 3600.0
    # Solar declination (Cooper)
    dec_deg = 23.45 * math.sin(math.radians(360.0 * (284 + n) / 365.0))
    dec_rad = math.radians(dec_deg)
    # Hour angle: 15 deg per hour from solar noon (~12.25 for Berlin)
    hour_angle_deg = 15.0 * (12.25 - hour_dec)
    hour_angle_rad = math.radians(hour_angle_deg)
    # Elevation
    sin_elev = math.sin(LAT_RAD) * math.sin(dec_rad) + math.cos(LAT_RAD) * math.cos(dec_rad) * math.cos(hour_angle_rad)
    sin_elev = max(-1.0, min(1.0, sin_elev))
    elev_deg = math.degrees(math.asin(sin_elev))
    # Azimuth (from N, 0..360)
    cos_az = (math.sin(dec_rad) - math.sin(LAT_RAD) * sin_elev) / (math.cos(LAT_RAD) * math.cos(math.asin(sin_elev)) if sin_elev < 1.0 else 1e-6)
    cos_az = max(-1.0, min(1.0, cos_az))
    azim_deg = math.degrees(math.acos(cos_az))
    if hour_angle_deg > 0:
        azim_deg = 360.0 - azim_deg
    return elev_deg, azim_deg


def _low_irradiance_factor(poa_wm2: float) -> float:
    """JAM54D41-430/LB 'Better low irradiance response': relative gain at low G so prediction matches cloudy/dawn data."""
    if poa_wm2 <= 0:
        return 1.0
    G_std = 1000.0
    if poa_wm2 >= G_std:
        return 1.0
    # Datasheet-style: at low G, module delivers slightly more than linear (e.g. ~1.05–1.08 at 200 W/m²)
    return 1.0 + 0.08 * (1.0 - poa_wm2 / G_std)


def _poa_kw_from_geometry(elev_deg: float, azim_deg: float, cloud_cover: float = 0.0) -> tuple[float, float]:
    """
    Returns (total_kw, rear_kw). Front 430W, Rear 464W (bifacial 0.80), albedo 0.38, 60° SW.
    When cloud_cover is high (0–1), prioritize diffuse radiation; apply low-irradiance response curve.
    """
    if elev_deg <= 0:
        return 0.0, 0.0
    tilt_rad = math.radians(TILT_DEG)
    ground_view = (1.0 - math.cos(tilt_rad)) / 2.0
    am = 1.0 / max(0.01, math.sin(math.radians(elev_deg)))
    ghi_clear = 1361.0 * (0.7 ** min(am, 5.0)) * math.sin(math.radians(elev_deg))
    sin_elev = max(0.01, math.sin(math.radians(elev_deg)))
    dni_clear = ghi_clear / sin_elev if sin_elev > 0.01 else 0.0
    # Cloud: reduce beam, prioritize diffuse (high cloud → more diffuse fraction)
    cloud = max(0.0, min(1.0, cloud_cover))
    clearness = 1.0 - cloud
    ghi = ghi_clear * (0.25 + 0.75 * clearness)
    dni = dni_clear * clearness
    diffuse_ghi = max(0.0, ghi - dni * sin_elev)
    inc_rad = math.acos(
        math.cos(math.radians(elev_deg)) * math.cos(tilt_rad)
        + math.sin(math.radians(elev_deg)) * math.sin(tilt_rad) * math.cos(math.radians(azim_deg - AZIMUTH_PANEL))
    )
    cos_inc = max(0.0, math.cos(inc_rad))
    poa_front_beam = dni * cos_inc
    # Diffuse on tilt (isotropic); when cloudy, diffuse_ghi dominates so we prioritize diffuse
    poa_front_diffuse = diffuse_ghi * (1.0 + math.cos(tilt_rad)) / 2.0 if ghi > 0 else 0.0
    poa_front_ground = ALBEDO_SPECULAR * ghi * ground_view
    poa_front = poa_front_beam + poa_front_diffuse + poa_front_ground
    poa_rear = ALBEDO_SPECULAR * ghi * ground_view
    # JAM54D41-430/LB better low irradiance response: scale effective POA (W/m²)
    poa_front *= _low_irradiance_factor(poa_front)
    poa_rear *= _low_irradiance_factor(poa_rear)
    kw_front = (poa_front / 1000.0) * (N_MODULES * PMAX_W) / 1000.0
    kw_rear = (poa_rear / 1000.0) * (N_MODULES * REAR_PMAX_W) / 1000.0 * BIFACIALITY
    return kw_front + kw_rear, kw_rear


def real_time_expected_kw(cloud_cover: float = 0.0) -> float:
    """Expected kW at current minute: solar geometry, 430W/464W bifacial, albedo 0.38; optional cloud for diffuse priority."""
    now = datetime.now(BERLIN) if BERLIN else datetime.now()
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    elev, azim = _solar_elevation_azimuth(now_naive)
    total, _ = _poa_kw_from_geometry(elev, azim, cloud_cover)
    return max(0.0, total)


def real_time_bifacial_gain_pct(cloud_cover: float = 0.0) -> float:
    """Rear contribution as % of total (JAM54D41-430/LB, 80% bifacial)."""
    now = datetime.now(BERLIN) if BERLIN else datetime.now()
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    elev, azim = _solar_elevation_azimuth(now_naive)
    total, rear = _poa_kw_from_geometry(elev, azim, cloud_cover)
    return (rear / total * 100) if total > 0 else 0.0


def get_varel_prediction(date_str: str, cloud_cover: float = 0.0) -> pd.DataFrame:
    """
    Physics-only prediction. Varel 53.396, 8.136; JAM54D41-430/LB, 430W/464W bifacial 0.80, albedo 0.38.
    When cloud_cover > 0, prioritizes diffuse radiation and applies better low-irradiance response.
    """
    times = pd.date_range(
        start=f"{date_str} 00:00",
        end=f"{date_str} 23:55",
        freq="5min",
    )
    preds: list[float] = []
    clouds: list[float] = []
    for t in times:
        dt = t.to_pydatetime()
        elev, azim = _solar_elevation_azimuth(dt)
        total_kw, _ = _poa_kw_from_geometry(elev, azim, cloud_cover)
        kwh_5min = max(0.0, total_kw * (5.0 / 60.0))
        preds.append(kwh_5min)
        clouds.append(cloud_cover * 100.0)
    return pd.DataFrame({"Time": times, "Predicted": preds, "Cloud_Cover": clouds})

# --- 2. THE DASHBOARD (Professional Mobile Command Center) ---
# Dark theme: centered header ABAMU RESIDENCE in silver
st.markdown(
    """
    <style>
    header[data-testid="stHeader"] { background: #121212; }
    .abamu-header {
        text-align: center;
        color: #C0C0C0;
        font-weight: 700;
        letter-spacing: 0.15em;
        font-size: 1.4rem;
        margin: 0.5rem 0;
        background: #121212;
        padding: 0.5rem 0;
    }
    </style>
    <p class="abamu-header">ABAMU RESIDENCE</p>
    """,
    unsafe_allow_html=True,
)
# --- Sidebar: Home Assistant connection (primary data source) ---
INVERTER_ENTITY_OPTIONS = [
    "sensor.growatt_actual_power",
    "sensor.inverter_power",
    "sensor.solar_power",
    "sensor.pv_power",
    "Custom...",
]
with st.sidebar:
    st.subheader("Home Assistant")
    ha_url = st.text_input(
        "HA URL",
        value=st.session_state.get("ha_url", "http://YOUR_HA_IP:8123"),
        key="ha_url",
        placeholder="http://YOUR_HA_IP:8123",
        help="Home Assistant instance URL.",
    )
    ha_token = st.text_input(
        "HA Token",
        type="password",
        value=st.session_state.get("ha_token", ""),
        key="ha_token",
        help="Long-Lived Access Token (Profile → Long-Lived Access Tokens).",
    )
    entity_choice = st.selectbox(
        "Inverter Entity ID",
        options=INVERTER_ENTITY_OPTIONS,
        index=0,
        key="ha_entity_choice",
        help="Power sensor for live data and history (e.g. sensor.growatt_actual_power).",
    )
    if entity_choice == "Custom...":
        ha_power_entity = st.text_input(
            "Custom entity ID",
            value=st.session_state.get("ha_power_entity", "sensor.inverter_power"),
            key="ha_power_entity",
            placeholder="sensor.my_inverter_power",
        )
    else:
        ha_power_entity = entity_choice

selected_date = st.sidebar.date_input("Analysis Date", datetime.now().date())
cloud_cover_pct = st.sidebar.slider("Cloud cover (%)", 0, 100, 0, help="Prioritize diffuse radiation when high; improves cloudy/low-irradiance match.")
cloud_cover = float(cloud_cover_pct) / 100.0
snow_override = st.sidebar.toggle("Snow Override", value=False, help="Drop prediction to 0; flag February as Snow-Locked")
d_str = selected_date.strftime("%Y-%m-%d")

def _safe_numeric(val) -> float:
    """Single value: pd.to_numeric(val, errors='coerce').fillna(0) as float."""
    if val is None:
        return 0.0
    s = pd.Series([val])
    return float(pd.to_numeric(s, errors="coerce").fillna(0).iloc[0])

# --- Fetch from Home Assistant: Live Power, Today's Energy, Total; Hour from history/period ---
ha_url = (st.session_state.get("ha_url") or "").strip()
ha_token = (st.session_state.get("ha_token") or "").strip()
_choice = st.session_state.get("ha_entity_choice", INVERTER_ENTITY_OPTIONS[0])
ha_entity = (_choice if _choice != "Custom..." else (st.session_state.get("ha_power_entity") or "sensor.growatt_actual_power")).strip()
ha_configured = bool(ha_url and ha_token and ha_entity and "YOUR_HA_IP" not in ha_url.upper())

actuals = []
live_kw_ha = None
today_kwh_ha = None
total_kwh_ha = None
sync_msg = "Configure HA above" if not ha_configured else "Syncing..."
last_sync = "N/A"
data_fetching = False

if ha_configured:
    data_fetching = True
    try:
        state = ha_get_state(ha_url, ha_token, ha_entity)
        if state:
            live_kw_ha = ha_live_power_kw(state)
            today_kwh_ha = ha_today_kwh_from_state(state)
            total_kwh_ha = ha_total_kwh_from_state(state)
            st.session_state["ha_online"] = True
            sync_msg = "🟢 Online"
            last_sync = datetime.now().strftime("%H:%M:%S")
        else:
            st.session_state["ha_online"] = False
            sync_msg = "HA unreachable"
        # Hour tab: history for selected date → 5-min kWh buckets
        start_iso = f"{d_str}T00:00:00"
        end_iso = f"{d_str}T23:59:59"
        history = ha_get_history(ha_url, ha_token, ha_entity, start_iso, end_iso)
        actuals = _history_to_5min_kwh(history, d_str)
        if not actuals:
            actuals = []
        if today_kwh_ha is None and actuals:
            today_kwh_ha = sum(actuals)
    except Exception:
        st.session_state["ha_online"] = False
        sync_msg = "Connection failed"
    data_fetching = False

if not ha_configured or not st.session_state.get("ha_online"):
    st.warning("**Connection to HA failed. Check URL/Token.** 430Wp Physics Prediction remains visible so you can still see solar potential.")

# Physics baseline: always compute prediction (430Wp / 80% bifacial)
df = get_varel_prediction(d_str, cloud_cover)
df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)
total_pre = float(df["Predicted"].sum())
realtime_kw = real_time_expected_kw(cloud_cover)

# --- 3. DISPLAY ---
with st.sidebar:
    st.subheader("System Status")
    st.write(f"HA Status: {sync_msg}")
    st.write(f"Last Sync: {last_sync}")
    if total_kwh_ha is not None and total_kwh_ha > 0:
        st.caption(f"Total (lifetime): {total_kwh_ha:.1f} kWh")
    show_clouds = st.toggle("Enable Cloud Overlay", value=True)

# Snow Override: drop prediction to 0 when enabled
if snow_override:
    df["Predicted"] = 0.0
    df["Cloud_Cover"] = 0.0  # clear cloud overlay when snow-locked

# Align df with actuals (HA history → Neon Yellow bars)
if actuals:
    n = min(len(actuals), len(df))
    df = df.iloc[:n].copy()
    df["Actual"] = pd.Series(pd.to_numeric(actuals[:n], errors="coerce")).fillna(0).values
    df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)
    df["Diff"] = df["Actual"].astype(float) - df["Predicted"].astype(float)
else:
    df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)

# Physics baseline: when HA has no/small actuals, keep full 430Wp prediction (no 0.15 shrink)
total_pre = float(df["Predicted"].sum())

# HUD: Live = HA live power or physics; Daily = HA today/sum(actuals) or physics
total_pre_num = _safe_numeric(total_pre)
current_kw = live_kw_ha if live_kw_ha is not None else realtime_kw
current_kw = _safe_numeric(current_kw)
daily_total_kwh = today_kwh_ha if today_kwh_ha is not None else (_safe_numeric(sum(actuals)) if actuals else total_pre_num)
daily_total_kwh = _safe_numeric(daily_total_kwh)
if daily_total_kwh < 0:
    daily_total_kwh = total_pre_num
system_health_pct = (daily_total_kwh / total_pre_num * 100) if total_pre_num > 0 and actuals else (_safe_numeric(current_kw) / SYSTEM_KWP * 100) if current_kw else 0.0
system_health_pct = _safe_numeric(system_health_pct)

# High-density layout: force 4 metrics into one row (width: 25% !important)
st.markdown(
    """
    <style>
    div[data-testid="column"] { width: 25% !important; flex: 1 1 25% !important; min-width: 25% !important; text-align: center; }
    </style>
    """,
    unsafe_allow_html=True,
)
pred_live_kw = 0.0 if snow_override else _safe_numeric(realtime_kw)  # 430Wp / 80% bifacial; 0 when Snow Override
pred_total_kwh = total_pre_num  # full-day integration; 0 when Snow Override
fetch_label = "fetching..." if data_fetching else ""
m1, m2, m3, m4 = st.columns(4)
m1.metric("Live kW", fetch_label or f"{current_kw:.2f}")
m2.metric("Pred Live", fetch_label or f"{pred_live_kw:.2f}")
m3.metric("Daily kWh", fetch_label or f"{daily_total_kwh:.1f}")
m4.metric("Pred Daily", fetch_label or f"{pred_total_kwh:.1f}")

# Snow-Locked flag for February when Snow Override is ON
if snow_override and selected_date.month == 2:
    st.info("❄️ **Snow-Locked** — February records: prediction overridden to 0.")

# --- Navigation Tabs (Growatt-style time-series) ---
tab_hour, tab_day, tab_month, tab_year = st.tabs(["Hour", "Day", "Month", "Year"])

# --- Mobile: touch-action pan-y for vertical scroll; pinch zooms time (X) only on Hour tab ---
st.markdown(
    """
    <style>
    div[data-testid="stVerticalBlock"]:has(.js-plotly-plot),
    div[data-testid="stVerticalBlock"]:has([id^="plotly"]) {
        touch-action: pan-y !important;
        overflow: hidden !important;
        -webkit-overflow-scrolling: touch !important;
    }
    .js-plotly-plot, [id^="plotly"] {
        touch-action: pan-y !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------- HOUR TAB: 07:00–21:00 detailed view, horizontal pinch-zoom, hovermode x unified ----------
with tab_hour:
    fig = go.Figure()

    # Predicted: Electric Green #39FF14 (430Wp / 80% Bifacial)
    fig.add_trace(
        go.Bar(
            x=df["Time"],
            y=df["Predicted"],
            name="Predicted",
            customdata=df["Cloud_Cover"],
            hovertemplate="Predicted: %{y:.2f} kWh<br>Cloud: %{customdata:.0f}%%<extra></extra>",
            marker=dict(color=PREDICTED_COLOR, line=dict(color="black", width=1)),
            opacity=0.5,
        )
    )
    if actuals:
        fig.add_trace(
            go.Bar(
                x=df["Time"],
                y=df["Actual"],
                name="Actual (HA)",
                customdata=df["Cloud_Cover"],
                hovertemplate="Actual: %{y:.2f} kWh<br>Cloud: %{customdata:.0f}%%<extra></extra>",
                marker=dict(color=ACTUAL_COLOR, line=dict(color="black", width=1)),
                opacity=0.8,
            )
        )
    fig.update_xaxes(
        range=[f"{d_str} 07:00", f"{d_str} 21:00"],
        type="date",
        dtick=3600000 * 3,
        tickformat="%H:%M",
        fixedrange=False,
        spikemode="across",
        spikesnap="cursor",
        spikethickness=1,
        spikedash="solid",
    )
    pred_max = float(pd.to_numeric(df["Predicted"], errors="coerce").fillna(0).max())
    actual_max = float(pd.to_numeric(df["Actual"], errors="coerce").fillna(0).max()) if "Actual" in df.columns else 0.0
    max_yield = max(7.5, pred_max, actual_max, 0.01)
    fig.update_yaxes(
        range=[0, max_yield],
        fixedrange=True,
        nticks=4,
        title_text="Yield (kWh)",
        side="left",
        anchor="x",
    )
    fig.update_layout(
        margin=dict(l=48, r=0, t=40, b=0),
        height=400,
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        barmode="overlay",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.5, xanchor="center", bordercolor="rgba(0,0,0,0)", borderwidth=0),
        xaxis=dict(
            gridcolor="rgba(255,255,255,0.08)",
            fixedrange=False,
            range=[f"{d_str} 07:00", f"{d_str} 21:00"],
            spikemode="across",
            spikethickness=1,
            spikedash="solid",
            showspikes=True,
            spikesnap="cursor",
        ),
        yaxis=dict(gridcolor="rgba(255,255,255,0.08)", fixedrange=True),
        dragmode="pan",
        uirevision="abamu_hour",
        title="",
    )
    st.markdown('<div style="touch-action: pan-y;">', unsafe_allow_html=True)
    with st.container():
        st.plotly_chart(fig, use_container_width=True, key="abamu_solar_chart", config={"scrollZoom": True, "displayModeBar": False})
    st.markdown("</div>", unsafe_allow_html=True)

def _ha_day_total_kwh(base_url: str, token: str, entity_id: str, date_str: str) -> float:
    """Sum 5-min kWh for one day from HA history (for Day/Month/Year tabs)."""
    start_iso = f"{date_str}T00:00:00"
    end_iso = f"{date_str}T23:59:59"
    hist = ha_get_history(base_url, token, entity_id, start_iso, end_iso)
    slots = _history_to_5min_kwh(hist, date_str)
    return float(sum(slots)) if slots else 0.0


def _ha_month_total_kwh(base_url: str, token: str, entity_id: str, year: int, month: int) -> float:
    """Sum daily energy for each day in month via HA history (Month tab)."""
    from calendar import monthrange
    _, ndays = monthrange(year, month)
    total = 0.0
    for day in range(1, ndays + 1):
        ds = f"{year}-{month:02d}-{day:02d}"
        total += _ha_day_total_kwh(base_url, token, entity_id, ds)
    return total


# ---------- DAY TAB: Last 7 days aggregate (HA actuals vs 430Wp physics) ----------
with tab_day:
    day_dates = [selected_date - timedelta(days=i) for i in range(6, -1, -1)]
    day_actuals = []
    day_preds = []
    for d in day_dates:
        ds = d.strftime("%Y-%m-%d")
        pdf = get_varel_prediction(ds, cloud_cover)
        day_pred = float(pd.to_numeric(pdf["Predicted"], errors="coerce").fillna(0).sum())
        day_preds.append(0.0 if snow_override else day_pred)
        if ha_configured:
            day_actuals.append(_ha_day_total_kwh(ha_url, ha_token, ha_entity, ds))
        else:
            day_actuals.append(0.0)
    day_actuals = [float(_safe_numeric(x)) for x in day_actuals]
    day_preds = [float(_safe_numeric(x)) for x in day_preds]
    fig_day = go.Figure()
    fig_day.add_trace(go.Bar(x=[d.strftime("%a %d") for d in day_dates], y=day_preds, name="Predicted", marker=dict(color=PREDICTED_COLOR, line=dict(color="black", width=1)), opacity=0.5))
    fig_day.add_trace(go.Bar(x=[d.strftime("%a %d") for d in day_dates], y=day_actuals, name="Actual (HA)", marker=dict(color=ACTUAL_COLOR, line=dict(color="black", width=1)), opacity=0.8))
    max_yield = max(7.5, max(day_actuals or [0]), max(day_preds or [0]), 0.01)
    fig_day.update_yaxes(range=[0, max_yield], fixedrange=True, nticks=4, title_text="Yield (kWh)")
    fig_day.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        barmode="overlay",
        hovermode="x unified",
        margin=dict(l=48, r=0, t=24, b=48),
        height=320,
        xaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.5, xanchor="center"),
    )
    st.plotly_chart(fig_day, use_container_width=True, key="abamu_day_chart", config={"displayModeBar": False})

# ---------- MONTH TAB: Last 12 months (HA actuals vs 430Wp physics) ----------
with tab_month:
    now = datetime.now(BERLIN).date() if BERLIN else datetime.now().date()
    month_labels = []
    month_actuals = []
    month_preds = []
    y, m = now.year, now.month
    for _ in range(12):
        month_labels.append(datetime(y, m, 1).strftime("%b %Y"))
        if y == now.year and m == now.month:
            month_preds.append(0.0 if snow_override else pred_total_kwh)
        else:
            month_preds.append(0.0)
        if ha_configured:
            month_actuals.append(_ha_month_total_kwh(ha_url, ha_token, ha_entity, y, m))
        else:
            month_actuals.append(daily_total_kwh if (y == now.year and m == now.month) else 0.0)
        m -= 1
        if m < 1:
            m, y = 12, y - 1
    month_labels.reverse()
    month_actuals.reverse()
    month_preds.reverse()
    fig_month = go.Figure()
    fig_month.add_trace(go.Bar(x=month_labels, y=month_preds, name="Predicted", marker=dict(color=PREDICTED_COLOR, line=dict(color="black", width=1)), opacity=0.5))
    fig_month.add_trace(go.Bar(x=month_labels, y=month_actuals, name="Actual (HA)", marker=dict(color=ACTUAL_COLOR, line=dict(color="black", width=1)), opacity=0.8))
    max_yield = max(7.5, max(month_actuals or [0]), max(month_preds or [0]), 0.01)
    fig_month.update_yaxes(range=[0, max_yield], fixedrange=True, nticks=4, title_text="Yield (kWh)")
    fig_month.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        barmode="overlay",
        hovermode="x unified",
        margin=dict(l=48, r=0, t=24, b=80),
        height=320,
        xaxis=dict(gridcolor="rgba(255,255,255,0.08)", tickangle=-45),
        yaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.5, xanchor="center"),
    )
    st.plotly_chart(fig_month, use_container_width=True, key="abamu_month_chart", config={"displayModeBar": False})

# ---------- YEAR TAB: Last 3 years (HA actuals vs 430Wp physics) ----------
with tab_year:
    year_labels = [str(now.year - 2), str(now.year - 1), str(now.year)]
    year_actuals = [0.0, 0.0, 0.0]
    year_preds = [0.0, 0.0, 0.0]
    if ha_configured:
        for i, yr in enumerate([now.year - 2, now.year - 1, now.year]):
            for mo in range(1, 13):
                year_actuals[i] += _ha_month_total_kwh(ha_url, ha_token, ha_entity, yr, mo)
        if now.year in [now.year - 2, now.year - 1, now.year]:
            idx = [now.year - 2, now.year - 1, now.year].index(now.year)
            year_actuals[idx] = max(year_actuals[idx], daily_total_kwh)
    else:
        year_actuals[2] = daily_total_kwh if selected_date.year == now.year else 0.0
    year_preds[2] = 0.0 if snow_override else (pred_total_kwh if selected_date.year == now.year else 0.0)
    fig_year = go.Figure()
    fig_year.add_trace(go.Bar(x=year_labels, y=year_preds, name="Predicted", marker=dict(color=PREDICTED_COLOR, line=dict(color="black", width=1)), opacity=0.5))
    fig_year.add_trace(go.Bar(x=year_labels, y=year_actuals, name="Actual (HA)", marker=dict(color=ACTUAL_COLOR, line=dict(color="black", width=1)), opacity=0.8))
    max_yield = max(7.5, max(year_actuals or [0]), max(year_preds or [0]), 0.01)
    fig_year.update_yaxes(range=[0, max_yield], fixedrange=True, nticks=4, title_text="Yield (kWh)")
    fig_year.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        barmode="overlay",
        hovermode="x unified",
        margin=dict(l=48, r=0, t=24, b=48),
        height=320,
        xaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.5, xanchor="center"),
    )
    st.plotly_chart(fig_year, use_container_width=True, key="abamu_year_chart", config={"displayModeBar": False})

st.markdown("---")
_, footer_col, _ = st.columns([1, 2, 1])
footer_col.caption("Powered by Ojoma Abamu")
