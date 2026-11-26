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


def test_get_daily_vitals_glucose_overnight_window_keyed_to_wake_date(tmp_data_dir: Path):
    server = _server_with_smart_fixture(tmp_data_dir)
    result = json.loads(server.get_daily_vitals(days=10000))

    # Glucose rows must not create extra day records on their own.
    assert result["days_returned"] == 3
    by_date = {r["date"]: r for r in result["records"]}

    # Night keyed 04-02 = 04-01 23:30 (6.0), 04-02 02:00 (4.0), 04-02 05:00 (8.0).
    # 04-02 08:00 (7.0) is outside the half-open window; 12:00 (9.0) is daytime.
    night = by_date["2026-04-02"]
    assert night["glucose_night_n"] == 3
    assert night["glucose_night_mean_mmol"] == 6.0
    assert night["glucose_night_min_mmol"] == 4.0
    assert night["glucose_night_max_mmol"] == 8.0
    # 4.0 and 6.0 are inside 3.9-7.8 mmol/L; 8.0 is not -> 2/3.
    assert night["glucose_night_tir_pct"] == 66.67

    # Single sample night.
    assert by_date["2026-04-03"]["glucose_night_n"] == 1
    assert by_date["2026-04-03"]["glucose_night_mean_mmol"] == 5.0
    assert by_date["2026-04-03"]["glucose_night_tir_pct"] == 100.0

    # No CGM samples before the first night -> fields present but null.
    assert by_date["2026-04-01"]["glucose_night_mean_mmol"] is None
    assert by_date["2026-04-01"]["glucose_night_n"] is None


def test_get_daily_vitals_without_glucose_column_emits_null_fields(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    result = json.loads(server.get_daily_vitals(days=10000))

    assert result["days_returned"] > 0
    for rec in result["records"]:
        assert rec["glucose_night_mean_mmol"] is None
        assert rec["glucose_night_tir_pct"] is None


def test_get_daily_nutrition_sums_meals_and_reports_timing(tmp_data_dir: Path):
    server = _server_with_smart_fixture(tmp_data_dir)
    result = json.loads(server.get_daily_nutrition(days=10000))

    # 04-03 has no food log at all -> omitted, not emitted as nulls.
    assert result["days_returned"] == 2
    by_date = {r["date"]: r for r in result["records"]}
    assert set(by_date) == {"2026-04-01", "2026-04-02"}

    # 04-01: meals at 08:30 (2000 kJ) and 21:45 (3000 kJ).
    day1 = by_date["2026-04-01"]
    assert day1["dietary_energy_kj"] == 5000.0
    assert day1["carbs_g"] == 120.0
    assert day1["protein_g"] == 50.0
    assert day1["fat_g"] == 35.0
    assert day1["fiber_g"] == 13.0
    assert day1["sugar_g"] == 32.0
    assert day1["caffeine_mg"] == 80.0
    assert day1["water_ml"] == 750.0
    assert day1["alcohol_drinks"] == 1.0
    assert day1["meals_logged"] == 2
    assert day1["first_meal_time"] == "08:30"
    assert day1["last_meal_time"] == "21:45"
    assert day1["last_meal_kj"] == 3000.0

    # 04-02: single meal, no caffeine / water / alcohol logged.
    day2 = by_date["2026-04-02"]
    assert day2["dietary_energy_kj"] == 2500.0
    assert day2["meals_logged"] == 1
    assert day2["first_meal_time"] == "13:00"
    assert day2["last_meal_time"] == "13:00"
    assert day2["caffeine_mg"] is None
    assert day2["water_ml"] is None
    assert day2["alcohol_drinks"] is None


def test_get_daily_nutrition_without_food_log_returns_no_records(populated_data_dir: Path):
    server = _import_server(populated_data_dir)
    result = json.loads(server.get_daily_nutrition(days=10000))
    assert result["days_returned"] == 0
    assert result["records"] == []


def test_nutrition_rows_do_not_add_days_to_other_daily_tools(tmp_data_dir: Path):
    server = _server_with_smart_fixture(tmp_data_dir)
    # Food-log rows at 08:30 / 21:45 / 13:00 must not create extra day
    # records in tools that do not aggregate nutrition columns.
    assert json.loads(server.get_daily_vitals(days=10000))["days_returned"] == 3
    assert json.loads(server.get_daily_fitness(days=10000))["days_returned"] == 3
    assert json.loads(server.get_daily_sleep(days=10000))["days_returned"] == 3


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

    # Overnight glucose mean is [6.0 (04-02), 5.0 (04-03)]; 04-01 has no CGM.
    glucose = metrics["glucose_night_mean_mmol"]
    assert glucose["n_days"] == 2
    assert glucose["yesterday_date"] == "2026-04-03"
    assert glucose["yesterday"] == 5.0
    assert glucose["p50"] == 5.5


def test_smart_tools_handle_empty_dir(tmp_data_dir: Path):
    server = _import_server(tmp_data_dir)
    assert json.loads(server.get_daily_sleep(days=14))["days_returned"] == 0
    assert json.loads(server.get_daily_fitness(days=14))["days_returned"] == 0
    assert json.loads(server.get_daily_vitals(days=14))["days_returned"] == 0
    assert json.loads(server.get_daily_nutrition(days=14))["days_returned"] == 0
    assert json.loads(server.get_baselines(days=30))["baselines"] == []
