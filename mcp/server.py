"""Apple Health MCP server.

Exposes Apple Health data (CSV exports written by the ingest service) to any
MCP client over Streamable HTTP. The bearer token is embedded in the URL path
because Claude's connector UI does not currently support custom auth headers.
"""
import os
import pathlib
import json
import io
import pandas as pd
import uvicorn
from datetime import datetime, timedelta
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv("/opt/health/.env")
TOKEN = os.getenv("HEALTH_TOKEN")
DATA_DIR = pathlib.Path(os.getenv("DATA_DIR", "/opt/health/data"))

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
        "Respiratory Rate (count/min)", "Blood Oxygen Saturation (%)",
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


mcp.settings.streamable_http_path = f"/mcp/{TOKEN}"

inner_app = mcp.streamable_http_app()


async def app(scope, receive, send):
    await inner_app(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
