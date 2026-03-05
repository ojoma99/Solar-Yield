import math
import os
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from urllib.parse import quote, urljoin

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Europe/Berlin")
except Exception:
    LOCAL_TZ = None


# ---------- Raw FSP-HA entities ----------
ENTITY_LIVE_POWER = "sensor.fsp0e3304v_system_power"
ENTITY_DAILY_YIELD = "sensor.fsp0e3304v_system_production_today"
ENTITY_LIFETIME_YIELD = "sensor.fsp0e3304v_lifetime_system_production"

# ---------- Physics model ----------
PMAX_W = 430
N_MODULES = 20
SYSTEM_KWP = (PMAX_W * N_MODULES) / 1000.0
LAT = 53.396
LON = 8.136
LAT_RAD = math.radians(LAT)
TILT_DEG = 60.0
AZIMUTH_PANEL = 225.0
ALBEDO = 0.38

# ---------- Colors ----------
ACTUAL_COLOR = "#FFFF00"      # Neon Yellow
PREDICTED_COLOR = "#39FF14"   # Electric Green


def _ha_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _to_float(value) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _is_valid_state(raw_state: str | None) -> bool:
    return raw_state not in (None, "", "unknown", "unavailable")


def _parse_ha_timestamp(ts: str | None):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=LOCAL_TZ) if LOCAL_TZ else dt
        return dt.astimezone(LOCAL_TZ) if LOCAL_TZ else dt.replace(tzinfo=None)
    except Exception:
        return None


def _to_local(dt: datetime) -> datetime:
    if LOCAL_TZ is None:
        return dt
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def _to_naive_local(dt: datetime) -> datetime:
    local = _to_local(dt)
    return local.replace(tzinfo=None) if local.tzinfo else local


@st.cache_data(ttl=20, show_spinner=False)
def ha_get_state(base_url: str, token: str, entity_id: str):
    url = urljoin(base_url.rstrip("/") + "/", f"api/states/{entity_id}")
    try:
        response = requests.get(url, headers=_ha_headers(token), timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception:
        return None


@st.cache_data(ttl=60, show_spinner=False)
def ha_get_history(base_url: str, token: str, entity_id: str, start_iso: str, end_iso: str):
    base = base_url.rstrip("/") + "/"
    url = urljoin(base, "api/history/period/" + quote(start_iso, safe=""))
    try:
        response = requests.get(
            url,
            headers=_ha_headers(token),
            params={"filter_entity_id": entity_id, "end_time": end_iso, "minimal_response": "1"},
            timeout=40,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def _state_to_power_kw(state: dict | None) -> float:
    if not state or not _is_valid_state(state.get("state")):
        return 0.0
    raw = _to_float(state.get("state"))
    unit = str((state.get("attributes") or {}).get("unit_of_measurement", "")).lower()
    if "kw" in unit:
        return max(0.0, raw)
    return max(0.0, raw / 1000.0)


def _state_to_energy_kwh(state: dict | None) -> float:
    if not state or not _is_valid_state(state.get("state")):
        return 0.0
    raw = _to_float(state.get("state"))
    unit = str((state.get("attributes") or {}).get("unit_of_measurement", "")).lower()
    if "wh" in unit and "kwh" not in unit:
        return max(0.0, raw / 1000.0)
    return max(0.0, raw)


def _history_numeric_df(history_payload: list) -> pd.DataFrame:
    rows = []
    for chunk in history_payload or []:
        if not isinstance(chunk, list):
            continue
        for event in chunk:
            if not isinstance(event, dict):
                continue
            state = event.get("state")
            if not _is_valid_state(state):
                continue
            ts = _parse_ha_timestamp(event.get("last_updated") or event.get("last_changed"))
            if ts is None:
                continue
            rows.append({"ts": ts, "value": _to_float(state)})
    if not rows:
        return pd.DataFrame(columns=["ts", "value"])
    df = pd.DataFrame(rows).sort_values("ts")
    return df


def _power_history_to_5min_df(
    history_payload: list,
    unit_hint: str,
    start_dt: datetime,
    end_dt: datetime,
) -> pd.DataFrame:
    events = _history_numeric_df(history_payload)
    base_index = pd.date_range(start=start_dt, end=end_dt, freq="5min", inclusive="left")
    if events.empty:
        return pd.DataFrame({"Time": base_index, "ActualPowerKW": 0.0, "ActualYieldKWh": 0.0})

    scale = 1.0 if "kw" in unit_hint.lower() else 0.001
    power_series = events.set_index("ts")["value"].astype(float) * scale
    power_series = power_series[~power_series.index.duplicated(keep="last")]
    power_series = power_series.clip(lower=0.0)
    boundary = pd.DatetimeIndex([start_dt])
    power_series = (
        power_series.reindex(power_series.index.union(boundary))
        .sort_index()
        .ffill()
        .fillna(0.0)
    )
    power_5min = (
        power_series.reindex(power_series.index.union(base_index))
        .sort_index()
        .ffill()
        .reindex(base_index)
        .fillna(0.0)
    )
    return pd.DataFrame(
        {
            "Time": [_to_naive_local(t.to_pydatetime()) for t in power_5min.index],
            "ActualPowerKW": power_5min.values,
            "ActualYieldKWh": power_5min.values * (5.0 / 60.0),
        }
    )


def _daily_sensor_history_to_day_df(
    history_payload: list,
    unit_hint: str,
    start_day: date,
    end_day: date,
) -> pd.DataFrame:
    events = _history_numeric_df(history_payload)
    day_index = pd.date_range(start=start_day, end=end_day, freq="D")
    if events.empty:
        return pd.DataFrame({"Day": day_index, "ActualYieldKWh": 0.0})

    scale = 1.0
    if "wh" in unit_hint.lower() and "kwh" not in unit_hint.lower():
        scale = 0.001
    events["day"] = pd.to_datetime(events["ts"]).dt.date
    day_max = events.groupby("day")["value"].max() * scale
    values = [float(max(0.0, day_max.get(d.date(), 0.0))) for d in day_index]
    return pd.DataFrame({"Day": day_index, "ActualYieldKWh": values})


def _solar_elevation_azimuth(dt_local_naive: datetime) -> tuple[float, float]:
    day_of_year = dt_local_naive.timetuple().tm_yday
    hour = dt_local_naive.hour + (dt_local_naive.minute / 60.0) + (dt_local_naive.second / 3600.0)
    decl_deg = 23.45 * math.sin(math.radians(360.0 * (284 + day_of_year) / 365.0))
    decl_rad = math.radians(decl_deg)
    hour_angle_deg = 15.0 * (12.25 - hour)
    hour_angle_rad = math.radians(hour_angle_deg)

    sin_elev = (
        math.sin(LAT_RAD) * math.sin(decl_rad)
        + math.cos(LAT_RAD) * math.cos(decl_rad) * math.cos(hour_angle_rad)
    )
    sin_elev = max(-1.0, min(1.0, sin_elev))
    elev_deg = math.degrees(math.asin(sin_elev))

    denom = math.cos(LAT_RAD) * max(1e-6, math.cos(math.asin(sin_elev)))
    cos_az = (math.sin(decl_rad) - math.sin(LAT_RAD) * sin_elev) / denom
    cos_az = max(-1.0, min(1.0, cos_az))
    azim_deg = math.degrees(math.acos(cos_az))
    if hour_angle_deg > 0:
        azim_deg = 360.0 - azim_deg
    return elev_deg, azim_deg


def _predicted_power_kw(dt_local_naive: datetime) -> float:
    elev_deg, azim_deg = _solar_elevation_azimuth(dt_local_naive)
    if elev_deg <= 0:
        return 0.0
    tilt_rad = math.radians(TILT_DEG)
    ground_view = (1.0 - math.cos(tilt_rad)) / 2.0
    sin_elev = max(0.01, math.sin(math.radians(elev_deg)))
    air_mass = 1.0 / sin_elev
    ghi_clear = 1361.0 * (0.7 ** min(air_mass, 5.0)) * sin_elev
    dni_clear = ghi_clear / sin_elev
    diffuse = max(0.0, ghi_clear - (dni_clear * sin_elev))
    incidence = math.acos(
        math.cos(math.radians(elev_deg)) * math.cos(tilt_rad)
        + math.sin(math.radians(elev_deg))
        * math.sin(tilt_rad)
        * math.cos(math.radians(azim_deg - AZIMUTH_PANEL))
    )
    cos_inc = max(0.0, math.cos(incidence))
    poa_wm2 = (
        dni_clear * cos_inc
        + diffuse * (1.0 + math.cos(tilt_rad)) / 2.0
        + ALBEDO * ghi_clear * ground_view
    )
    dc_kw = (poa_wm2 / 1000.0) * SYSTEM_KWP
    return max(0.0, min(dc_kw, SYSTEM_KWP * 1.1))


def _predicted_5min_df(start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    time_index = pd.date_range(start=start_dt, end=end_dt, freq="5min", inclusive="left")
    pred_power = []
    for ts in time_index:
        pred_power.append(_predicted_power_kw(_to_naive_local(ts.to_pydatetime())))
    pred_power_series = pd.Series(pred_power, index=time_index)
    return pd.DataFrame(
        {
            "Time": [_to_naive_local(t.to_pydatetime()) for t in pred_power_series.index],
            "PredictedPowerKW": pred_power_series.values,
            "PredictedYieldKWh": pred_power_series.values * (5.0 / 60.0),
        }
    )


def _day_range(d: date) -> tuple[datetime, datetime]:
    start = _to_local(datetime.combine(d, time.min))
    end = start + timedelta(days=1)
    return start, end


def _month_range(year: int, month: int) -> tuple[datetime, datetime]:
    start = _to_local(datetime(year, month, 1))
    if month == 12:
        end = _to_local(datetime(year + 1, 1, 1))
    else:
        end = _to_local(datetime(year, month + 1, 1))
    return start, end


def _year_range(year: int) -> tuple[datetime, datetime]:
    return _to_local(datetime(year, 1, 1)), _to_local(datetime(year + 1, 1, 1))


def _chart_layout(y_title: str, height: int = 360) -> dict:
    return dict(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=56, r=12, t=24, b=40),
        height=height,
        hovermode="x unified",
        dragmode="pan",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0.5, xanchor="center"),
        xaxis=dict(gridcolor="rgba(255,255,255,0.08)", fixedrange=False),
        yaxis=dict(title=y_title, gridcolor="rgba(255,255,255,0.08)", fixedrange=True),
    )


st.set_page_config(page_title="Abamu Residence Raw Monitor", layout="wide", page_icon="📈")

st.markdown(
    """
    <style>
    div[data-testid="column"] {
        width: 25% !important;
        flex: 1 1 25% !important;
        min-width: 25% !important;
    }
    .js-plotly-plot, [id^="plotly"] {
        touch-action: pan-y !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Abamu Residence — Raw Data Monitor")

with st.sidebar:
    st.subheader("Home Assistant")
    ha_url = st.text_input(
        "HA URL",
        value=st.session_state.get("ha_url", os.getenv("HA_URL", "http://YOUR_HA_IP:8123")),
        key="ha_url",
    ).strip()
    ha_token = st.text_input(
        "HA Token",
        value=st.session_state.get("ha_token", os.getenv("HA_TOKEN", "")),
        key="ha_token",
        type="password",
    ).strip()
    selected_date = st.date_input("Date", datetime.now().date())
    st.caption(f"Live: {ENTITY_LIVE_POWER}")
    st.caption(f"Daily: {ENTITY_DAILY_YIELD}")
    st.caption(f"Lifetime: {ENTITY_LIFETIME_YIELD}")

ha_ready = bool(ha_url and ha_token and "YOUR_HA_IP" not in ha_url.upper())

live_state = ha_get_state(ha_url, ha_token, ENTITY_LIVE_POWER) if ha_ready else None
daily_state = ha_get_state(ha_url, ha_token, ENTITY_DAILY_YIELD) if ha_ready else None
lifetime_state = ha_get_state(ha_url, ha_token, ENTITY_LIFETIME_YIELD) if ha_ready else None

if not ha_ready:
    st.warning("Enter Home Assistant URL and token to load FSP-HA raw values.")
elif not (live_state and daily_state and lifetime_state):
    st.warning("Unable to read one or more FSP-HA sensors. Check Home Assistant connectivity.")

live_power_kw = _state_to_power_kw(live_state)
daily_yield_kwh = _state_to_energy_kwh(daily_state)
lifetime_yield_kwh = _state_to_energy_kwh(lifetime_state)

day_start, day_end = _day_range(selected_date)
prediction_5min = _predicted_5min_df(day_start, day_end)
predicted_power_kw = _predicted_power_kw(_to_naive_local(datetime.now(LOCAL_TZ) if LOCAL_TZ else datetime.now()))
predicted_yield_kwh = float(prediction_5min["PredictedYieldKWh"].sum())

metric_a, metric_b, metric_c, metric_d = st.columns(4)
metric_a.metric("Live Power", f"{live_power_kw:.2f} kW")
metric_b.metric("Predicted Power", f"{predicted_power_kw:.2f} kW")
metric_c.metric("Daily Yield", f"{daily_yield_kwh:.2f} kWh")
metric_d.metric("Predicted Yield", f"{predicted_yield_kwh:.2f} kWh")

with st.sidebar:
    st.subheader("Raw Status")
    st.write(f"Lifetime Yield: {lifetime_yield_kwh:.2f} kWh")
    st.write(f"Month View: {selected_date.strftime('%B %Y')}")
    st.write(f"Year View: {selected_date.year}")

power_unit_hint = str((live_state or {}).get("attributes", {}).get("unit_of_measurement", "W"))
daily_unit_hint = str((daily_state or {}).get("attributes", {}).get("unit_of_measurement", "kWh"))

if ha_ready:
    day_history = ha_get_history(
        ha_url,
        ha_token,
        ENTITY_LIVE_POWER,
        day_start.isoformat(),
        day_end.isoformat(),
    )
    actual_5min = _power_history_to_5min_df(day_history, power_unit_hint, day_start, day_end)
else:
    actual_5min = pd.DataFrame({"Time": prediction_5min["Time"], "ActualPowerKW": 0.0, "ActualYieldKWh": 0.0})

hour_df = actual_5min.merge(prediction_5min, on="Time", how="outer").fillna(0.0)

tab_hour, tab_day, tab_month, tab_year = st.tabs(["Hour", "Day", "Month", "Year"])

with tab_hour:
    fig_hour = go.Figure()
    fig_hour.add_trace(
        go.Scatter(
            x=hour_df["Time"],
            y=hour_df["ActualPowerKW"],
            mode="lines",
            name="Actual",
            line=dict(color=ACTUAL_COLOR, width=2),
            hovertemplate="Actual: %{y:.3f} kW<extra></extra>",
        )
    )
    fig_hour.add_trace(
        go.Scatter(
            x=hour_df["Time"],
            y=hour_df["PredictedPowerKW"],
            mode="lines",
            name="Predicted 430Wp",
            line=dict(color=PREDICTED_COLOR, width=2),
            hovertemplate="Predicted: %{y:.3f} kW<extra></extra>",
        )
    )
    fig_hour.update_layout(**_chart_layout("Power (kW)", height=400))
    st.plotly_chart(
        fig_hour,
        use_container_width=True,
        config={"scrollZoom": True, "displayModeBar": False},
        key="hour_chart",
    )

with tab_day:
    day_actual = (
        hour_df.set_index("Time")["ActualYieldKWh"]
        .resample("1h")
        .sum()
        .rename("ActualYieldKWh")
    )
    day_pred = (
        hour_df.set_index("Time")["PredictedYieldKWh"]
        .resample("1h")
        .sum()
        .rename("PredictedYieldKWh")
    )
    day_chart = pd.concat([day_actual, day_pred], axis=1).fillna(0.0).reset_index()
    fig_day = go.Figure()
    fig_day.add_trace(
        go.Bar(
            x=day_chart["Time"],
            y=day_chart["ActualYieldKWh"],
            name="Actual",
            marker=dict(color=ACTUAL_COLOR),
            hovertemplate="Actual: %{y:.3f} kWh<extra></extra>",
        )
    )
    fig_day.add_trace(
        go.Bar(
            x=day_chart["Time"],
            y=day_chart["PredictedYieldKWh"],
            name="Predicted 430Wp",
            marker=dict(color=PREDICTED_COLOR),
            hovertemplate="Predicted: %{y:.3f} kWh<extra></extra>",
        )
    )
    fig_day.update_layout(**_chart_layout("Yield (kWh)"))
    fig_day.update_layout(barmode="group")
    st.plotly_chart(
        fig_day,
        use_container_width=True,
        config={"scrollZoom": True, "displayModeBar": False},
        key="day_chart",
    )

month_start, month_end = _month_range(selected_date.year, selected_date.month)
month_start_date = month_start.date()
month_end_date = (month_end - timedelta(days=1)).date()
if ha_ready:
    month_history = ha_get_history(
        ha_url,
        ha_token,
        ENTITY_DAILY_YIELD,
        month_start.isoformat(),
        month_end.isoformat(),
    )
    month_actual = _daily_sensor_history_to_day_df(
        month_history,
        daily_unit_hint,
        month_start_date,
        month_end_date,
    )
else:
    month_actual = pd.DataFrame(
        {
            "Day": pd.date_range(month_start_date, month_end_date, freq="D"),
            "ActualYieldKWh": 0.0,
        }
    )

month_pred_values = []
for day_value in month_actual["Day"]:
    d0 = day_value.date()
    d_start, d_end = _day_range(d0)
    month_pred_values.append(float(_predicted_5min_df(d_start, d_end)["PredictedYieldKWh"].sum()))
month_chart = month_actual.copy()
month_chart["PredictedYieldKWh"] = month_pred_values

with tab_month:
    fig_month = go.Figure()
    fig_month.add_trace(
        go.Bar(
            x=month_chart["Day"],
            y=month_chart["ActualYieldKWh"],
            name="Actual",
            marker=dict(color=ACTUAL_COLOR),
            hovertemplate="Actual: %{y:.3f} kWh<extra></extra>",
        )
    )
    fig_month.add_trace(
        go.Bar(
            x=month_chart["Day"],
            y=month_chart["PredictedYieldKWh"],
            name="Predicted 430Wp",
            marker=dict(color=PREDICTED_COLOR),
            hovertemplate="Predicted: %{y:.3f} kWh<extra></extra>",
        )
    )
    fig_month.update_layout(**_chart_layout("Yield (kWh)"))
    fig_month.update_layout(barmode="group")
    st.plotly_chart(
        fig_month,
        use_container_width=True,
        config={"scrollZoom": True, "displayModeBar": False},
        key="month_chart",
    )

year_start, year_end = _year_range(selected_date.year)
year_start_date = year_start.date()
year_end_date = (year_end - timedelta(days=1)).date()
if ha_ready:
    year_history = ha_get_history(
        ha_url,
        ha_token,
        ENTITY_DAILY_YIELD,
        year_start.isoformat(),
        year_end.isoformat(),
    )
    year_daily = _daily_sensor_history_to_day_df(
        year_history,
        daily_unit_hint,
        year_start_date,
        year_end_date,
    )
else:
    year_daily = pd.DataFrame(
        {
            "Day": pd.date_range(year_start_date, year_end_date, freq="D"),
            "ActualYieldKWh": 0.0,
        }
    )

pred_year_daily = []
for day_value in year_daily["Day"]:
    d0 = day_value.date()
    d_start, d_end = _day_range(d0)
    pred_year_daily.append(float(_predicted_5min_df(d_start, d_end)["PredictedYieldKWh"].sum()))
year_daily["PredictedYieldKWh"] = pred_year_daily
year_daily["Month"] = pd.to_datetime(year_daily["Day"]).dt.month
year_month = (
    year_daily.groupby("Month")[["ActualYieldKWh", "PredictedYieldKWh"]]
    .sum()
    .reindex(range(1, 13), fill_value=0.0)
    .reset_index()
)
year_month["Label"] = [datetime(selected_date.year, m, 1).strftime("%b") for m in year_month["Month"]]

with tab_year:
    fig_year = go.Figure()
    fig_year.add_trace(
        go.Bar(
            x=year_month["Label"],
            y=year_month["ActualYieldKWh"],
            name="Actual",
            marker=dict(color=ACTUAL_COLOR),
            hovertemplate="Actual: %{y:.3f} kWh<extra></extra>",
        )
    )
    fig_year.add_trace(
        go.Bar(
            x=year_month["Label"],
            y=year_month["PredictedYieldKWh"],
            name="Predicted 430Wp",
            marker=dict(color=PREDICTED_COLOR),
            hovertemplate="Predicted: %{y:.3f} kWh<extra></extra>",
        )
    )
    fig_year.update_layout(**_chart_layout("Yield (kWh)"))
    fig_year.update_layout(barmode="group")
    st.plotly_chart(
        fig_year,
        use_container_width=True,
        config={"scrollZoom": True, "displayModeBar": False},
        key="year_chart",
    )
