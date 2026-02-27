import streamlit as st
import growattServer
import requests
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime
import io

# --- CONFIG ---
SYSTEM_KWP = 8.6
LAT, LON = 53.396, 8.136
GROWATT_TOKEN = "tb346b22pb1e34nhf057tcq48xkyc7aq"

st.set_page_config(page_title="Varel Solar Truth", layout="wide", page_icon="⚓")

# --- PHYSICS ENGINE (ALWAY AVAILABLE) ---
def get_varel_prediction(date_str):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&hourly=temperature_2m,shortwave_radiation,visibility,cloud_cover&timezone=Europe%2FBerlin&start_date={date_str}&end_date={date_str}"
    res = requests.get(url).json()['hourly']
    
    times = pd.date_range(start=f"{date_str} 00:00", end=f"{date_str} 23:55", freq='5min')
    preds, clouds = [], []
    for i in range(len(res['time'])):
        t, irr, v, c = res['temperature_2m'][i], res['shortwave_radiation'][i], res['visibility'][i]/1000, res['cloud_cover'][i]
        # Varel Harbor Physics: Temp Coeff + Haze + 60° SW Tilt
        f_temp = 1 + (-0.003 * (t - 25))
        f_haze = 0.82 if v < 12 else 1.0
        f_angle = 0.92 
        val = (irr / 1000) * SYSTEM_KWP * f_temp * f_haze * f_angle / 12
        for _ in range(12): preds.append(max(0, val)); clouds.append(c)
    return pd.DataFrame({"Time": times, "Predicted": preds, "Cloud_Cover": clouds})

# --- UI START ---
st.title("⚓ Solar Truth: Hafenstr. 18")
selected_date = st.date_input("Analysis Date", datetime.now().date())
d_str = selected_date.strftime("%Y-%m-%d")

# 1. GENERATE PREDICTION (ALWAYS WORKS)
df = get_varel_prediction(d_str)

# 2. ATTEMPT GROWATT SYNC
actuals = []
sync_status = "🔴 Offline / No Data"
last_seen = "N/A"

try:
    # Force European OpenAPI V1 Endpoint
    api = growattServer.OpenApiV1(token=GROWATT_TOKEN)
    api.server_url = "https://openapi.growatt.com/v1/" 
    
    # Try getting plant list using the V1 method
    plants_res = api.plant_list()
    if plants_res['data']['plants']:
        plant = plants_res['data']['plants'][0]
        pid = plant['plant_id']
        
        # Pull 5-minute history
        hist = api.plant_energy_history(pid, d_str, d_str, 'day', 1, 300)
        # Debug view of raw Growatt response to understand structure
        data_block = hist.get('data', {})
        st.sidebar.write("Growatt history keys:", list(hist.keys()))
        st.sidebar.write("Growatt data keys:", list(data_block.keys()))
        sample_energy = data_block.get('energy_data', [])[:3]
        st.sidebar.write("Sample energy_data[0:3]:", sample_energy)

        actuals = [float(x.get('energy', 0)) for x in data_block.get('energy_data', [])]
        
        if actuals:
            sync_status = "🟢 Online"
            last_seen = datetime.now().strftime("%H:%M:%S")
except Exception as e:
    st.sidebar.warning(f"Growatt Sync Delay: {e}")

# 3. DASHBOARD DISPLAY
with st.sidebar:
    st.subheader("System Status")
    st.write(f"Connection: {sync_status}")
    st.write(f"Last API Sync: {last_seen}")
    show_clouds = st.toggle("Enable Cloud Overlay", value=True)

c1, c2, c3 = st.columns(3)
c1.metric("Predicted Total", f"{df['Predicted'].sum():.2f} kWh")

if actuals:
    df = df.iloc[:len(actuals)]
    df['Actual'] = actuals
    df['Diff'] = df['Actual'] - df['Predicted']
    total_act = sum(actuals)
    c2.metric("Actual Today", f"{total_act:.2f} kWh")
    c3.metric("Health Score", f"{(total_act/df['Predicted'].sum()*100):.1f}%")

# --- HISTOGRAM ---
from plotly.subplots import make_subplots
fig = make_subplots(specs=[[{"secondary_y": True}]])
fig.add_trace(go.Bar(x=df['Time'], y=df['Predicted'], name='Predicted', marker_color='orange', opacity=0.3), secondary_y=False)

if actuals:
    df['Color'] = df.apply(lambda r: '#2ecc71' if r['Actual'] >= r['Predicted'] else '#e74c3c', axis=1)
    fig.add_trace(go.Bar(x=df['Time'], y=df['Actual'], name='Actual', marker_color=df['Color']), secondary_y=False)

if show_clouds:
    fig.add_trace(go.Scatter(x=df['Time'], y=df['Cloud_Cover'], name='Cloud %', line=dict(color='#3498db', dash='dot')), secondary_y=True)

fig.update_layout(template="plotly_dark", barmode='overlay', title=f"Yield Analysis for {d_str}")
st.plotly_chart(fig, use_container_width=True)

if actuals:
    st.subheader("📋 5-Minute Comparison Table")
    df_table = df[['Time', 'Actual', 'Predicted', 'Diff']].copy()
    df_table['Time'] = df_table['Time'].dt.strftime('%H:%M')
    st.dataframe(df_table.style.background_gradient(subset=['Diff'], cmap='RdYlGn'))
