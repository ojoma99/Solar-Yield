import os
import streamlit as st
import growattServer
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from datetime import datetime
import math
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
GROWATT_TOKEN = os.environ.get("GROWATT_TOKEN", "8d5u01rym66qw9rcf8k34414v71n3wju")
GROWATT_SERVER = "https://openapi.growatt.com/"  # EU server (no /v1/ — library appends v1/)

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
selected_date = st.sidebar.date_input("Analysis Date", datetime.now().date())
cloud_cover_pct = st.sidebar.slider("Cloud cover (%)", 0, 100, 0, help="Prioritize diffuse radiation when high; improves cloudy/low-irradiance match.")
cloud_cover = float(cloud_cover_pct) / 100.0
d_str = selected_date.strftime("%Y-%m-%d")

df = get_varel_prediction(d_str, cloud_cover)
# Coerce prediction to numeric so all math is float (no str - str)
df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)
total_pre = float(df["Predicted"].sum())
realtime_kw = real_time_expected_kw(cloud_cover)

def _safe_numeric(val) -> float:
    """Single value: pd.to_numeric(val, errors='coerce').fillna(0) as float."""
    if val is None:
        return 0.0
    s = pd.Series([val])
    return float(pd.to_numeric(s, errors="coerce").fillna(0).iloc[0])

def _extract_energy(val) -> float:
    """Extract kWh from Growatt entry; all inputs wrapped in pd.to_numeric(..., errors='coerce').fillna(0)."""
    if val is None:
        return 0.0
    if isinstance(val, dict):
        for key in ("energy", "day_total", "dayTotal", "dayPachage", "dayPackage", "total_yield"):
            v = val.get(key)
            if v is not None:
                return _safe_numeric(v)
        return 0.0
    return _safe_numeric(val)


# Growatt: session with browser User-Agent; no-cache for fresh auth; retry on None
actuals = []
current_power_w = None
sync_msg = "Inverter Syncing..."
last_sync = "N/A"
_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "token": GROWATT_TOKEN,
})

try:
    api = growattServer.OpenApiV1(token=GROWATT_TOKEN)
    api.session = _session
    api.server_url = GROWATT_SERVER
    plants_res = api.plant_list()
    if plants_res is None:
        fresh = requests.Session()
        fresh.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "token": GROWATT_TOKEN,
        })
        api.session = fresh
        plants_res = api.plant_list()
    plants = (plants_res or {}).get("plants") or []
    if plants:
        first_plant = plants[0]
        pid = first_plant.get("plant_id") or first_plant.get("id")
        if pid is not None:
            hist = api.plant_energy_history(pid, d_str, d_str, "day", 1, 300)
            if hist is None:
                hist = api.plant_energy_history(pid, d_str, d_str, "day", 1, 300)  # retry
            energy_list = (hist or {}).get("energy_data") or (hist or {}).get("dayPackage") or []
            if isinstance(energy_list, list):
                raw = [_extract_energy(x) for x in energy_list]
                actuals = list(pd.to_numeric(raw, errors="coerce").fillna(0))
            if actuals:
                sync_msg = "🟢 Online"
                last_sync = datetime.now().strftime("%H:%M:%S")
            _today = datetime.now(BERLIN).date() if BERLIN else datetime.now().date()
            if selected_date == _today:
                try:
                    power_data = api.plant_power_overview(pid, selected_date)
                    powers = (power_data or {}).get("powers") or []
                    if powers:
                        valid = [p for p in powers if p.get("power") is not None]
                        if valid:
                            p = valid[-1].get("power")
                            current_power_w = _safe_numeric(p) if p is not None else None
                            if current_power_w is not None and current_power_w < 0:
                                current_power_w = None
                except Exception:
                    pass
except growattServer.GrowattV1ApiError as e:
    sync_msg = "Inverter Syncing..."
    st.sidebar.warning(f"Growatt: {getattr(e, 'error_msg', str(e))}. Check token at openapi.growatt.com.")
except Exception as e:
    sync_msg = "Inverter Syncing..."
    st.sidebar.warning(f"Growatt sync failed: {e}. Physics prediction shown.")

# --- 3. DISPLAY ---
with st.sidebar:
    st.subheader("System Status")
    st.write(f"Growatt Status: {sync_msg}")
    st.write(f"Last Sync: {last_sync}")
    show_clouds = st.toggle("Enable Cloud Overlay", value=True)

# Align df with actuals; all yield/power numeric before any subtraction (fix str - str)
if actuals:
    df = df.iloc[:len(actuals)].copy()
    df["Actual"] = pd.Series(pd.to_numeric(actuals, errors="coerce")).fillna(0)
    df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)
    df["Diff"] = df["Actual"].astype(float) - df["Predicted"].astype(float)
else:
    df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)

# HUD: all values pd.to_numeric(val, errors='coerce').fillna(0) — prevents 'str - str' math errors
total_pre_num = _safe_numeric(total_pre)
current_kw = (current_power_w / 1000.0) if current_power_w is not None else realtime_kw
current_kw = _safe_numeric(current_kw)
daily_total_kwh = _safe_numeric(sum(actuals)) if actuals else total_pre_num
if daily_total_kwh < 0:
    daily_total_kwh = total_pre_num
system_health_pct = (daily_total_kwh / total_pre_num * 100) if total_pre_num > 0 and actuals else (_safe_numeric(current_kw) / SYSTEM_KWP * 100) if current_kw else 0.0
system_health_pct = _safe_numeric(system_health_pct)

m1, m2, m3 = st.columns(3)
m1.metric("Live Power", f"{current_kw:.2f} kW")
m2.metric("Yield", f"{daily_total_kwh:.1f} kWh")
m3.metric("Efficiency", f"{system_health_pct:.1f}%")

# Spacer before chart
st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)

# --- Mobile: touch-action pan-y + overflow hidden so page scrolls up/down; chart pinch/pan inside ---
st.markdown(
    """
    <style>
    div[data-testid="stVerticalBlock"]:has(.js-plotly-plot),
    div[data-testid="stVerticalBlock"]:has([id^="plotly"]) {
        touch-action: pan-y !important;
        overflow: hidden !important;
    }
    .js-plotly-plot, [id^="plotly"] {
        touch-action: pan-y !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# --- 4. THE HISTOGRAM (Physics baseline: 430Wp / 80% Bifacial — Green bars always active; if Growatt fails, UI shows physics only) ---
fig = go.Figure()

# Predicted: Electric Green #39FF14, 1.5px black border (always drawn so UI is never empty)
fig.add_trace(
    go.Bar(
        x=df["Time"],
        y=df["Predicted"],
        name="Predicted",
        customdata=df["Cloud_Cover"],
        hovertemplate="Predicted: %{y:.2f} kWh<br>Cloud: %{customdata:.0f}%%<extra></extra>",
        marker=dict(
            color=PREDICTED_COLOR,
            line=dict(color="black", width=1),
        ),
        opacity=0.5,
    )
)

# Actual: Neon Yellow #FFFF00, 1px black border (Growatt inverter)
if actuals:
    fig.add_trace(
        go.Bar(
            x=df["Time"],
            y=df["Actual"],
            name="Actual",
            customdata=df["Cloud_Cover"],
            hovertemplate="Actual: %{y:.2f} kWh<br>Cloud: %{customdata:.0f}%%<extra></extra>",
            marker=dict(
                color=ACTUAL_COLOR,
                line=dict(color="black", width=1),
            ),
            opacity=0.8,
        )
    )

# Fixed window 07:00–21:00; x stretchable (fixedrange=False) for two-finger horizontal stretch
fig.update_xaxes(
    range=[f"{d_str} 07:00", f"{d_str} 21:00"],
    type="date",
    dtick=3600000 * 3,
    tickformat="%H:%M",
    fixedrange=False,
)
# Tight crop Y: [0, max(actual, predicted)]; if both nearly zero use [0, 0.5] so bars still fill vertical space
pred_max = float(pd.to_numeric(df["Predicted"], errors="coerce").fillna(0).max())
actual_max = float(pd.to_numeric(df["Actual"], errors="coerce").fillna(0).max()) if "Actual" in df.columns else 0.0
y_top = max(pred_max, actual_max, 0.5)
fig.update_yaxes(
    range=[0, y_top],
    fixedrange=True,
    nticks=5,
    title_text="Yield (kWh)",
    side="left",
    anchor="x",
)

# --- Clean Chart: legend top, no toolbar, deep charcoal bg, pan for mobile ---
fig.update_layout(
    margin=dict(l=48, r=0, t=40, b=0),
    height=400,
    template="plotly_dark",
    paper_bgcolor="#121212",
    plot_bgcolor="#121212",
    font=dict(color="#e0e0e0"),
    barmode="overlay",
    hovermode="x unified",
    legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.02,
        x=0.5,
        xanchor="center",
        bordercolor="rgba(0,0,0,0)",
        borderwidth=0,
    ),
    xaxis=dict(gridcolor="rgba(255,255,255,0.1)", fixedrange=False, range=[f"{d_str} 07:00", f"{d_str} 21:00"]),
    yaxis=dict(gridcolor="rgba(255,255,255,0.1)", fixedrange=True),
    dragmode="pan",
    uirevision="abamu_chart",
    title="",
)

with st.container():
    st.plotly_chart(
        fig,
        use_container_width=True,
        key="abamu_solar_chart",
        config={
            "scrollZoom": True,
            "displayModeBar": False,
        },
    )

st.markdown("---")
st.markdown(
    '<p style="text-align: center; color: #888; font-size: 0.85rem; margin: 0.5rem 0;">powered by ojoma abamu</p>',
    unsafe_allow_html=True,
)
