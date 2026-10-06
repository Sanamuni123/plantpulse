"""PlantPulse – AI Command Center for Factory Uptime.

Runs against Snowflake (tables/views from ../sql + Cortex COMPLETE) when a
[connections.snowflake] secret is configured, otherwise in offline demo mode.
"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import plantpulse_core as core

st.set_page_config(page_title="PlantPulse – Factory Uptime", page_icon="🏭", layout="wide")

CORTEX_MODEL = "mistral-large2"
RISK_COLORS = {"HIGH": "#d64545", "MEDIUM": "#e8a33d", "LOW": "#3a9d5d"}


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
def snowflake_configured() -> bool:
    try:
        return "snowflake" in st.secrets.get("connections", {})
    except Exception:
        return False


def _normalize(data: dict) -> dict:
    for df in data.values():
        df.columns = [c.lower() for c in df.columns]
    data["oee_daily"]["shift_date"] = pd.to_datetime(data["oee_daily"]["shift_date"])
    for key in ("scores", "maintenance"):
        data[key]["ts"] = pd.to_datetime(data[key]["ts"])
    return data


def load_snowflake() -> dict:
    conn = st.connection("snowflake")
    q = lambda sql: conn.query(sql, ttl=600)
    return _normalize(dict(
        machines=q("SELECT * FROM MACHINES"),
        oee_daily=q("SELECT * FROM OEE_DAILY"),
        scores=q("SELECT * FROM SENSOR_SCORES ORDER BY MACHINE_ID, TS"),
        risk=q("SELECT * FROM MACHINE_RISK ORDER BY RISK_SCORE DESC"),
        maintenance=q("SELECT * FROM MAINTENANCE_EVENTS"),
    ))


@st.cache_data(show_spinner="Generating demo plant data…")
def load_demo() -> dict:
    return core.build_demo_dataset()


def cortex_complete(prompt: str) -> str:
    cur = st.connection("snowflake").cursor()
    cur.execute("SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)", (CORTEX_MODEL, prompt))
    return cur.fetchone()[0]


LIVE = snowflake_configured()
try:
    data = load_snowflake() if LIVE else load_demo()
except Exception as exc:  # fall back so the public demo never breaks
    st.warning(f"Snowflake unavailable ({exc.__class__.__name__}); showing demo data.")
    LIVE, data = False, load_demo()

# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("## 🏭 PlantPulse")
    st.caption("AI Command Center for Factory Uptime")
    if LIVE:
        st.success("Connected to **Snowflake** · Copilot: **Cortex COMPLETE**")
    else:
        st.info("**Demo mode**: synthetic data generated in-app (same logic as the Snowflake SQL). "
                "Add Snowflake secrets to go live.")
    lines = st.multiselect("Production lines", list(core.LINES),
                           default=list(core.LINES), format_func=lambda l: f"{l} – {core.LINES[l]}")
    days = st.slider("OEE window (days)", 7, 30, 14)
    st.divider()
    st.caption("Built with Snowflake · Cortex AI · Cortex Code CLI · Streamlit")

lines = lines or list(core.LINES)
oee = data["oee_daily"]
oee = oee[oee.line.isin(lines) & (oee.shift_date > oee.shift_date.max() - pd.Timedelta(days=days))]
risk = data["risk"][data["risk"].line.isin(lines)]
scores = data["scores"]

# --------------------------------------------------------------------------- #
# Header KPIs
# --------------------------------------------------------------------------- #
st.title("PlantPulse – Factory Uptime Command Center")
st.caption(f"{len(risk)} machines · {len(lines)} lines · sensor data through "
           f"{scores.ts.max():%d %b %Y %H:%M}")


def weighted(df):
    a = df.run_min.sum() / df.planned_min.sum()
    p = min(df.ideal_run_min.sum() / df.run_min.sum(), 1)
    q = df.good_count.sum() / df.total_count.sum()
    return a, p, q, a * p * q


a, p, q, o = weighted(oee)
prev = data["oee_daily"]
prev = prev[prev.line.isin(lines) & (prev.shift_date <= oee.shift_date.min())
            & (prev.shift_date > oee.shift_date.min() - pd.Timedelta(days=days))]
o_prev = weighted(prev)[3] if len(prev) else None

k = st.columns(5)
k[0].metric("Plant OEE", f"{o:.1%}", f"{(o - o_prev) * 100:+.1f} pts" if o_prev else None,
            help="Availability × Performance × Quality. World-class ≈ 85%.")
k[1].metric("Availability", f"{a:.1%}")
k[2].metric("Performance", f"{p:.1%}")
k[3].metric("Quality", f"{q:.1%}")
n_high = int((risk.risk_level == "HIGH").sum())
k[4].metric("High-risk machines", n_high, f"{int((risk.risk_level == 'MEDIUM').sum())} medium",
            delta_color="off")

if n_high:
    names = ", ".join(risk[risk.risk_level == "HIGH"].machine_id)
    st.error(f"⚠️ **Predicted failure risk:** {names}. Vibration is trending toward the "
             f"{core.VIBRATION_LIMIT} mm/s limit. See *Machine Health* or ask the *AI Copilot*.")

tab_overview, tab_health, tab_copilot = st.tabs(["📊 Plant Overview", "🩺 Machine Health", "🤖 AI Copilot"])

# --------------------------------------------------------------------------- #
# Plant overview
# --------------------------------------------------------------------------- #
with tab_overview:
    c1, c2 = st.columns([3, 2])
    trend = (oee.groupby(["shift_date", "line"])
             .apply(lambda g: weighted(g)[3], include_groups=False).rename("oee").reset_index())
    trend["line"] = trend.line.map(lambda l: f"{l} – {core.LINES[l]}")
    fig = px.line(trend, x="shift_date", y="oee", color="line", markers=True,
                  labels={"shift_date": "", "oee": "OEE", "line": "Line"})
    fig.add_hline(y=0.85, line_dash="dot", annotation_text="World-class 85%", line_color="gray")
    fig.update_layout(yaxis_tickformat=".0%", height=380, margin=dict(t=30, b=10),
                      legend=dict(orientation="h", y=1.12), title="Daily OEE by line")
    c1.plotly_chart(fig, use_container_width=True)

    loss = pd.DataFrame({"Loss": ["Availability loss", "Performance loss", "Quality loss"],
                         "pts": [(1 - a) * 100, a * (1 - p) * 100, a * p * (1 - q) * 100]})
    fig = px.bar(loss, x="pts", y="Loss", orientation="h", text=loss.pts.map("{:.1f} pts".format),
                 color="Loss", color_discrete_sequence=["#d64545", "#e8a33d", "#6b7bd6"])
    fig.update_layout(showlegend=False, height=380, margin=dict(t=30, b=10),
                      title="Where OEE is lost", xaxis_title="OEE points lost", yaxis_title="")
    c2.plotly_chart(fig, use_container_width=True)

    heat = oee.pivot_table(index="machine_id", columns="shift_date", values="oee")
    fig = px.imshow(heat, color_continuous_scale="RdYlGn", zmin=0.5, zmax=0.9, aspect="auto",
                    labels=dict(color="OEE", x="", y=""))
    fig.update_xaxes(tickformat="%d %b")
    fig.update_layout(height=420, margin=dict(t=40, b=10), title="OEE heatmap · machine × day")
    st.plotly_chart(fig, use_container_width=True)

# --------------------------------------------------------------------------- #
# Machine health
# --------------------------------------------------------------------------- #
with tab_health:
    st.subheader("Failure-risk ranking")
    show = risk[["machine_id", "line", "machine_type", "risk_level", "risk_score",
                 "days_to_maintenance", "current_vibration", "vibration_trend_per_day",
                 "current_temperature"]].assign(anomaly_pct_24h=risk.anomaly_rate_24h * 100)
    st.dataframe(
        show.style.map(lambda v: f"color: {RISK_COLORS.get(v, 'inherit')}; font-weight: 600",
                       subset=["risk_level"]),
        hide_index=True, use_container_width=True,
        column_config={
            "risk_score": st.column_config.ProgressColumn("Risk score", min_value=0, max_value=100, format="%d"),
            "days_to_maintenance": st.column_config.NumberColumn("Days to limit", format="%.1f"),
            "current_vibration": st.column_config.NumberColumn("Vibration (mm/s)", format="%.2f"),
            "vibration_trend_per_day": st.column_config.NumberColumn("Trend /day", format="%+.2f"),
            "current_temperature": st.column_config.NumberColumn("Temp (°C)", format="%.1f"),
            "anomaly_pct_24h": st.column_config.NumberColumn("Anomalies 24h", format="%.0f%%"),
        })

    mid = st.selectbox("Inspect machine", risk.machine_id.tolist())
    g = scores[scores.machine_id == mid]
    anom = g[g.is_anomaly]
    c1, c2 = st.columns(2)
    for col, unit, box in (("vibration", "mm/s", c1), ("temperature", "°C", c2)):
        fig = go.Figure()
        fig.add_scatter(x=g.ts, y=g[col], mode="lines", name=col.title(), line=dict(color="#4c78a8"))
        fig.add_scatter(x=anom.ts, y=anom[col], mode="markers", name="Anomaly",
                        marker=dict(color="#d64545", size=7))
        if col == "vibration":
            fig.add_hline(y=core.VIBRATION_LIMIT, line_dash="dash", line_color="#d64545",
                          annotation_text="Alarm limit")
        fig.update_layout(title=f"{mid} · {col} ({unit})", height=340, margin=dict(t=40, b=10),
                          legend=dict(orientation="h", y=-0.15))
        box.plotly_chart(fig, use_container_width=True)

    events = data["maintenance"][data["maintenance"].machine_id == mid]
    st.markdown("**Maintenance history**")
    st.dataframe(events[["ts", "event_type", "downtime_min", "notes"]], hide_index=True,
                 use_container_width=True)

# --------------------------------------------------------------------------- #
# AI copilot
# --------------------------------------------------------------------------- #
with tab_copilot:
    st.subheader("Reliability Copilot")
    st.caption(f"Grounded in live plant data · engine: "
               f"{'Snowflake Cortex COMPLETE (' + CORTEX_MODEL + ')' if LIVE else 'offline rule engine (demo mode)'}")

    if "chat" not in st.session_state:
        st.session_state.chat = []

    suggestions = ["Which machines are at risk this week and why?",
                   "Draft work orders for the high-risk machines",
                   "Which machines have the worst OEE and what is the main loss?",
                   "Is the Welding line healthy?"]
    cols = st.columns(len(suggestions))
    clicked = next((s for c, s in zip(cols, suggestions) if c.button(s, use_container_width=True)), None)
    question = st.chat_input("Ask about uptime, risk, OEE or maintenance…") or clicked

    for role, msg in st.session_state.chat:
        st.chat_message(role).markdown(msg)

    if question:
        st.chat_message("user").markdown(question)
        with st.chat_message("assistant"), st.spinner("Analyzing plant data…"):
            if LIVE:
                try:
                    answer = cortex_complete(core.build_prompt(question, core.build_context(data)))
                except Exception as exc:
                    answer = (f"_Cortex call failed ({exc.__class__.__name__}); using offline engine._\n\n"
                              + core.offline_copilot(question, data))
            else:
                answer = core.offline_copilot(question, data)
            st.markdown(answer)
        st.session_state.chat += [("user", question), ("assistant", answer)]

    with st.expander("See the grounded context sent to the LLM"):
        st.code(core.build_context(data), language="text")
