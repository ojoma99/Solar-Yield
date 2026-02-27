import streamlit as st
import growattServer
import requests
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from datetime import datetime
import math

# Force a dark Plotly theme with explicit colors
pio.templates.default = "plotly_dark"

# --- SYSTEM CONFIG ---
SYSTEM_KWP = 8.6
LAT, LON = 53.396, 8.136
GROWATT_TOKEN = "tb346b22pb1e34nhf057tcq48xkyc7aq"

# --- High Contrast Sunlight Theme ---
PREDICTED_COLOR = "#39FF14"  # Electric Green (Predicted)
ACTUAL_COLOR = "#39FF14"     # Electric Green (Actual)

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

# --- 1. PHYSICS ENGINE (ALWAYS AVAILABLE) ---
def get_varel_prediction(date_str: str) -> pd.DataFrame:
    """
    Physics model for Abamu Residence (Hafenstr. 18).

    - Uses Open-Meteo hourly data (GHI, tilted irradiance proxy, visibility, cloud cover)
    - Approximates Hay-Davies-style projection via global_tilted_irradiance
    - Adds harbor water-surface albedo (specular 0.36)
    - Applies -0.30%/°C thermal coefficient
    - Applies an 18% haze penalty when visibility < 15 km
    - Applies a 0.95 clarity factor on very clear hours (visibility > 15 km)
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&hourly=temperature_2m,shortwave_radiation,global_tilted_irradiance,visibility,cloud_cover"
        "&timezone=Europe%2FBerlin"
        f"&start_date={date_str}&end_date={date_str}"
    )
    res = requests.get(url).json()["hourly"]

    times = pd.date_range(
        start=f"{date_str} 00:00",
        end=f"{date_str} 23:55",
        freq="5min",
    )

    preds: list[float] = []
    clouds: list[float] = []

    tilt_deg = 60.0
    tilt_rad = math.radians(tilt_deg)
    # View factor for ground-reflected component on a tilted plane
    ground_view = (1.0 - math.cos(tilt_rad)) / 2.0
    albedo_specular = 0.36

    for i in range(len(res["time"])):
        t = res["temperature_2m"][i]           # °C
        ghi = res["shortwave_radiation"][i]    # W/m², horizontal
        gti = res["global_tilted_irradiance"][i]  # W/m², already on 60° plane (Open-Meteo model)
        v_km = res["visibility"][i] / 1000.0
        c = res["cloud_cover"][i]

        # Haze: only penalize when visibility is clearly limited (< 15 km)
        f_haze = 0.82 if v_km < 15.0 else 1.0
        # Clarity: slightly reduce prediction on ultra-clear hours (> 15 km)
        f_clarity = 0.95 if v_km > 15.0 else 1.0

        # Water-surface specular albedo contribution (harbor reflections)
        g_ground = ghi * albedo_specular * ground_view

        poa_irradiance = (gti + g_ground) * f_haze * f_clarity  # W/m² on plane-of-array

        # Thermal power coefficient based on ambient temperature
        f_temp = 1.0 + (-0.003 * (t - 25.0))

        hourly_energy_kwh = SYSTEM_KWP * (poa_irradiance / 1000.0) * f_temp

        # Distribute hourly energy into 12 x 5-minute bins
        slice_kwh = max(0.0, hourly_energy_kwh / 12.0)
        for _ in range(12):
            preds.append(slice_kwh)
            clouds.append(c)

    return pd.DataFrame({"Time": times, "Predicted": preds, "Cloud_Cover": clouds})

# --- 2. THE DASHBOARD ---
# Abamu Residence heading
st.markdown("# Abamu Residence  \n### Solar Predictor")
selected_date = st.sidebar.date_input("Analysis Date", datetime.now().date())
d_str = selected_date.strftime("%Y-%m-%d")

# Step A: Generate Prediction (Orange Bars)
df = get_varel_prediction(d_str)
total_pre = df['Predicted'].sum()

# Step B: Attempt Growatt Sync (Green/Red Bars)
actuals = []
sync_msg = "🔴 Sync Delay / No Data"
last_sync = "N/A"

try:
    api = growattServer.OpenApiV1(token=GROWATT_TOKEN)
    api.server_url = "https://openapi.growatt.com/v1/" # Force EU Server
    
    plants_res = api.plant_list()
    if plants_res and 'data' in plants_res and plants_res['data']['plants']:
        pid = plants_res['data']['plants'][0]['plant_id']
        hist = api.plant_energy_history(pid, d_str, d_str, 'day', 1, 300)
        actuals = [float(x.get('energy', 0)) for x in hist['data']['energy_data']]
        
        if actuals:
            sync_msg = "🟢 Online"
            last_sync = datetime.now().strftime("%H:%M:%S")
except Exception as e:
    # Quiet fail: Don't break the app, just inform the user
    st.sidebar.warning(f"Sync Delay: using physics-only prediction ({e})")

# --- 3. DISPLAY ---
with st.sidebar:
    st.subheader("System Status")
    st.write(f"Growatt Status: {sync_msg}")
    st.write(f"Last Sync: {last_sync}")
    show_clouds = st.toggle("Enable Cloud Overlay", value=True)

m1, m2, m3 = st.columns(3)
m1.metric("Predicted Total", f"{total_pre:.2f} kWh")

if actuals:
    df = df.iloc[:len(actuals)]  # Align lengths
    df["Actual"] = actuals
    df["Diff"] = df["Actual"] - df["Predicted"]
    total_act = sum(actuals)
    m2.metric("Actual Today", f"{total_act:.2f} kWh")
    m3.metric("Health Score", f"{(total_act / total_pre * 100):.1f}%")
else:
    m2.metric("Actual Today", "0.00 kWh (Waiting...)")
    m3.metric("Health Score", "N/A")

# Spacer between metrics and chart for legend/modebar
st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)

# --- 4. THE HISTOGRAM ---
from plotly.subplots import make_subplots
fig = go.Figure()

# Predicted yield bars (single y-axis)
fig.add_trace(
    go.Bar(
        x=df["Time"],
        y=df["Predicted"],
        name="Predicted",
        marker=dict(
            color=PREDICTED_COLOR,
            line=dict(color="black", width=1),
        ),
        opacity=0.5,
    )
)

# Optional actual yield overlay
if actuals:
    fig.add_trace(
        go.Bar(
            x=df["Time"],
            y=df["Actual"],
            name="Actual",
            marker=dict(
                color=ACTUAL_COLOR,
                line=dict(color="black", width=1),
            ),
            opacity=0.8,
        )
    )

# Cloud data kept in legend via an invisible trace (no clutter on chart)
fig.add_trace(
    go.Scatter(
        x=df["Time"],
        y=df["Cloud_Cover"],
        name="Cloud %",
        mode="lines",
        line=dict(color="#3498db", width=2),
        visible="legendonly",
    )
)

# --- Focus Window: 07:00–21:00 ---
fig.update_xaxes(
    range=[f"{d_str} 07:00", f"{d_str} 21:00"],
    type="date",
    dtick=3600000 * 3,
    tickformat="%H:%M",
)

# --- Interaction & Layout ---
fig.update_layout(
    margin=dict(l=0, r=0, t=40, b=0),
    height=400,
    template="plotly_dark",
    barmode="overlay",
    hovermode="x unified",
    legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.1,
        xanchor="center",
        x=0.5,
    ),
    dragmode="pan",
    modebar=dict(orientation="v", bgcolor="rgba(0,0,0,0)"),
    title="",
)

st.plotly_chart(
    fig,
    use_container_width=True,
    config={
        "scrollZoom": True,
    },
)

st.caption("Powered by Ojoma Abamu")
