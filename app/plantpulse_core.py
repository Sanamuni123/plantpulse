"""PlantPulse core: demo data generation + analytics.

Every function here mirrors a SQL object in ../sql so the app behaves the same
whether it reads from Snowflake (views) or runs in offline demo mode (pandas).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LINES = {"L1": "Stamping", "L2": "Welding", "L3": "Assembly"}
MACHINE_TYPES = {"L1": "Hydraulic Press", "L2": "Robot Welder", "L3": "CNC Mill"}
AT_RISK = {"M-L1-03", "M-L3-04"}          # degrading right now (last 72h)
PAST_FAILURE = "M-L2-02"                  # failed 15 days ago, preceded by a 72h ramp
EARLY_WEAR = "M-L2-04"                    # mild wear starting 5 days ago -> MEDIUM risk
HOURS = 720                               # 30 days of hourly sensor readings
DAYS = 30
VIBRATION_LIMIT = 6.5                     # mm/s – ISO 10816 style alarm threshold
SHIFTS = {"A": 0, "B": 8, "C": 16}        # shift start hour offsets


# --------------------------------------------------------------------------- #
# Synthetic data  (mirrors sql/02_synthetic_data.sql)
# --------------------------------------------------------------------------- #
def degradation(machine_id: str, hours_ago) -> np.ndarray:
    """0 = healthy, 1 = about to fail."""
    h = np.asarray(hours_ago, dtype=float)
    if machine_id in AT_RISK:
        return np.clip(1 - h / 72.0, 0, 1)
    if machine_id == PAST_FAILURE:
        return np.where((h >= 360) & (h <= 432), (432 - h) / 72.0, 0.0)
    if machine_id == EARLY_WEAR:
        return 0.25 * np.clip(1 - h / 120.0, 0, 1)
    return np.zeros_like(h)


def make_machines() -> pd.DataFrame:
    rows = []
    for li, line in enumerate(LINES):
        for i in range(1, 5):
            rows.append(dict(
                machine_id=f"M-{line}-{i:02d}",
                line=line,
                line_name=LINES[line],
                machine_type=MACHINE_TYPES[line],
                install_date=pd.Timestamp("2019-01-01") + pd.Timedelta(days=150 * (li * 4 + i)),
                ideal_cycle_time_s=30 + 5 * i + 10 * li,
            ))
    return pd.DataFrame(rows)


def make_sensor_readings(machines: pd.DataFrame, now: pd.Timestamp, rng) -> pd.DataFrame:
    h = np.arange(HOURS)
    ts = now - pd.to_timedelta(h, unit="h")
    frames = []
    for mid in machines.machine_id:
        d, n = degradation(mid, h), len(h)
        frames.append(pd.DataFrame(dict(
            machine_id=mid, ts=ts,
            temperature=65 + 15 * d + rng.normal(0, 1.5, n),
            vibration=2.0 + 4 * d + rng.normal(0, 0.25, n),
            pressure=6.0 - 1.0 * d + rng.normal(0, 0.15, n),
            rpm=1500 - 120 * d + rng.normal(0, 20, n),
        )))
    return pd.concat(frames, ignore_index=True).sort_values(["machine_id", "ts"], ignore_index=True)


def make_production_log(machines: pd.DataFrame, now: pd.Timestamp, rng) -> pd.DataFrame:
    rows = []
    today = now.normalize()
    for m in machines.itertuples():
        for day in range(DAYS):
            for shift, offset in SHIFTS.items():
                hours_ago = day * 24 + (24 - offset - 4)
                d = float(degradation(m.machine_id, hours_ago))
                downtime = rng.uniform(10, 45) + 120 * d
                if m.machine_id == PAST_FAILURE and day == 15 and shift == "A":
                    downtime += 240
                run_min = 480 - downtime
                perf = rng.uniform(0.82, 0.95) - 0.15 * d
                total = int(run_min * 60 / m.ideal_cycle_time_s * perf)
                good = int(total * (rng.uniform(0.96, 0.995) - 0.06 * d))
                rows.append(dict(machine_id=m.machine_id, shift_date=today - pd.Timedelta(days=day),
                                 shift=shift, planned_min=480.0, run_min=round(run_min, 1),
                                 total_count=total, good_count=good))
    return pd.DataFrame(rows)


def make_maintenance_events(machines: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for i, mid in enumerate(machines.machine_id):
        rows.append(dict(machine_id=mid, ts=now - pd.Timedelta(days=5 + (i * 2) % 20, hours=3),
                         event_type="PLANNED", downtime_min=60,
                         notes="Scheduled preventive maintenance: lubrication, belt and filter check."))
    rows.append(dict(machine_id=PAST_FAILURE, ts=now - pd.Timedelta(hours=360), event_type="FAILURE",
                     downtime_min=240,
                     notes="Unplanned stop: wire-feed motor bearing seized. Vibration and temperature "
                           "rose for ~48h before failure. Bearing replaced."))
    return pd.DataFrame(rows).sort_values("ts", ignore_index=True)


def build_demo_dataset(seed: int = 42, now: pd.Timestamp | None = None) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    now = (now or pd.Timestamp.now()).floor("h")
    machines = make_machines()
    sensors = make_sensor_readings(machines, now, rng)
    production = make_production_log(machines, now, rng)
    maintenance = make_maintenance_events(machines, now)
    scores = sensor_scores(sensors)
    return dict(
        machines=machines,
        oee_daily=oee_daily(production, machines),
        scores=scores,
        risk=machine_risk(scores, machines),
        maintenance=maintenance,
    )


# --------------------------------------------------------------------------- #
# Analytics  (mirrors sql/03_views.sql)
# --------------------------------------------------------------------------- #
def oee_daily(production: pd.DataFrame, machines: pd.DataFrame) -> pd.DataFrame:
    """OEE = Availability x Performance x Quality, aggregated per machine per day."""
    p = production.merge(machines[["machine_id", "line", "ideal_cycle_time_s"]], on="machine_id")
    p["ideal_run_min"] = p.total_count * p.ideal_cycle_time_s / 60.0
    g = p.groupby(["machine_id", "line", "shift_date"], as_index=False).agg(
        planned_min=("planned_min", "sum"), run_min=("run_min", "sum"),
        ideal_run_min=("ideal_run_min", "sum"), total_count=("total_count", "sum"),
        good_count=("good_count", "sum"))
    g["availability"] = g.run_min / g.planned_min
    g["performance"] = (g.ideal_run_min / g.run_min).clip(upper=1)
    g["quality"] = g.good_count / g.total_count
    g["oee"] = g.availability * g.performance * g.quality
    return g


def sensor_scores(sensors: pd.DataFrame) -> pd.DataFrame:
    """Rolling z-score vs. a 7-day baseline that ends 24h before each reading."""
    s = sensors.sort_values(["machine_id", "ts"]).copy()
    for col in ("vibration", "temperature"):
        grp = s.groupby("machine_id")[col]
        mean = grp.transform(lambda x: x.shift(24).rolling(168, min_periods=48).mean())
        std = grp.transform(lambda x: x.shift(24).rolling(168, min_periods=48).std())
        s[f"z_{col}"] = (s[col] - mean) / std
    s["is_anomaly"] = (s.z_vibration.abs() > 3) | (s.z_temperature.abs() > 3)
    return s


def machine_risk(scores: pd.DataFrame, machines: pd.DataFrame) -> pd.DataFrame:
    latest = scores.ts.max()
    rows = []
    for mid, g in scores.groupby("machine_id"):
        last24 = g[g.ts > latest - pd.Timedelta(hours=24)]
        last72 = g[g.ts > latest - pd.Timedelta(hours=72)]
        x_days = (last72.ts - latest).dt.total_seconds() / 86400
        slope = float(np.polyfit(x_days, last72.vibration, 1)[0])
        cur = g.iloc[-1]
        avg_zv = float(last24.z_vibration.mean())
        avg_zt = float(last24.z_temperature.mean())
        anomaly_rate = float(last24.is_anomaly.mean())
        score = 100 * (0.5 * np.clip(max(avg_zv, 0) / 4, 0, 1)
                       + 0.3 * np.clip(max(avg_zt, 0) / 4, 0, 1)
                       + 0.2 * anomaly_rate)
        days = np.clip((VIBRATION_LIMIT - cur.vibration) / slope, 0, 30) if slope > 0.05 else 30.0
        rows.append(dict(machine_id=mid, current_vibration=round(cur.vibration, 2),
                         current_temperature=round(cur.temperature, 1),
                         vibration_trend_per_day=round(slope, 2), avg_z_vibration=round(avg_zv, 2),
                         avg_z_temperature=round(avg_zt, 2), anomaly_rate_24h=round(anomaly_rate, 2),
                         risk_score=int(round(score)), days_to_maintenance=round(float(days), 1)))
    r = pd.DataFrame(rows).merge(machines[["machine_id", "line", "machine_type"]], on="machine_id")
    r["risk_level"] = np.select([r.risk_score >= 70, r.risk_score >= 40], ["HIGH", "MEDIUM"], "LOW")
    return r.sort_values("risk_score", ascending=False, ignore_index=True)


# --------------------------------------------------------------------------- #
# Copilot helpers
# --------------------------------------------------------------------------- #
def build_context(data: dict[str, pd.DataFrame]) -> str:
    """Compact, grounded facts handed to the LLM (Cortex COMPLETE)."""
    risk = data["risk"]
    oee = data["oee_daily"]
    recent = oee[oee.shift_date > oee.shift_date.max() - pd.Timedelta(days=7)]
    line_oee = recent.groupby("line")[["availability", "performance", "quality", "oee"]].mean().round(3)
    mach_oee = recent.groupby("machine_id").oee.mean().round(3).sort_values().head(5)
    maint = data["maintenance"].sort_values("ts", ascending=False).head(6)
    return (
        "MACHINE RISK (last 24h, risk_score 0-100, vibration in mm/s, limit "
        f"{VIBRATION_LIMIT}):\n{risk.drop(columns=['avg_z_temperature']).to_csv(index=False)}\n"
        f"LINE OEE (7-day avg):\n{line_oee.to_csv()}\n"
        f"LOWEST OEE MACHINES (7-day avg):\n{mach_oee.to_csv()}\n"
        f"RECENT MAINTENANCE:\n{maint[['machine_id', 'ts', 'event_type', 'notes']].to_csv(index=False)}"
    )


def build_prompt(question: str, context: str) -> str:
    return (
        "You are PlantPulse, a reliability-engineering copilot for a manufacturing plant. "
        "Answer ONLY from the data below; cite machine IDs and numbers. Be concise and use "
        "bullet points. When a machine is HIGH risk, include a short maintenance work order "
        "(asset, priority, suspected cause, actions, parts, estimated downtime).\n\n"
        f"DATA:\n{context}\nQUESTION: {question}"
    )


def work_order(row) -> str:
    cause = ("bearing wear / misalignment (vibration rising "
             f"{row.vibration_trend_per_day} mm/s per day)")
    if row.avg_z_temperature > 3:
        cause += ", lubrication breakdown (temperature elevated)"
    return (
        f"**WORK ORDER – {row.machine_id} ({row.machine_type}, line {row.line})**\n"
        f"- Priority: {'P1 – within 24h' if row.risk_level == 'HIGH' else 'P2 – this week'}\n"
        f"- Trigger: risk score {row.risk_score}/100, vibration {row.current_vibration} mm/s "
        f"(limit {VIBRATION_LIMIT}), est. {row.days_to_maintenance} days to limit\n"
        f"- Suspected cause: {cause}\n"
        "- Actions: lock-out/tag-out → inspect & replace drive-end bearing → check shaft alignment "
        "→ re-lubricate → 30-min test run with vibration probe\n"
        "- Parts: bearing kit, grease cartridge\n- Estimated downtime: 90–120 min "
        "(schedule at shift change to protect OEE)"
    )


def offline_copilot(question: str, data: dict[str, pd.DataFrame]) -> str:
    """Rule-based fallback used when Snowflake Cortex is not configured."""
    q = question.lower()
    risk = data["risk"]
    oee = data["oee_daily"]
    recent = oee[oee.shift_date > oee.shift_date.max() - pd.Timedelta(days=7)]
    for line in LINES:
        if line.lower() in q or LINES[line].lower() in q:
            risk = risk[risk.line == line]
            recent = recent[recent.line == line]
    at_risk = risk[risk.risk_level != "LOW"]

    if "oee" in q or "efficien" in q or "worst" in q:
        by_m = recent.groupby("machine_id")[["availability", "performance", "quality", "oee"]].mean()
        worst = by_m.sort_values("oee").head(3)
        lines = [f"- **{m}**: OEE {r.oee:.1%} (A {r.availability:.1%} · P {r.performance:.1%} · "
                 f"Q {r.quality:.1%})" for m, r in worst.iterrows()]
        weakest = worst.iloc[0][["availability", "performance", "quality"]].astype(float).idxmin()
        return ("**Lowest OEE machines, last 7 days:**\n" + "\n".join(lines) +
                f"\n\nBiggest loss driver on {worst.index[0]}: **{weakest}**. "
                "Fixing the high-risk machines first will recover availability.")

    if at_risk.empty:
        return "✅ No machines are at medium or high risk right now. All vibration trends are stable."

    parts = ["**Machines needing attention:**"]
    for r in at_risk.itertuples():
        parts.append(f"- **{r.machine_id}** ({r.machine_type}, {r.line}) – {r.risk_level} risk "
                     f"{r.risk_score}/100: vibration {r.current_vibration} mm/s, trending "
                     f"+{r.vibration_trend_per_day}/day, {r.anomaly_rate_24h:.0%} of last-24h readings "
                     f"anomalous → ~{r.days_to_maintenance} days to limit.")
    if "work order" in q or "plan" in q or "what should" in q or "fix" in q:
        parts += ["", *[work_order(r) for r in at_risk.itertuples()]]
    else:
        parts.append("\nAsk *“draft work orders”* to get a maintenance plan.")
    return "\n".join(parts)
