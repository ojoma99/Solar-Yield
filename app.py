import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import requests

# 1. SETUP & ENVIRONMENT
load_dotenv()
HA_URL = os.getenv("HA_URL")
HA_TOKEN = os.getenv("HA_TOKEN")
# Using the internal wattage sensor for maximum accuracy
HA_POWER_ENTITY = "sensor.fsp0e3304v_internal_wattage"
HA_TODAY_ENTITY = "sensor.fsp0e3304v_energy_today"

st.set_page_config(page_title="Abamu Sovereign Solar", layout="wide", initial_sidebar_state="collapsed")

# 2. DATA FETCHING (SOVEREIGN BRIDGE)
def get_ha_state(entity_id):
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    try:
        response = requests.get(f"{HA_URL}/api/states/{entity_id}", headers=headers, timeout=5)
        return response.json().get('state')
    except:
        return "0"

def get_ha_history(entity_id, hours=24):
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    start_time = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        url = f"{HA_URL}/api/history/period/{start_time}?filter_entity_id={entity_id}"
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()[0]
        df_hist = pd.DataFrame(data)
        df_hist['time'] = pd.to_datetime(df_hist['last_changed']).dt.tz_localize(None)
        df_hist['Actual'] = pd.to_numeric(df_hist['state'], errors='coerce').fillna(0)
        # Resample to 15-min bins to match prediction frequency
        return df_hist.set_index('time')['Actual'].resample('15Min').mean().reset_index()
    except:
        return pd.DataFrame(columns=['time', 'Actual'])

# 3. THE CALIBRATION ENGINE (VAREL 225° SW)
def calculate_physics_prediction(timestamp):
    # This is where your 8.6kWp math lives. 
    # For now, we use a standard bell curve adjusted for your 225 SW orientation.
    hour = timestamp.hour + timestamp.minute / 60
    
    # SHIFT: Peak production for 225° SW is around 14:15 (2.25 hours after solar noon)
    peak_hour = 14.25
    width = 3.5 # Spread of the sun across the panels
    
    # PHYSICS: Clear Sky Potential for 8.6kWp in March
    potential = 5.8 * (2.718 ** -(((hour - peak_hour) ** 2) / (2 * (width ** 2))))
    
    # HORIZON MASK: Before 10:30 AM, Varel horizon/roof shading cuts yield by 70%
    if hour < 10.5:
        return potential * 0.3
    # SYSTEM LOSS: 12% for cabling, heat, and dust
    return potential * 0.88

# 4. DASHBOARD UI
st.title("⚓ Abamu Sovereign Solar")

# Real-time HUD base values
live_val = get_ha_state(HA_POWER_ENTITY)
try:
    live_kw = float(live_val) / 1000 if float(live_val) > 10 else 0.0
except Exception:
    live_kw = 0.0

today_val = get_ha_state(HA_TODAY_ENTITY)
today_kwh = 0.0
try:
    today_kwh = float(today_val)
except Exception:
    today_kwh = 0.0

# 5. THE CHART (HOUR VIEW) + PHYSICS PREDICTIONS
# Generate Prediction Time Slots
times = pd.date_range(
    start=datetime.now().replace(hour=5, minute=0),
    end=datetime.now().replace(hour=20, minute=0),
    freq='15Min',
)
df = pd.DataFrame({'time': times})
df['Predicted'] = df['time'].apply(calculate_physics_prediction)

# Physics-based live and daily predictions for HUD
live_predicted_kw = calculate_physics_prediction(datetime.now())
today_predicted_kwh = float(df['Predicted'].sum() * (15.0 / 60.0))

# 6. Fetch and Merge Actuals for the chart
history = get_ha_history(HA_POWER_ENTITY, hours=12)

# Ensure the 'Actual' column exists in df even if history is empty
if not history.empty:
    df['time'] = pd.to_datetime(df['time']).dt.tz_localize(None)
    history['time'] = pd.to_datetime(history['time']).dt.tz_localize(None)

    # Sort both for merge_asof
    df = df.sort_values('time')
    history = history.sort_values('time')

    df = pd.merge_asof(df, history, on='time', direction='nearest')
    df['Actual'] = df['Actual'] / 1000  # Convert W to kW
else:
    # Fallback if no history is found
    df['Actual'] = 0.0

# Ensure no NaNs are left to break the max() calculation
df['Actual'] = df['Actual'].fillna(0.0)

# Get the current time in the same format as your dataframe (timezone-naive)
now = pd.Timestamp.now().replace(tzinfo=None)

# Filter the dataframe to only include rows up to right now
df_current = df[df['time'] <= now].copy()

# 7. HUD LAYOUT (two rows, high-density)
# Row 1: Instantaneous Power
col1, col2 = st.columns(2)
col1.metric("Live", f"{live_kw:.2f} kW")
col2.metric("Live Predicted", f"{live_predicted_kw:.2f} kW")

# Row 2: Today's Accumulation
col3, col4 = st.columns(2)
col3.metric("Today", f"{today_kwh:.2f} kWh")
col4.metric("Today Predicted", f"{today_predicted_kwh:.2f} kWh")

# Plotting
fig = go.Figure()
# Plotly has no go.Area trace type; use Scatter with fill='tozeroy' for the predicted band
fig.add_trace(
    go.Scatter(
        x=df['time'],
        y=df['Predicted'],
        name="Predicted",
        mode="lines",
        fill="tozeroy",
        line=dict(color="#00FF41"),
        opacity=0.3,
    )
)

# Only show Actual bars for timestamps up to \"now\" to avoid future projections from HA history
now_ts = datetime.now()
actual_masked = df['Actual'].where(df['time'] <= now_ts, 0.0)
fig.add_trace(go.Bar(x=df_current['time'], y=df_current['Actual'], name="Actual", marker_color='#FFFF00'))

# DYNAMIC SCALING: Zoom to the highest data point + 10%
max_y = max(df['Predicted'].max(), df_current['Actual'].max() if not df_current.empty else 0.0, 0.5)
fig.update_layout(
    template="plotly_dark",
    yaxis=dict(range=[0, max_y * 1.1], title="kW"),
    # Extra top and bottom margin so modebar and legend never overlap HUD or bars
    margin=dict(l=20, r=20, t=40, b=80),
    height=450,
    # Move legend below the chart to avoid clashing with modebar
    legend=dict(
        orientation="h",
        yanchor="top",
        y=-0.2,
        xanchor="right",
        x=1,
    ),
    # Enable smooth zooming/panning without overlapping UI
    dragmode="pan",
    hovermode="x unified",
)

st.plotly_chart(fig, use_container_width=True, config={'displayModeBar': True, 'scrollZoom': True})