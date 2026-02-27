import streamlit as st
import growattServer
import requests
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
import math

# --- SYSTEM CONFIG ---
SYSTEM_KWP = 8.6
LAT, LON = 53.396, 8.136
GROWATT_TOKEN = "tb346b22pb1e34nhf057tcq48xkyc7aq"

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
    df = df.iloc[:len(actuals)] # Align lengths
    df['Actual'] = actuals
    df['Diff'] = df['Actual'] - df['Predicted']
    total_act = sum(actuals)
    m2.metric("Actual Today", f"{total_act:.2f} kWh")
    m3.metric("Health Score", f"{(total_act/total_pre*100):.1f}%")
else:
    m2.metric("Actual Today", "0.00 kWh (Waiting...)")
    m3.metric("Health Score", "N/A")

# --- 4. THE HISTOGRAM ---
from plotly.subplots import make_subplots
fig = make_subplots(specs=[[{"secondary_y": True}]])

fig.add_trace(
    go.Bar(
        x=df["Time"],
        y=df["Predicted"],
        name="Predicted (Orange)",
        marker_color="orange",
        opacity=0.3,
    ),
    secondary_y=False,
)

if actuals:
    df["Color"] = df.apply(
        lambda r: "#2ecc71" if r["Actual"] >= r["Predicted"] else "#e74c3c", axis=1
    )
    fig.add_trace(
        go.Bar(
            x=df["Time"],
            y=df["Actual"],
            name="Actual (Yield)",
            marker_color=df["Color"],
        ),
        secondary_y=False,
    )

if show_clouds:
    fig.add_trace(
        go.Scatter(
            x=df["Time"],
            y=df["Cloud_Cover"],
            name="Cloud %",
            line=dict(color="#3498db", width=2),
        ),
        secondary_y=True,
    )

fig.update_layout(
    margin=dict(l=0, r=0, t=30, b=0),  # minimal margins, leave room for title
    height=400,                        # fixed height for mobile
    template="plotly_dark",
    barmode="overlay",
    hovermode="x unified",
    legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.02,           # just above chart
        xanchor="center",
        x=0.5,
    ),
    dragmode="zoom",
    title=f"Hafenstr. 18 Performance: {d_str}",
)

st.plotly_chart(
    fig,
    use_container_width=True,
    config={
        "scrollZoom": True,      # pinch-to-zoom
        "responsive": True,      # full-width on mobile
        "displayModeBar": False, # cleaner UI
    },
)
