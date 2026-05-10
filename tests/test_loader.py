"""Tests for the CSV loader and per-call cache in mcp/server.py."""
import os
import time
from pathlib import Path

from .conftest import FIXTURES, _import_server


def test_read_csv_tolerant_handles_clean_file(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    df = server._read_csv_tolerant(FIXTURES / "sample_clean.csv")
    assert list(df.columns)[0] == "Date/Time"
    assert len(df) == 5


def test_read_csv_tolerant_handles_multipart_envelope(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    df_clean = server._read_csv_tolerant(FIXTURES / "sample_clean.csv")
    df_wrapped = server._read_csv_tolerant(FIXTURES / "sample_wrapped.csv")
    # Same column set, same rows, regardless of envelope.
    assert list(df_clean.columns) == list(df_wrapped.columns)
    assert len(df_clean) == len(df_wrapped)
    assert df_clean["Date/Time"].tolist() == df_wrapped["Date/Time"].tolist()


def test_load_all_csv_returns_combined_deduped_frame(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    df = server.load_all_csv()
    # Both fixtures contain the same 5 rows; dedup on Date/Time -> 5 unique rows.
    assert len(df) == 5
    assert df["Date/Time"].is_monotonic_increasing


def test_load_all_csv_returns_same_instance_on_cache_hit(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    first = server.load_all_csv()
    second = server.load_all_csv()
    assert first is second, "warm call should return the cached DataFrame instance"


def test_load_all_csv_invalidates_when_file_mtime_changes(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    first = server.load_all_csv()
    victim = next(populated_data_dir.glob("health_*.csv"))
    # Bump mtime forward to force a signature change.
    future = time.time() + 10
    os.utime(victim, (future, future))
    second = server.load_all_csv()
    assert second is not first, "touched file should invalidate the cache"
    assert len(second) == len(first), "row count should be unchanged"


def test_load_all_csv_invalidates_when_file_added(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    first = server.load_all_csv()
    extra = populated_data_dir / "health_extra.csv"
    extra.write_bytes((FIXTURES / "sample_clean.csv").read_bytes())
    second = server.load_all_csv()
    assert second is not first


def test_load_all_csv_returns_empty_for_empty_dir(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    df = server.load_all_csv()
    assert df.empty
