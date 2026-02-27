import streamlit as st
import growattServer
import requests
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime

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
def get_varel_prediction(date_str):
    # Fetching Weather for Varel Harbor
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&hourly=temperature_2m,shortwave_radiation,visibility,cloud_cover&timezone=Europe%2FBerlin&start_date={date_str}&end_date={date_str}"
    res = requests.get(url).json()['hourly']
    
    times = pd.date_range(start=f"{date_str} 00:00", end=f"{date_str} 23:55", freq='5min')
    preds, clouds = [], []
    for i in range(len(res['time'])):
        # Model: Temp Coeff + Harbor Haze (18% penalty) + 60° SW Tilt
        t, irr, v, c = res['temperature_2m'][i], res['shortwave_radiation'][i], res['visibility'][i]/1000, res['cloud_cover'][i]
        f_temp = 1 + (-0.003 * (t - 25))
        f_haze = 0.82 if v < 12 else 1.0
        f_angle = 0.92 
        val = (irr / 1000) * SYSTEM_KWP * f_temp * f_haze * f_angle / 12
        for _ in range(12): preds.append(max(0, val)); clouds.append(c)
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
sync_msg = "🔴 Offline / API Lock"
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
    st.sidebar.warning(f"Growatt Sync Error: {e}")

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
    margin=dict(l=0, r=0, t=0, b=0),
    template="plotly_dark",
    barmode="overlay",
    height=400,
    legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.02,
        xanchor="right",
        x=1,
    ),
    dragmode="zoom",
    hovermode="x unified",
    title=f"Hafenstr. 18 Performance: {d_str}",
)

st.plotly_chart(
    fig,
    use_container_width=True,
    config={
        "scrollZoom": True,
        "displayModeBar": False,
        "responsive": True,
    },
)
