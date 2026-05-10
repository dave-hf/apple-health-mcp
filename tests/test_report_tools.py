"""Tests for the save_health_report / get_health_report MCP tools."""
import json
from datetime import datetime, timedelta


def test_save_then_get_round_trips_the_content(reports_dir: tuple):
    rd, server = reports_dir
    body = "# Today's report\n\nSlept like a log."
    saved = json.loads(server.save_health_report(content=body, date="2026-05-10"))
    assert saved["date"] == "2026-05-10"
    assert saved["bytes"] == len(body.encode("utf-8"))
    assert (rd / "2026-05-10.md").read_text(encoding="utf-8") == body

    fetched = json.loads(server.get_health_report(date="2026-05-10"))
    assert fetched["date"] == "2026-05-10"
    assert fetched["content"] == body


def test_save_defaults_date_to_today(reports_dir: tuple):
    rd, server = reports_dir
    today = datetime.now().date().isoformat()
    saved = json.loads(server.save_health_report(content="hello"))
    assert saved["date"] == today
    assert (rd / f"{today}.md").exists()


def test_save_rejects_bad_date(reports_dir: tuple):
    _, server = reports_dir
    out = json.loads(server.save_health_report(content="x", date="2026-13-01"))
    assert "error" in out


def test_save_rejects_path_traversal(reports_dir: tuple):
    _, server = reports_dir
    out = json.loads(server.save_health_report(content="x", date="../../etc/passwd"))
    assert "error" in out


def test_save_rejects_empty_content(reports_dir: tuple):
    _, server = reports_dir
    out = json.loads(server.save_health_report(content="   "))
    assert "error" in out


def test_save_rejects_oversized_content(reports_dir: tuple):
    _, server = reports_dir
    body = "x" * 200_001
    out = json.loads(server.save_health_report(content=body))
    assert "error" in out


def test_get_with_no_files_returns_no_report_sentinel(reports_dir: tuple):
    _, server = reports_dir
    out = json.loads(server.get_health_report())
    assert out["date"] is None
    assert "note" in out


def test_get_specific_date_missing_file_returns_sentinel(reports_dir: tuple):
    _, server = reports_dir
    out = json.loads(server.get_health_report(date="2026-01-01"))
    assert out["date"] is None
    assert "No report" in out["note"]


def test_get_finds_most_recent_within_max_age(reports_dir: tuple):
    rd, server = reports_dir
    today = datetime.now().date()
    yesterday = (today - timedelta(days=1)).isoformat()
    older = (today - timedelta(days=3)).isoformat()
    (rd / f"{older}.md").write_text("older")
    (rd / f"{yesterday}.md").write_text("yesterday")

    out = json.loads(server.get_health_report(max_age_days=7))
    assert out["date"] == yesterday
    assert out["content"] == "yesterday"


def test_get_skips_files_outside_max_age(reports_dir: tuple):
    rd, server = reports_dir
    today = datetime.now().date()
    stale = (today - timedelta(days=14)).isoformat()
    (rd / f"{stale}.md").write_text("stale")

    out = json.loads(server.get_health_report(max_age_days=7))
    assert out["date"] is None
    assert "last 7" in out["note"]


def test_get_ignores_non_date_filenames(reports_dir: tuple):
    rd, server = reports_dir
    (rd / "notes.md").write_text("not a report")
    (rd / "README.md").write_text("docs")

    out = json.loads(server.get_health_report())
    assert out["date"] is None


def test_save_overwrites_existing_report_for_same_date(reports_dir: tuple):
    rd, server = reports_dir
    server.save_health_report(content="v1", date="2026-05-10")
    server.save_health_report(content="v2", date="2026-05-10")
    assert (rd / "2026-05-10.md").read_text() == "v2"
