"""Apple Health MCP server.

Exposes Apple Health data (CSV exports written by the ingest service) to any
MCP client over Streamable HTTP. The bearer token is embedded in the URL path
because Claude's connector UI does not currently support custom auth headers.
"""
import io
import json
import os
import pathlib
import re
from datetime import datetime, timedelta

import pandas as pd
import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(os.environ.get("DOTENV_PATH", "/opt/health/.env"))
TOKEN = os.getenv("HEALTH_TOKEN")
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/opt/health/data"))
REPORTS_DIR = pathlib.Path(os.getenv("REPORTS_DIR", "/opt/health/reports"))

_MAX_REPORT_BYTES = 200_000
_REPORT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

mcp = FastMCP("apple-health", stateless_http=True)
mcp.settings.transport_security.enable_dns_rebinding_protection = False


def _dir_signature() -> tuple:
    """Cheap fingerprint of the data directory.

    Each tool call recomputes this and compares against the cached signature;
    a single ``stat`` per file is far cheaper than re-reading every CSV. The
    fingerprint changes when a file is added, removed, or rewritten.
    """
    return tuple(
        (f.name, f.stat().st_mtime_ns, f.stat().st_size)
        for f in sorted(DATA_DIR.glob("health_*.csv"))
    )


_cache: tuple[tuple, pd.DataFrame] | None = None


def _read_csv_tolerant(path: pathlib.Path) -> pd.DataFrame:
    """Read a CSV that may still carry a multipart MIME envelope.

    Files saved before the ingest service stripped the envelope start with
    ``--Boundary...`` followed by Content-Disposition / Content-Type headers,
    a blank line, the actual CSV, and finally a closing ``--Boundary--``
    marker. We detect that prefix and slice it off so pandas only sees CSV.
    """
    raw = path.read_bytes()
    if raw.startswith(b"--"):
        _, _, tail = raw.partition(b"\r\n\r\n")
        if not tail:
            _, _, tail = raw.partition(b"\n\n")
        if tail:
            cut = tail.rfind(b"\r\n--")
            if cut == -1:
                cut = tail.rfind(b"\n--")
            if cut != -1:
                tail = tail[:cut]
            raw = tail.strip()
    return pd.read_csv(io.BytesIO(raw), parse_dates=["Date/Time"])


def load_all_csv() -> pd.DataFrame:
    global _cache
    sig = _dir_signature()
    if _cache is not None and _cache[0] == sig:
        return _cache[1]
    if not sig:
        _cache = (sig, pd.DataFrame())
        return _cache[1]
    dfs = []
    for name, _mtime, _size in sig:
        f = DATA_DIR / name
        try:
            dfs.append(_read_csv_tolerant(f))
        except Exception as e:
            print(f"[load_all_csv] skipping {f.name}: {e}", flush=True)
    if not dfs:
        _cache = (sig, pd.DataFrame())
        return _cache[1]
    df = (
        pd.concat(dfs)
        .drop_duplicates(subset=["Date/Time"])
        .sort_values("Date/Time")
        .reset_index(drop=True)
    )
    _cache = (sig, df)
    return df


# --- Daily-aggregation helpers used by the structured tools below -----------

_SLEEP_AGGS = [
    ("total_h", "Sleep Analysis [Total] (hr)", "first"),
    ("asleep_h", "Sleep Analysis [Asleep] (hr)", "first"),
    ("in_bed_h", "Sleep Analysis [In Bed] (hr)", "first"),
    ("core_h", "Sleep Analysis [Core] (hr)", "first"),
    ("deep_h", "Sleep Analysis [Deep] (hr)", "first"),
    ("rem_h", "Sleep Analysis [REM] (hr)", "first"),
    ("awake_h", "Sleep Analysis [Awake] (hr)", "first"),
    ("wrist_temp_c", "Apple Sleeping Wrist Temperature (degC)", "mean"),
]

_FITNESS_AGGS = [
    ("steps", "Step Count (count)", "sum"),
    ("distance_km", "Walking + Running Distance (km)", "sum"),
    ("active_energy_kj", "Active Energy (kJ)", "sum"),
    ("exercise_min", "Apple Exercise Time (min)", "sum"),
    ("stand_min", "Apple Stand Time (min)", "sum"),
    ("flights", "Flights Climbed (count)", "sum"),
    ("walking_speed_kmh_avg", "Walking Speed (km/hr)", "mean"),
    ("walking_hr_avg", "Walking Heart Rate Average (count/min)", "mean"),
    ("vo2_max", "VO2 Max (ml/(kg·min))", "last"),
]

_VITALS_AGGS = [
    ("hr_min", "Heart Rate [Min] (count/min)", "min"),
    ("hr_max", "Heart Rate [Max] (count/min)", "max"),
    ("hr_avg", "Heart Rate [Avg] (count/min)", "mean"),
    ("resting_hr", "Resting Heart Rate (count/min)", "mean"),
    ("hrv_ms", "Heart Rate Variability (ms)", "mean"),
    ("respiratory_rate", "Respiratory Rate (count/min)", "mean"),
    ("blood_oxygen_pct", "Blood Oxygen Saturation (%)", "mean"),
]

# Metrics tracked by get_baselines — drives the report routine's anomaly detection.
_BASELINE_AGGS = [
    ("steps", "Step Count (count)", "sum"),
    ("sleep_total_h", "Sleep Analysis [Total] (hr)", "first"),
    ("deep_h", "Sleep Analysis [Deep] (hr)", "first"),
    ("rem_h", "Sleep Analysis [REM] (hr)", "first"),
    ("hrv_ms", "Heart Rate Variability (ms)", "mean"),
    ("resting_hr", "Resting Heart Rate (count/min)", "mean"),
    ("hr_avg", "Heart Rate [Avg] (count/min)", "mean"),
    ("respiratory_rate", "Respiratory Rate (count/min)", "mean"),
    ("blood_oxygen_pct", "Blood Oxygen Saturation (%)", "mean"),
    ("active_energy_kj", "Active Energy (kJ)", "sum"),
    ("walking_speed_kmh", "Walking Speed (km/hr)", "mean"),
]

# Continuous glucose monitor (CGM) samples arrive at ~5-min cadence all day.
# The briefing cares about the overnight profile, so ``get_daily_vitals``
# aggregates the window 22:00 (previous evening) .. 08:00 and keys it to the
# wake date, matching how sleep records are keyed.
_GLUCOSE_COL = "Blood Glucose (mmol/L)"
_GLUCOSE_NIGHT_START_H = 22
_GLUCOSE_NIGHT_END_H = 8
_GLUCOSE_TIR_LOW_MMOL = 3.9
_GLUCOSE_TIR_HIGH_MMOL = 7.8
_GLUCOSE_NIGHT_FIELDS = [
    "glucose_night_mean_mmol",
    "glucose_night_min_mmol",
    "glucose_night_max_mmol",
    "glucose_night_tir_pct",
    "glucose_night_n",
]

# Food-log entries (third-party app -> HealthKit) arrive as one row per
# logged meal with the macro totals for that meal. Daily totals are sums;
# meal timing is reported separately so the briefing can pair the last meal
# of the evening with the overnight glucose curve that follows it.
_NUTRITION_ENERGY_COL = "Dietary Energy (kJ)"
_NUTRITION_AGGS = [
    ("dietary_energy_kj", _NUTRITION_ENERGY_COL, "sum"),
    ("carbs_g", "Carbohydrates (g)", "sum"),
    ("protein_g", "Protein (g)", "sum"),
    ("fat_g", "Total Fat (g)", "sum"),
    ("fiber_g", "Fiber (g)", "sum"),
    ("sugar_g", "Sugar (g)", "sum"),
    ("caffeine_mg", "Caffeine (mg)", "sum"),
    ("water_ml", "Water (mL)", "sum"),
    ("alcohol_drinks", "Alcohol Consumption (count)", "sum"),
]
_MEAL_TIMING_FIELDS = ["meals_logged", "first_meal_time", "last_meal_time", "last_meal_kj"]

_INTEGER_FIELDS = {"steps", "flights", "glucose_night_n", "meals_logged"}
_STRING_FIELDS = {"first_meal_time", "last_meal_time"}


def _round_or_none(value, ndigits: int = 2):
    if value is None or pd.isna(value):
        return None
    return round(float(value), ndigits)


def _filter_recent(df: pd.DataFrame, days: int) -> pd.DataFrame:
    if df.empty:
        return df
    cutoff = datetime.now() - timedelta(days=days)
    return df[df["Date/Time"] >= cutoff]


def _daily_series(df: pd.DataFrame, col: str, agg: str) -> pd.Series:
    """Daily aggregate of ``col`` keyed by date, dropping NaN inputs first."""
    if df.empty or col not in df.columns:
        return pd.Series([], dtype="float64")
    sub = df[df[col].notna()][["Date/Time", col]].copy()
    if sub.empty:
        return pd.Series([], dtype="float64")
    sub["date"] = sub["Date/Time"].dt.date
    return sub.groupby("date")[col].agg(agg)


def _build_daily_frame(df: pd.DataFrame, aggs: list[tuple]) -> pd.DataFrame:
    cols = {key: _daily_series(df, col, agg) for key, col, agg in aggs}
    return pd.DataFrame(cols)


def _glucose_night_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Overnight CGM aggregates keyed by wake date.

    Samples between 22:00 and 08:00 are shifted forward so the whole night
    lands on the morning's calendar date, then reduced to mean / min / max,
    percent of samples inside 3.9-7.8 mmol/L (time in range) and sample
    count. Returns an empty frame with the expected columns when the
    glucose column is absent or has no samples in the window.
    """
    empty = pd.DataFrame({k: pd.Series([], dtype="float64") for k in _GLUCOSE_NIGHT_FIELDS})
    if df.empty or _GLUCOSE_COL not in df.columns:
        return empty
    sub = df[df[_GLUCOSE_COL].notna()][["Date/Time", _GLUCOSE_COL]].copy()
    if sub.empty:
        return empty
    shift_h = 24 - _GLUCOSE_NIGHT_START_H
    shifted = sub["Date/Time"] + pd.Timedelta(hours=shift_h)
    in_window = shifted.dt.hour < _GLUCOSE_NIGHT_END_H + shift_h
    sub = sub[in_window]
    if sub.empty:
        return empty
    sub["date"] = shifted[in_window].dt.date
    values = sub[_GLUCOSE_COL]
    in_range = values.between(_GLUCOSE_TIR_LOW_MMOL, _GLUCOSE_TIR_HIGH_MMOL)
    grouped = values.groupby(sub["date"])
    return pd.DataFrame(
        {
            "glucose_night_mean_mmol": grouped.mean(),
            "glucose_night_min_mmol": grouped.min(),
            "glucose_night_max_mmol": grouped.max(),
            "glucose_night_tir_pct": in_range.groupby(sub["date"]).mean() * 100,
            "glucose_night_n": grouped.size().astype("float64"),
        }
    )


def _meal_timing_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Per-day meal count and first / last logged-meal clock times.

    A meal is any row carrying a dietary-energy value. Times are ``HH:MM``
    strings; ``last_meal_kj`` is the energy of the latest meal of the day.
    Returns an empty frame with the expected columns when there is no
    food log.
    """
    empty = pd.DataFrame({k: pd.Series([], dtype="object") for k in _MEAL_TIMING_FIELDS})
    if df.empty or _NUTRITION_ENERGY_COL not in df.columns:
        return empty
    sub = df[df[_NUTRITION_ENERGY_COL].notna()][["Date/Time", _NUTRITION_ENERGY_COL]].copy()
    if sub.empty:
        return empty
    sub = sub.sort_values("Date/Time")
    sub["date"] = sub["Date/Time"].dt.date
    grouped = sub.groupby("date")
    first = grouped["Date/Time"].min()
    last = grouped["Date/Time"].max()
    return pd.DataFrame(
        {
            "meals_logged": grouped.size().astype("float64"),
            "first_meal_time": first.dt.strftime("%H:%M"),
            "last_meal_time": last.dt.strftime("%H:%M"),
            "last_meal_kj": grouped[_NUTRITION_ENERGY_COL].last(),
        }
    )


def _records_from_frame(daily: pd.DataFrame) -> list[dict]:
    if daily.empty:
        return []
    records = []
    for date, row in daily.sort_index().iterrows():
        if not row.notna().any():
            continue
        rec: dict = {"date": str(date)}
        for col in daily.columns:
            if col in _STRING_FIELDS:
                raw = row[col]
                rec[col] = None if raw is None or pd.isna(raw) else str(raw)
                continue
            value = _round_or_none(row[col])
            if value is not None and col in _INTEGER_FIELDS:
                value = int(round(value))
            rec[col] = value
        records.append(rec)
    return records


def _format_records(records: list[dict], days_requested: int) -> str:
    return json.dumps(
        {
            "records": records,
            "days_requested": days_requested,
            "days_returned": len(records),
        },
        indent=2,
        default=str,
    )


# --- Tools -------------------------------------------------------------------


@mcp.tool()
def list_metrics() -> str:
    """List all available health metrics that have data."""
    df = load_all_csv()
    if df.empty:
        return "No health data available yet."
    cols = [c for c in df.columns if c != "Date/Time" and df[c].notna().any()]
    return "\n".join(cols)


@mcp.tool()
def get_latest() -> str:
    """Get the most recent health data snapshot."""
    df = load_all_csv()
    if df.empty:
        return "No health data available yet."
    row = df.dropna(how="all", subset=[c for c in df.columns if c != "Date/Time"]).iloc[-1]
    data = row.dropna().to_dict()
    data["Date/Time"] = str(data["Date/Time"])
    return json.dumps(data, indent=2)


@mcp.tool()
def get_metric(metric: str, days: int = 30) -> str:
    """Get a specific health metric over a date range."""
    df = load_all_csv()
    if df.empty:
        return "No health data available yet."
    if metric not in df.columns:
        return f"Metric '{metric}' not found. Use list_metrics first."
    cutoff = datetime.now() - timedelta(days=days)
    filtered = df[df["Date/Time"] >= cutoff][["Date/Time", metric]].dropna()
    return filtered.to_string(index=False)


@mcp.tool()
def get_summary(days: int = 7) -> str:
    """Get a health summary for the past N days."""
    df = load_all_csv()
    if df.empty:
        return "No health data available yet."
    cutoff = datetime.now() - timedelta(days=days)
    recent = df[df["Date/Time"] >= cutoff]
    summary_cols = [
        "Step Count (count)", "Heart Rate [Avg] (count/min)", "Heart Rate Variability (ms)",
        "Resting Heart Rate (count/min)", "Sleep Analysis [Asleep] (hr)",
        "Sleep Analysis [Deep] (hr)", "Sleep Analysis [REM] (hr)",
        "Active Energy (kJ)", "VO2 Max (ml/(kg·min))", "Weight (kg)",
        "Respiratory Rate (count/min)", "Blood Oxygen Saturation (%)", _GLUCOSE_COL,
    ]
    available = [c for c in summary_cols if c in recent.columns and recent[c].notna().any()]
    out = [f"Health summary — last {days} days\n"]
    for col in available:
        vals = recent[col].dropna()
        if not vals.empty:
            out.append(
                f"{col}: avg={vals.mean():.2f}, min={vals.min():.2f}, "
                f"max={vals.max():.2f}, last={vals.iloc[-1]:.2f}"
            )
    return "\n".join(out)


@mcp.tool()
def get_sleep(days: int = 14) -> str:
    """Get sleep analysis for the past N days."""
    df = load_all_csv()
    if df.empty:
        return "No health data available yet."
    cutoff = datetime.now() - timedelta(days=days)
    sleep_cols = [c for c in df.columns if "Sleep" in c or "Wrist Temperature" in c]
    recent = df[df["Date/Time"] >= cutoff][["Date/Time"] + sleep_cols].dropna(
        subset=sleep_cols, how="all"
    )
    return recent.to_string(index=False)


@mcp.tool()
def get_fitness(days: int = 14) -> str:
    """Get fitness metrics (steps, HRV, heart rate, VO2 max) for past N days."""
    df = load_all_csv()
    if df.empty:
        return "No health data available yet."
    cutoff = datetime.now() - timedelta(days=days)
    fitness_cols = [
        "Step Count (count)", "Heart Rate [Min] (count/min)", "Heart Rate [Max] (count/min)",
        "Heart Rate [Avg] (count/min)", "Heart Rate Variability (ms)",
        "Resting Heart Rate (count/min)", "VO2 Max (ml/(kg·min))",
        "Active Energy (kJ)", "Apple Exercise Time (min)", "Walking + Running Distance (km)",
    ]
    available = [c for c in fitness_cols if c in df.columns]
    recent = df[df["Date/Time"] >= cutoff][["Date/Time"] + available].dropna(
        subset=available, how="all"
    )
    return recent.to_string(index=False)


@mcp.tool()
def get_daily_sleep(days: int = 14) -> str:
    """One JSON record per night with sleep stages and wrist temperature.

    Returns ``{"records": [...], "days_requested": N, "days_returned": M}``
    where each record has ``date``, sleep-stage fields in hours
    (``total_h``, ``asleep_h``, ``in_bed_h``, ``core_h``, ``deep_h``,
    ``rem_h``, ``awake_h``) and ``wrist_temp_c`` (mean of per-minute
    wrist temperature samples). Days with no sleep data are omitted.
    """
    df = _filter_recent(load_all_csv(), days)
    if df.empty:
        return _format_records([], days)
    daily = _build_daily_frame(df, _SLEEP_AGGS)
    return _format_records(_records_from_frame(daily), days)


@mcp.tool()
def get_daily_fitness(days: int = 14) -> str:
    """One JSON record per day with activity and movement metrics.

    Per-minute event streams (steps, energy, distance, exercise time,
    stand time, flights) are summed. Walking speed and walking heart
    rate are averaged. VO2 max takes the last non-null measurement of
    the day. Days with no fitness data are omitted.
    """
    df = _filter_recent(load_all_csv(), days)
    if df.empty:
        return _format_records([], days)
    daily = _build_daily_frame(df, _FITNESS_AGGS)
    return _format_records(_records_from_frame(daily), days)


@mcp.tool()
def get_daily_vitals(days: int = 14) -> str:
    """One JSON record per day with cardio, respiratory, oxygen and glucose vitals.

    HR uses min-of-mins, max-of-maxes, mean-of-avgs across the day.
    Resting HR, HRV, respiratory rate, and blood-oxygen saturation are
    averaged across all per-minute samples. Continuous glucose monitor
    samples are reduced to an overnight profile (``glucose_night_mean_mmol``,
    ``glucose_night_min_mmol``, ``glucose_night_max_mmol``,
    ``glucose_night_tir_pct`` = percent of samples in 3.9-7.8 mmol/L,
    ``glucose_night_n``) over 22:00-08:00, keyed to the wake date; the
    fields are null when no CGM data exists. Days with no vitals data
    are omitted.
    """
    df = _filter_recent(load_all_csv(), days)
    if df.empty:
        return _format_records([], days)
    daily = _build_daily_frame(df, _VITALS_AGGS).join(_glucose_night_frame(df), how="outer")
    return _format_records(_records_from_frame(daily), days)


@mcp.tool()
def get_daily_nutrition(days: int = 14) -> str:
    """One JSON record per day with logged food intake and meal timing.

    Food-log entries written to HealthKit by a third-party app are summed
    per calendar day: ``dietary_energy_kj``, ``carbs_g``, ``protein_g``,
    ``fat_g``, ``fiber_g``, ``sugar_g``, ``caffeine_mg``, ``water_ml``,
    ``alcohol_drinks``. Meal timing is derived from rows carrying dietary
    energy: ``meals_logged`` (count), ``first_meal_time`` and
    ``last_meal_time`` (``HH:MM``, local time) and ``last_meal_kj``.
    Pair ``last_meal_time`` with the *next* wake date's
    ``glucose_night_*`` fields from ``get_daily_vitals`` to relate the
    evening's food to the overnight glucose curve. Food logs are partial
    by nature — a day with no record means nothing was logged, not that
    nothing was eaten. Days with no nutrition data are omitted.
    """
    df = _filter_recent(load_all_csv(), days)
    if df.empty:
        return _format_records([], days)
    daily = _build_daily_frame(df, _NUTRITION_AGGS).join(_meal_timing_frame(df), how="outer")
    return _format_records(_records_from_frame(daily), days)


@mcp.tool()
def get_baselines(days: int = 30) -> str:
    """Distribution + recency stats for headline metrics over the last N days.

    For each metric: ``p10``, ``p50``, ``p90`` of the per-day series,
    ``yesterday`` (most recent day's value), ``yesterday_date``,
    ``yesterday_vs_p50_pct``, ``trend_7d_vs_30d_pct``, and ``n_days``.
    Useful for asking "is today unusual?" without re-analysing a long
    history.
    """
    df = _filter_recent(load_all_csv(), days)
    if df.empty:
        return json.dumps({"baselines": [], "days_requested": days}, indent=2)

    per_day = [(key, _daily_series(df, col, agg)) for key, col, agg in _BASELINE_AGGS]
    per_day.append(("glucose_night_mean_mmol", _glucose_night_frame(df)["glucose_night_mean_mmol"]))

    baselines = []
    for key, series in per_day:
        series = series.sort_index()
        if series.empty:
            continue
        p50 = float(series.quantile(0.50))
        latest = float(series.iloc[-1])
        mean_30 = float(series.mean())
        mean_7 = float(series.tail(7).mean()) if len(series) > 0 else None
        rec = {
            "metric": key,
            "p10": _round_or_none(series.quantile(0.10)),
            "p50": _round_or_none(p50),
            "p90": _round_or_none(series.quantile(0.90)),
            "yesterday": _round_or_none(latest),
            "yesterday_date": str(series.index[-1]),
            "yesterday_vs_p50_pct": (
                _round_or_none((latest - p50) / p50 * 100, ndigits=1) if p50 else None
            ),
            "trend_7d_vs_30d_pct": (
                _round_or_none((mean_7 - mean_30) / mean_30 * 100, ndigits=1)
                if mean_30 and mean_7 is not None
                else None
            ),
            "n_days": int(len(series)),
        }
        baselines.append(rec)

    return json.dumps(
        {"baselines": baselines, "days_requested": days},
        indent=2,
        default=str,
    )


# --- Daily report read/write tools ------------------------------------------


def _resolve_report_date(date: str | None) -> str:
    """Validate and normalise a YYYY-MM-DD report date, defaulting to today."""
    if date is None:
        return datetime.now().date().isoformat()
    if not _REPORT_DATE_RE.match(date):
        raise ValueError(f"invalid date: {date!r}; expected YYYY-MM-DD")
    # Strict parse — rejects e.g. 2026-13-01.
    datetime.strptime(date, "%Y-%m-%d")
    return date


@mcp.tool()
def save_health_report(content: str, date: str | None = None) -> str:
    """Persist a daily HEALTH REPORT (markdown) to the reports directory.

    The scheduled report-generation routine calls this at the end of its run.
    Files are written as ``REPORTS_DIR/<YYYY-MM-DD>.md`` and overwrite any
    existing file for the same date. ``date`` defaults to today; pass an
    explicit ``YYYY-MM-DD`` to back-date or future-date a report.

    Returns ``{"saved": "<path>", "bytes": N, "date": "<YYYY-MM-DD>"}`` on
    success or ``{"error": "<message>"}`` on validation failure.
    """
    if not isinstance(content, str) or not content.strip():
        return json.dumps({"error": "content must be a non-empty string"})
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_REPORT_BYTES:
        return json.dumps({"error": f"content exceeds {_MAX_REPORT_BYTES} bytes"})
    try:
        resolved = _resolve_report_date(date)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORTS_DIR / f"{resolved}.md"
    target.write_bytes(encoded)
    return json.dumps(
        {"saved": str(target), "bytes": target.stat().st_size, "date": resolved}
    )


@mcp.tool()
def get_health_report(date: str | None = None, max_age_days: int = 7) -> str:
    """Read a daily HEALTH REPORT from the reports directory.

    With ``date`` set to ``YYYY-MM-DD``: returns that day's report or a
    no-report sentinel.

    With ``date`` left as ``None``: returns the most recent report whose
    filename date is within ``max_age_days`` of today. Useful for the
    Fitness Coach Agent ("show me today's report").

    Returns ``{"date": "<YYYY-MM-DD>", "content": "<markdown>"}`` on hit or
    ``{"date": null, "note": "<message>"}`` on miss.
    """
    if date is not None:
        try:
            resolved = _resolve_report_date(date)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        target = REPORTS_DIR / f"{resolved}.md"
        if target.exists():
            return json.dumps(
                {"date": resolved, "content": target.read_text(encoding="utf-8")}
            )
        return json.dumps({"date": None, "note": f"No report for {resolved}."})

    if not REPORTS_DIR.exists():
        return json.dumps({"date": None, "note": "Reports directory does not exist yet."})

    today = datetime.now().date()
    cutoff = today - timedelta(days=max_age_days)
    candidates: list[tuple] = []
    for path in REPORTS_DIR.glob("*.md"):
        if not _REPORT_DATE_RE.match(path.stem):
            continue
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            candidates.append((day, path))

    if not candidates:
        return json.dumps(
            {"date": None, "note": f"No report in the last {max_age_days} day(s)."}
        )
    candidates.sort()
    latest_day, latest_path = candidates[-1]
    return json.dumps(
        {"date": latest_day.isoformat(), "content": latest_path.read_text(encoding="utf-8")}
    )


mcp.settings.streamable_http_path = f"/mcp/{TOKEN}"

inner_app = mcp.streamable_http_app()


async def app(scope, receive, send):
    await inner_app(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
