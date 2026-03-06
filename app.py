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

# --- SOVEREIGN CONFIG (Hard-Coded) ---
HA_URL = os.environ.get("HA_URL", "http://192.168.8.124:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "PASTE_YOUR_LONG_LIVED_ACCESS_TOKEN_HERE")

# FSP Inverter Sensor DNA
HA_POWER_ENTITY = "sensor.fsp0e3304v_system_power"
HA_TODAY_ENTITY = "sensor.fsp0e3304v_system_production_today"
HA_LIFETIME_ENTITY = "sensor.fsp0e3304v_lifetime_system_production"

# Varel Geometry
LAT, LON = 53.396, 8.136
LAT_RAD = math.radians(LAT)
SYSTEM_KWP = 8.6  # 20 Modules @ 430Wp
TILT_DEG = 60.0
AZIMUTH_PANEL = 225.0  # SW
ALBEDO_SPECULAR = 0.38
N_MODULES = 20
PMAX_W = 430

# Themes
pio.templates.default = "plotly_dark"
PREDICTED_COLOR = "#39FF14"  # Electric Green
ACTUAL_COLOR = "#FFFF00"     # Neon Yellow

st.set_page_config(page_title="Abamu Solar Sovereign", layout="wide", page_icon="⚓")

# --- UI HEADER ---
st.markdown("""
    <style>
    [data-testid="stHeader"] { background: #121212; }
    .abamu-header {
        text-align: center; color: #C0C0C0; font-weight: 700;
        letter-spacing: 0.2em; font-size: 1.5rem; padding: 1rem 0;
    }
    /* Force 4 columns into one row on mobile */
    div[data-testid="column"] {
        width: 25% !important; flex: 1 1 25% !important;
        min-width: 25% !important; text-align: center;
    }
    </style>
    <p class="abamu-header">ABAMU SOVEREIGN TERMINAL</p>
""", unsafe_allow_html=True)

# --- DATA ENGINE ---
def get_ha_state(entity_id):
    url = urljoin(HA_URL, f"api/states/{entity_id}")
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=5)
        return r.json().get('state', '0')
    except Exception:
        return '0'

def fetch_raw_data():
    live = float(pd.to_numeric(get_ha_state(HA_POWER_ENTITY), errors='coerce').fillna(0))
    today = float(pd.to_numeric(get_ha_state(HA_TODAY_ENTITY), errors='coerce').fillna(0))
    total = float(pd.to_numeric(get_ha_state(HA_LIFETIME_ENTITY), errors='coerce').fillna(0))
    return live, today, total

# --- PHYSICS BASELINE (430Wp Bifacial, Cooper/AM) ---
def _solar_elevation_azimuth(dt):
    n = dt.timetuple().tm_yday
    hour_dec = dt.hour + dt.minute / 60.0 + getattr(dt, "second", 0) / 3600.0
    dec_deg = 23.45 * math.sin(math.radians(360.0 * (284 + n) / 365.0))
    dec_rad = math.radians(dec_deg)
    hour_angle_deg = 15.0 * (12.25 - hour_dec)
    hour_angle_rad = math.radians(hour_angle_deg)
    sin_elev = math.sin(LAT_RAD) * math.sin(dec_rad) + math.cos(LAT_RAD) * math.cos(dec_rad) * math.cos(hour_angle_rad)
    sin_elev = max(-1.0, min(1.0, sin_elev))
    elev_deg = math.degrees(math.asin(sin_elev))
    cos_az = (math.sin(dec_rad) - math.sin(LAT_RAD) * sin_elev) / (math.cos(LAT_RAD) * math.cos(math.asin(sin_elev)) if sin_elev < 1.0 else 1e-6)
    cos_az = max(-1.0, min(1.0, cos_az))
    azim_deg = math.degrees(math.acos(cos_az))
    if hour_angle_deg > 0:
        azim_deg = 360.0 - azim_deg
    return elev_deg, azim_deg

def _poa_kw(elev_deg, azim_deg):
    if elev_deg <= 0:
        return 0.0
    tilt_rad = math.radians(TILT_DEG)
    ground_view = (1.0 - math.cos(tilt_rad)) / 2.0
    am = 1.0 / max(0.01, math.sin(math.radians(elev_deg)))
    ghi = 1361.0 * (0.7 ** min(am, 5.0)) * math.sin(math.radians(elev_deg))
    sin_elev = max(0.01, math.sin(math.radians(elev_deg)))
    dni = ghi / sin_elev if sin_elev > 0.01 else 0.0
    inc_rad = math.acos(
        math.cos(math.radians(elev_deg)) * math.cos(tilt_rad)
        + math.sin(math.radians(elev_deg)) * math.sin(tilt_rad) * math.cos(math.radians(azim_deg - AZIMUTH_PANEL))
    )
    cos_inc = max(0.0, math.cos(inc_rad))
    poa_front = dni * cos_inc + 0.20 * ghi * (1.0 + math.cos(tilt_rad)) / 2.0 + ALBEDO_SPECULAR * ghi * ground_view
    kw = (poa_front / 1000.0) * (N_MODULES * PMAX_W) / 1000.0
    return max(0.0, kw)

def get_physics_prediction():
    now = datetime.now()
    elev, azim = _solar_elevation_azimuth(now)
    pred_live = _poa_kw(elev, azim)
    d_str = now.strftime("%Y-%m-%d")
    times = pd.date_range(start=f"{d_str} 00:00", end=f"{d_str} 23:55", freq="5min")
    pred_daily = 0.0
    for t in times:
        dt = t.to_pydatetime()
        elev_t, azim_t = _solar_elevation_azimuth(dt)
        kw = _poa_kw(elev_t, azim_t)
        pred_daily += max(0.0, kw * (5.0 / 60.0))
    return pred_live, pred_daily

def get_prediction_bars(date_str):
    times = pd.date_range(start=f"{date_str} 07:00", end=f"{date_str} 21:00", freq="5min")
    preds = []
    for t in times:
        dt = t.to_pydatetime()
        elev, azim = _solar_elevation_azimuth(dt)
        kw = _poa_kw(elev, azim)
        preds.append(max(0.0, kw * (5.0 / 60.0)))
    return times, preds

# --- HUD EXECUTION ---
live_kw, today_kwh, total_kwh = fetch_raw_data()
pred_live, pred_daily = get_physics_prediction()

m1, m2, m3, m4 = st.columns(4)
m1.metric("Live kW", f"{live_kw:.2f}")
m2.metric("Pred Live", f"{pred_live:.2f}")
m3.metric("Daily kWh", f"{today_kwh:.1f}")
m4.metric("Pred Daily", f"{pred_daily:.1f}")

# --- TABS ---
tab_hour, tab_day, tab_month, tab_year = st.tabs(["Hour", "Day", "Month", "Year"])

with tab_hour:
    d_str = datetime.now().strftime("%Y-%m-%d")
    times, preds = get_prediction_bars(d_str)
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=times, y=preds, name="Predicted",
        marker=dict(color=PREDICTED_COLOR, line=dict(color="black", width=1)),
        opacity=0.5,
    ))
    max_data = max(max(preds) if preds else 0.01, today_kwh, 0.01)
    fig.update_yaxes(range=[0, max_data], fixedrange=True, nticks=4, title_text="Yield (kWh)")
    fig.update_xaxes(
        fixedrange=False,
        spikemode="across",
        spikesnap="cursor",
        spikethickness=1,
        spikedash="solid",
    )
    fig.update_layout(
        height=400,
        margin=dict(l=48, r=0, t=24, b=0),
        hovermode="x unified",
        dragmode="pan",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0"),
        barmode="overlay",
        xaxis=dict(gridcolor="rgba(255,255,255,0.08)", showspikes=True),
        yaxis=dict(gridcolor="rgba(255,255,255,0.08)"),
    )
    st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': False, 'scrollZoom': True})

with tab_day:
    st.info("Day view: aggregate by day (HA history integration coming soon).")

with tab_month:
    st.info("Month view: aggregate by month (HA history integration coming soon).")

with tab_year:
    st.info("Year view: aggregate by year (HA history integration coming soon).")

st.caption(f"System Lifetime: {total_kwh:.1f} kWh | Local HA Bridge Active")
