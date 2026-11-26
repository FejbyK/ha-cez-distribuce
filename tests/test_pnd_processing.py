"""Unit tests for custom_components/cez_distribuce/pnd_processing.py and const.py helpers."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_CEZ_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "cez_distribuce"
if str(_CEZ_DIR) not in sys.path:
    sys.path.insert(0, str(_CEZ_DIR))


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _CEZ_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


const = _load_module("const", "const.py")
pnd = _load_module("cez_test_pnd_processing", "pnd_processing.py")
PRAGUE = ZoneInfo("Europe/Prague")


class TestPndProcessing(unittest.TestCase):
    """Tests for PND timestamp/unit conversion, tail trimming, gap detection, and Recorder rows."""

    def test_assembly_codes_and_intervals(self) -> None:
        self.assertEqual(const.pnd_assembly_code_for("S"), const.PND_ASSEMBLY_15MIN)
        self.assertEqual(const.pnd_assembly_code_for("V"), const.PND_ASSEMBLY_HOURLY_PRODUCTION)
        self.assertEqual(const.pnd_assembly_code_for("M"), const.PND_ASSEMBLY_HOURLY_PRODUCTION)
        self.assertEqual(const.pnd_assembly_code_for("B"), const.PND_ASSEMBLY_HOURLY_PRODUCTION)
        self.assertEqual(const.pnd_max_interval_days_for("03"), const.MAX_PND_15MIN_INTERVAL_DAYS)
        self.assertEqual(const.pnd_max_interval_days_for("05"), const.MAX_PND_INTERVAL_DAYS)
        self.assertEqual(const.pnd_max_interval_days_for("06"), const.MAX_PND_INTERVAL_DAYS)

    def test_infer_interval_minutes(self) -> None:
        raw_15 = [
            {"time": "2026-04-01T10:15:00.000Z", "value": 1.0},
            {"time": "2026-04-01T10:30:00.000Z", "value": 1.2},
            {"time": "2026-04-01T10:45:00.000Z", "value": 0.8},
        ]
        self.assertEqual(pnd.infer_interval_minutes(raw_15, fallback=60), 15)
        self.assertEqual(pnd.infer_interval_minutes(raw_15[:1], fallback=60), 60)

    def test_process_pnd_points_15min_kw_to_kwh_and_tail_trim(self) -> None:
        start = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)
        now = datetime(2026, 4, 1, 13, 0, tzinfo=timezone.utc)
        # "time" in ČEZ pnd/data is END of interval!
        # Hour 10:00..11:00 has four 15-min points of 2.0 kW -> 4 * (2.0 * 0.25) = 2.0 kWh
        # Hour 11:00..12:00 has four trailing 0.0 kW points -> trimmed as incomplete tail
        raw = [
            {"time": "2026-04-01T10:15:00.000Z", "value": 2.0},
            {"time": "2026-04-01T10:30:00.000Z", "value": 2.0},
            {"time": "2026-04-01T10:45:00.000Z", "value": 2.0},
            {"time": "2026-04-01T11:00:00.000Z", "value": 2.0},
            {"time": "2026-04-01T11:15:00.000Z", "value": 0.0},
            {"time": "2026-04-01T11:30:00.000Z", "value": 0.0},
            {"time": "2026-04-01T11:45:00.000Z", "value": 0.0},
            {"time": "2026-04-01T12:00:00.000Z", "value": 0.0},
        ]
        complete, hourly, trimmed = pnd.process_pnd_points(raw, "kW", 15, start, now)
        self.assertEqual(trimmed, 4)
        self.assertEqual(len(complete), 4)
        self.assertEqual(list(hourly.keys()), [start])
        self.assertAlmostEqual(hourly[start], 2.0, places=6)

    def test_build_statistic_rows_merges_and_shifts_subsequent(self) -> None:
        h0 = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        h1 = datetime(2026, 4, 1, 1, 0, tzinfo=timezone.utc)
        h2 = datetime(2026, 4, 1, 2, 0, tzinfo=timezone.utc)

        # Updating h1 from 1.0 -> 2.5 with baseline_sum=10.0 and subsequent h2 (state=3.0)
        rows, final_sum = pnd.build_statistic_rows(
            hourly_deltas={h1: 2.5},
            existing_in_window=[(h1, 1.0, 11.0)],
            baseline_sum=10.0,
            subsequent_rows=[(h2, 3.0, 14.0)],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["start"], h1)
        self.assertAlmostEqual(rows[0]["state"], 2.5, places=4)
        self.assertAlmostEqual(rows[0]["sum"], 12.5, places=4)
        self.assertEqual(rows[1]["start"], h2)
        self.assertAlmostEqual(rows[1]["state"], 3.0, places=4)
        self.assertAlmostEqual(rows[1]["sum"], 15.5, places=4)
        self.assertAlmostEqual(final_sum, 15.5, places=4)
        self.assertNotIn(h0, [r["start"] for r in rows])

    def test_rows_differ_from_existing(self) -> None:
        h1 = datetime(2026, 4, 1, 1, 0, tzinfo=timezone.utc)
        h2 = datetime(2026, 4, 1, 2, 0, tzinfo=timezone.utc)
        rows = [
            {"start": h1, "state": 1.25, "sum": 11.25},
            {"start": h2, "state": 0.75, "sum": 12.0},
        ]
        existing_same = [(h1, 1.25, 11.25), (h2, 0.75, 12.0)]
        existing_diff_state = [(h1, 1.25, 11.25), (h2, 0.80, 12.0)]
        existing_diff_sum = [(h1, 1.25, 11.25), (h2, 0.75, 12.05)]
        existing_subset = [(h1, 1.25, 11.25)]

        self.assertFalse(pnd.rows_differ_from_existing(rows, existing_same))
        self.assertTrue(pnd.rows_differ_from_existing(rows, existing_diff_state))
        self.assertTrue(pnd.rows_differ_from_existing(rows, existing_diff_sum))
        self.assertTrue(pnd.rows_differ_from_existing(rows, existing_subset))
        self.assertFalse(pnd.rows_differ_from_existing([], []))

    def test_find_statistic_gaps(self) -> None:
        base = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
        # Missing hour 2, and zero-run of 3 hours at 4,5,6
        hours = [
            (base + timedelta(hours=0), 1.0),
            (base + timedelta(hours=1), 1.0),
            (base + timedelta(hours=3), 1.0),
            (base + timedelta(hours=4), 0.0),
            (base + timedelta(hours=5), 0.0),
            (base + timedelta(hours=6), 0.0),
        ]
        gap_start, missing, zero_hours = pnd.find_statistic_gaps(hours, 3)
        self.assertEqual(gap_start, base + timedelta(hours=2))
        self.assertEqual(missing, 1)
        self.assertEqual(zero_hours, 3)

        # Production mode (zero_run_hours=None) ignores zeros
        _, missing_p, zero_p = pnd.find_statistic_gaps(hours, None)
        self.assertEqual(missing_p, 1)
        self.assertEqual(zero_p, 0)

    def test_reading_baseline(self) -> None:
        # Reading with time 00:00:00 means end of that day -> 2026-04-02 00:00 CEST == 2026-04-01 22:00 UTC
        reading = {
            "datumOdectu": "2026-04-01T00:00:00",
            "casOdectu": "00:00:00",
            "stavVt": "1234.5",
            "stavNt": "567.25",
        }
        baseline = pnd.reading_baseline(reading, PRAGUE)
        self.assertIsNotNone(baseline)
        assert baseline is not None
        base_time, total = baseline
        self.assertEqual(base_time, datetime(2026, 4, 1, 22, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(total, 1801.75, places=4)

        # Reading with explicit technician time 14:30 -> 2026-04-01 14:00 CEST == 2026-04-01 12:00 UTC
        reading_timed = {
            "datumOdectu": "2026-04-01T00:00:00",
            "casOdectu": "14:30:00",
            "stavVt": "100",
            "stavNt": "50",
        }
        baseline_timed = pnd.reading_baseline(reading_timed, PRAGUE)
        assert baseline_timed is not None
        self.assertEqual(baseline_timed[0], datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(baseline_timed[1], 150.0, places=4)


if __name__ == "__main__":
    unittest.main()
