import os
import streamlit as st
import growattServer
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from datetime import datetime, timedelta
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
GROWATT_SERVER = "https://openapi.growatt.com/"  # EU server (no /v1/ — library appends v1/)

# Persistent browser session: realistic User-Agent; cookies stored in session after login
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def _apply_token_to_session(session: requests.Session, token: str) -> None:
    """Apply token and headers to session for Growatt API calls."""
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "token": token,
    })

def _is_auth_error(exc: BaseException) -> bool:
    """True if exception indicates 401 Unauthorized or 403 Forbidden."""
    code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if code in (401, 403):
        return True
    msg = str(exc).lower()
    return "401" in str(exc) or "403" in str(exc) or "unauthorized" in msg or "forbidden" in msg

def _check_session_and_retry(api, session: requests.Session, token: str, fetch_fn):
    """Run fetch_fn(); if it fails with 401/403, re-apply token and retry once. Returns (result, error)."""
    try:
        return fetch_fn(), None
    except Exception as e:
        if not _is_auth_error(e):
            return None, e
        _apply_token_to_session(session, token)
        api.session = session
        api.server_url = GROWATT_SERVER
        try:
            return fetch_fn(), None
        except Exception as e2:
            return None, e2

def _growatt_login_username_password(username: str, password: str) -> str | None:
    """Try to get token from Growatt Open API using username/password. Returns token or None."""
    if not (username and password):
        return None
    try:
        url = GROWATT_SERVER.rstrip("/") + "/openApi/v1/user/login"
        sess = requests.Session()
        sess.headers.update({"User-Agent": USER_AGENT, "Content-Type": "application/json"})
        r = sess.post(url, json={"userName": username.strip(), "password": password}, timeout=15)
        r.raise_for_status()
        data = r.json()
        token = (data.get("data") or {}).get("token") or data.get("token")
        return str(token).strip() if token else None
    except Exception:
        return None

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

# Session state for global Growatt data (all tabs)
if "plants" not in st.session_state:
    st.session_state["plants"] = []
if "growatt_api" not in st.session_state:
    st.session_state["growatt_api"] = None
if "plant_id" not in st.session_state:
    st.session_state["plant_id"] = None
if "growatt_token" not in st.session_state:
    st.session_state["growatt_token"] = None

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
# --- Sidebar: Persistent Login ---
with st.sidebar:
    st.subheader("Growatt Login")
    username = st.text_input("Username", key="growatt_username")
    password = st.text_input("Password", type="password", key="growatt_password")
    if st.button("Login"):
        token = _growatt_login_username_password(username, password)
        if token:
            _session = requests.Session()
            _apply_token_to_session(_session, token)
            api_login = growattServer.OpenApiV1(token=token)
            api_login.session = _session
            api_login.server_url = GROWATT_SERVER
            try:
                plants_res = api_login.plant_list()
                plants_list = (plants_res or {}).get("plants") or []
                if plants_list:
                    first_plant = plants_list[0]
                    pid_val = first_plant.get("plant_id") or first_plant.get("id")
                    if pid_val is not None:
                        st.session_state["growatt_api"] = api_login
                        st.session_state["plant_id"] = pid_val
                        st.session_state["growatt_token"] = token
                        st.session_state["plants"] = plants_list
                        st.success("Logged in!")
                    else:
                        st.error("No plant ID found")
                else:
                    st.error("No plants found")
            except Exception as e:
                st.error(f"Login failed: {e}")
        else:
            st.error("Invalid credentials")

selected_date = st.sidebar.date_input("Analysis Date", datetime.now().date())
cloud_cover_pct = st.sidebar.slider("Cloud cover (%)", 0, 100, 0, help="Prioritize diffuse radiation when high; improves cloudy/low-irradiance match.")
cloud_cover = float(cloud_cover_pct) / 100.0
snow_override = st.sidebar.toggle("Snow Override", value=False, help="Drop prediction to 0; flag February as Snow-Locked")
d_str = selected_date.strftime("%Y-%m-%d")

# Resolve api and plant_id for data fetch (updated after login)
api = st.session_state.get("growatt_api")
_pid = st.session_state.get("plant_id")

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


# Growatt: fetch energy data when logged in (api + plant_id from session_state)
actuals = []
current_power_w = None
sync_msg = "Login to sync" if not (api and _pid) else "Inverter Syncing..."
last_sync = "N/A"

if api and _pid:
    _token = st.session_state.get("growatt_token")
    _session = getattr(api, "session", None) or requests.Session()
    if _token:
        _apply_token_to_session(_session, _token)
    api.session = _session
    api.server_url = GROWATT_SERVER
    try:
        hist = api.plant_energy_history(_pid, d_str, d_str, "day", 1, 300)
        if hist is None:
            hist = api.plant_energy_history(_pid, d_str, d_str, "day", 1, 300)
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
                power_data = api.plant_power_overview(_pid, selected_date)
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
        st.sidebar.warning(f"Growatt: {getattr(e, 'error_msg', str(e))}. Re-login may help.")
    except Exception as e:
        sync_msg = "Inverter Syncing..."
        st.sidebar.warning(f"Growatt sync failed: {e}. Physics prediction shown.")

# --- 3. DISPLAY ---
with st.sidebar:
    st.subheader("System Status")
    st.write(f"Growatt Status: {sync_msg}")
    st.write(f"Last Sync: {last_sync}")
    show_clouds = st.toggle("Enable Cloud Overlay", value=True)

# Snow Override: drop prediction to 0 when enabled
if snow_override:
    df["Predicted"] = 0.0
    df["Cloud_Cover"] = 0.0  # clear cloud overlay when snow-locked

# Align df with actuals; all yield/power numeric before any subtraction (fix str - str)
if actuals:
    df = df.iloc[:len(actuals)].copy()
    df["Actual"] = pd.Series(pd.to_numeric(actuals, errors="coerce")).fillna(0)
    df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)
    df["Diff"] = df["Actual"].astype(float) - df["Predicted"].astype(float)
else:
    df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0)

# Physics calibration: JAM54D41-430/LB (430Wp, 80% Bifacial) is the fallback when cloud/inverter data is delayed; show green Predicted bars. When cloudy, apply 0.15 irradiance multiplier.
if not actuals or _safe_numeric(sum(actuals)) < 0.5:
    df["Predicted"] = pd.to_numeric(df["Predicted"], errors="coerce").fillna(0) * 0.15
total_pre = float(df["Predicted"].sum())

# HUD: all values pd.to_numeric(val, errors='coerce').fillna(0) — prevents 'str - str' math errors
total_pre_num = _safe_numeric(total_pre)
current_kw = (current_power_w / 1000.0) if current_power_w is not None else realtime_kw
current_kw = _safe_numeric(current_kw)
daily_total_kwh = _safe_numeric(sum(actuals)) if actuals else total_pre_num
if daily_total_kwh < 0:
    daily_total_kwh = total_pre_num
system_health_pct = (daily_total_kwh / total_pre_num * 100) if total_pre_num > 0 and actuals else (_safe_numeric(current_kw) / SYSTEM_KWP * 100) if current_kw else 0.0
system_health_pct = _safe_numeric(system_health_pct)

# Single-row metric HUD (portrait fix): force 4 metrics into one row
st.markdown(
    '<style>div[data-testid="column"] {width: 25% !important; flex: 1 1 25% !important; min-width: 25% !important; text-align: center;}</style>',
    unsafe_allow_html=True,
)
pred_live_kw = 0.0 if snow_override else _safe_numeric(realtime_kw)  # 430Wp / 80% bifacial; 0 when Snow Override
pred_total_kwh = total_pre_num  # full-day integration; 0 when Snow Override
m1, m2, m3, m4 = st.columns(4)
m1.metric("Live kW", f"{current_kw:.2f}")
m2.metric("Pred Live", f"{pred_live_kw:.2f}")
m3.metric("Daily kWh", f"{daily_total_kwh:.1f}")
m4.metric("Pred Daily", f"{pred_total_kwh:.1f}")

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
                name="Actual",
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
    max_val = max(pred_max, actual_max, 0.01)
    fig.update_yaxes(
        range=[0, max_val],
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

# ---------- DAY TAB: Last 7 days aggregate (Actual vs Predicted, 430Wp physics) ----------
with tab_day:
    day_dates = [selected_date - timedelta(days=i) for i in range(6, -1, -1)]
    day_actuals = []
    day_preds = []
    for d in day_dates:
        ds = d.strftime("%Y-%m-%d")
        pdf = get_varel_prediction(ds, cloud_cover)
        day_pred = float(pd.to_numeric(pdf["Predicted"], errors="coerce").fillna(0).sum())
        day_preds.append(0.0 if snow_override else day_pred)
        if _pid and api is not None:
            try:
                h = api.plant_energy_history(_pid, ds, ds, "day", 1, 300)
                el = (h or {}).get("energy_data") or (h or {}).get("dayPackage") or []
                day_actuals.append(float(pd.to_numeric([_extract_energy(x) for x in el], errors="coerce").fillna(0).sum()) if isinstance(el, list) else 0.0)
            except Exception:
                day_actuals.append(0.0)
        else:
            day_actuals.append(0.0)
    day_actuals = [float(pd.to_numeric(x, errors="coerce").fillna(0)) for x in day_actuals]
    day_preds = [float(pd.to_numeric(x, errors="coerce").fillna(0)) for x in day_preds]
    fig_day = go.Figure()
    fig_day.add_trace(go.Bar(x=[d.strftime("%a %d") for d in day_dates], y=day_preds, name="Predicted", marker=dict(color=PREDICTED_COLOR, line=dict(color="black", width=1)), opacity=0.5))
    fig_day.add_trace(go.Bar(x=[d.strftime("%a %d") for d in day_dates], y=day_actuals, name="Actual", marker=dict(color=ACTUAL_COLOR, line=dict(color="black", width=1)), opacity=0.8))
    max_day = max(max(day_actuals or [0]), max(day_preds or [0]), 0.01)
    fig_day.update_yaxes(range=[0, max_day], fixedrange=True, nticks=4, title_text="Yield (kWh)")
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

# ---------- MONTH TAB: Last 12 months (current month from daily; rest placeholder) ----------
with tab_month:
    now = datetime.now(BERLIN).date() if BERLIN else datetime.now().date()
    month_labels = []
    month_actuals = []
    month_preds = []
    y, m = now.year, now.month
    for _ in range(12):
        month_labels.append(datetime(y, m, 1).strftime("%b %Y"))
        if y == now.year and m == now.month:
            month_actuals.append(daily_total_kwh)
            month_preds.append(pred_total_kwh)
        else:
            month_actuals.append(0.0)
            month_preds.append(0.0)
        m -= 1
        if m < 1:
            m, y = 12, y - 1
    month_labels.reverse()
    month_actuals.reverse()
    month_preds.reverse()
    fig_month = go.Figure()
    fig_month.add_trace(go.Bar(x=month_labels, y=month_preds, name="Predicted", marker=dict(color=PREDICTED_COLOR, line=dict(color="black", width=1)), opacity=0.5))
    fig_month.add_trace(go.Bar(x=month_labels, y=month_actuals, name="Actual", marker=dict(color=ACTUAL_COLOR, line=dict(color="black", width=1)), opacity=0.8))
    max_month = max(max(month_actuals or [0]), max(month_preds or [0]), 0.01)
    fig_month.update_yaxes(range=[0, max_month], fixedrange=True, nticks=4, title_text="Yield (kWh)")
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

# ---------- YEAR TAB: Last 3 years (current year from daily; rest placeholder) ----------
with tab_year:
    year_labels = [str(now.year - 2), str(now.year - 1), str(now.year)]
    year_actuals = [0.0, 0.0, daily_total_kwh if selected_date.year == now.year else 0.0]
    year_preds = [0.0, 0.0, pred_total_kwh if selected_date.year == now.year else 0.0]
    fig_year = go.Figure()
    fig_year.add_trace(go.Bar(x=year_labels, y=year_preds, name="Predicted", marker=dict(color=PREDICTED_COLOR, line=dict(color="black", width=1)), opacity=0.5))
    fig_year.add_trace(go.Bar(x=year_labels, y=year_actuals, name="Actual", marker=dict(color=ACTUAL_COLOR, line=dict(color="black", width=1)), opacity=0.8))
    max_year = max(max(year_actuals or [0]), max(year_preds or [0]), 0.01)
    fig_year.update_yaxes(range=[0, max_year], fixedrange=True, nticks=4, title_text="Yield (kWh)")
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
