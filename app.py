import streamlit as st
import growattServer
import requests
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime

# --- CONFIG ---
SYSTEM_KWP = 8.6
LAT, LON = 53.396, 8.136
GROWATT_TOKEN = "tb346b22pb1e34nhf057tcq48xkyc7aq"

st.set_page_config(page_title="Varel Solar Truth", layout="wide", page_icon="⚓")

# --- PHYSICS ENGINE (ALWAYS RUNS) ---
def get_varel_data(date_str):
    url = f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}&hourly=temperature_2m,shortwave_radiation,visibility,cloud_cover&timezone=Europe%2FBerlin&start_date={date_str}&end_date={date_str}"
    res = requests.get(url).json()['hourly']
    
    times = pd.date_range(start=f"{date_str} 00:00", end=f"{date_str} 23:55", freq='5min')
    preds, clouds = [], []
    for i in range(len(res['time'])):
        t, irr, v, c = res['temperature_2m'][i], res['shortwave_radiation'][i], res['visibility'][i]/1000, res['cloud_cover'][i]
        # Physics: Temp Coeff (-0.30%) + Harbor Haze (18% penalty if <12km) + 60° Tilt
        f_temp = 1 + (-0.003 * (t - 25))
        f_haze = 0.82 if v < 12 else 1.0
        f_angle = 0.92 
        val = (irr / 1000) * SYSTEM_KWP * f_temp * f_haze * f_angle / 12
        for _ in range(12): preds.append(max(0, val)); clouds.append(c)
    return pd.DataFrame({"Time": times, "Predicted": preds, "Cloud_Cover": clouds})

# --- UI LOGIC ---
st.title("⚓ Hafenstr. 18: Solar Truth Dashboard")
d = st.date_input("Analysis Date", datetime.now().date())
d_str = d.strftime("%Y-%m-%d")

# Step 1: ALWAYS GET PREDICTION
df = get_varel_data(d_str)
total_pre = df['Predicted'].sum()

# Step 2: TRY TO GET GROWATT ACTUALS
actuals = []
sync_success = False

try:
    api = growattServer.OpenApiV1(token=GROWATT_TOKEN)
    api.server_url = "https://openapi.growatt.com/v1/" # EU Server
    p_res = api.plant_list()
    pid = p_res['data']['plants'][0]['plant_id']
    hist = api.plant_energy_history(pid, d_str, d_str, 'day', 1, 300)
    actuals = [float(x.get('energy', 0)) for x in hist['data']['energy_data']]
    sync_success = True
except Exception:
    st.sidebar.error("🔌 Inverter Offline: Showing Predicted Yield Only")

# Step 3: DISPLAY DATA
c1, c2, c3 = st.columns(3)
c1.metric("Predicted Today", f"{total_pre:.2f} kWh")

if sync_success and len(actuals) > 0:
    df = df.iloc[:len(actuals)]
    df['Actual'] = actuals
    df['Diff'] = df['Actual'] - df['Predicted']
    total_act = df['Actual'].sum()
    c2.metric("Actual Today", f"{total_act:.2f} kWh")
    c3.metric("Health Score", f"{(total_act/total_pre*100):.1f}%")
else:
    c2.write("Actual: N/A")
    c3.write("Health: N/A")

# --- HISTOGRAM ---
fig = go.Figure()
fig.add_trace(go.Bar(x=df['Time'], y=df['Predicted'], name='Predicted Potential', marker_color='orange', opacity=0.4))

if sync_success and 'Actual' in df:
    df['Color'] = df.apply(lambda r: '#2ecc71' if r['Actual'] >= r['Predicted'] else '#e74c3c', axis=1)
    fig.add_trace(go.Bar(x=df['Time'], y=df['Actual'], name='Actual Yield', marker_color=df['Color']))

fig.update_layout(template="plotly_dark", barmode='overlay', title=f"Yield Profile: {d_str}")
st.plotly_chart(fig, use_container_width=True)
