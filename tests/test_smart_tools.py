"""Tests for the structured daily / baseline MCP tools.

The fixtures are dated 2026-04-0X. Tests pass ``days=10000`` so
``_filter_recent`` does not drop the test data based on wall-clock time.
"""
import json
import shutil
from pathlib import Path

from .conftest import FIXTURES, _import_server


def _server_with_smart_fixture(tmp_data_dir: Path):
    shutil.copy(FIXTURES / "sample_smart.csv", tmp_data_dir / "health_smart.csv")
    return _import_server(tmp_data_dir)


def test_get_daily_sleep_emits_one_record_per_night(tmp_data_dir: Path):
    server = _server_with_smart_fixture(tmp_data_dir)
    result = json.loads(server.get_daily_sleep(days=10000))

    assert result["days_requested"] == 10000
    assert result["days_returned"] == 3
    dates = [r["date"] for r in result["records"]]
    assert dates == ["2026-04-01", "2026-04-02", "2026-04-03"]

    day1 = result["records"][0]
    assert day1["total_h"] == 8.0
    assert day1["asleep_h"] == 7.5
    assert day1["deep_h"] == 1.0
    assert day1["rem_h"] == 1.5
    # 04-01 has only one wrist-temp reading at 35.5
    assert day1["wrist_temp_c"] == 35.5


def test_get_daily_fitness_sums_per_minute_steps(tmp_data_dir: Path):
    server = _server_with_smart_fixture(tmp_data_dir)
    result = json.loads(server.get_daily_fitness(days=10000))

    assert result["days_returned"] == 3
    by_date = {r["date"]: r for r in result["records"]}

    # 04-01 has step rows of 1200 and 2300 -> 3500 total.
    assert by_date["2026-04-01"]["steps"] == 3500
    assert by_date["2026-04-02"]["steps"] == 1500
    assert by_date["2026-04-03"]["steps"] == 2000

    # Distance: 0.9 + 1.6 = 2.5 on 04-01.
    assert by_date["2026-04-01"]["distance_km"] == 2.5
    # Active energy: 250 + 400 = 650 kJ.
    assert by_date["2026-04-01"]["active_energy_kj"] == 650.0
    # Flights summed.
    assert by_date["2026-04-01"]["flights"] == 3
    # VO2 max takes the last non-null reading of the day.
    assert by_date["2026-04-01"]["vo2_max"] == 38.0
    # Day with no VO2 reading -> None.
    assert by_date["2026-04-02"]["vo2_max"] is None


def test_get_daily_vitals_aggregates_per_day(tmp_data_dir: Path):
    server = _server_with_smart_fixture(tmp_data_dir)
    result = json.loads(server.get_daily_vitals(days=10000))

    by_date = {r["date"]: r for r in result["records"]}

    # 04-01 HR mins: 60, 65 -> overall min = 60.
    assert by_date["2026-04-01"]["hr_min"] == 60
    # 04-01 HR maxes: 80, 90 -> overall max = 90.
    assert by_date["2026-04-01"]["hr_max"] == 90
    # 04-01 HR avgs: 72, 88 -> mean = 80.
    assert by_date["2026-04-01"]["hr_avg"] == 80.0
    # Resting HR: only one reading per day on 04-01 (55) -> mean = 55.
    assert by_date["2026-04-01"]["resting_hr"] == 55.0
    # Blood oxygen: 98, 97 on 04-01 -> mean 97.5.
    assert by_date["2026-04-01"]["blood_oxygen_pct"] == 97.5


def test_get_baselines_orders_quantiles_and_marks_yesterday(tmp_data_dir: Path):
    server = _server_with_smart_fixture(tmp_data_dir)
    result = json.loads(server.get_baselines(days=10000))

    metrics = {b["metric"]: b for b in result["baselines"]}

    # Series for steps is [3500, 1500, 2000] across the three days.
    steps = metrics["steps"]
    assert steps["p10"] <= steps["p50"] <= steps["p90"]
    assert steps["n_days"] == 3
    assert steps["yesterday_date"] == "2026-04-03"
    assert steps["yesterday"] == 2000.0

    # Sleep total is [8.0, 7.5, 9.0]; yesterday is 9.0.
    sleep = metrics["sleep_total_h"]
    assert sleep["yesterday"] == 9.0
    assert sleep["p10"] <= sleep["p50"] <= sleep["p90"]


def test_smart_tools_handle_empty_dir(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    assert json.loads(server.get_daily_sleep(days=14))["days_returned"] == 0
    assert json.loads(server.get_daily_fitness(days=14))["days_returned"] == 0
    assert json.loads(server.get_daily_vitals(days=14))["days_returned"] == 0
    assert json.loads(server.get_baselines(days=30))["baselines"] == []
