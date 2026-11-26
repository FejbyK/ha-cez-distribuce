"""Unit tests for custom_components/cez_distribuce/hdo_split.py."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_CEZ_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "cez_distribuce"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _CEZ_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hdo_split = _load_module("cez_test_hdo_split", "hdo_split.py")
PRAGUE = ZoneInfo("Europe/Prague")


def _as_prague(dt: datetime) -> datetime:
    return dt.astimezone(PRAGUE)


class TestHdoSplit(unittest.TestCase):
    """Tests for HDO schedule parsing, lookup, 15-min/hourly split, and cost calculation."""

    def test_parse_casy_segments(self) -> None:
        self.assertEqual(
            hdo_split.parse_casy_segments("00:00-06:00; 13:15-14:45; 22:35-24:00"),
            [(0, 360), (795, 885), (1355, 1440)],
        )
        # Overnight range wraps cleanly around midnight
        self.assertEqual(
            hdo_split.parse_casy_segments("22:35-06:35"),
            [(0, 395), (1355, 1440)],
        )
        self.assertEqual(hdo_split.parse_casy_segments(""), [])
        self.assertEqual(hdo_split.parse_casy_segments("bad-value; 10:00-10:00"), [])

    def test_extract_signal_schedules_filters_by_signal(self) -> None:
        payload = {
            "signals": [
                {"signal": "a3b7dp01", "datum": "01.04.2026", "casy": "00:00-06:00;22:00-24:00"},
                {"signal": "a3b7dp02", "datum": "01.04.2026", "casy": "01:00-05:00"},
                {"signal": "a3b7dp01", "datum": "02.04.2026", "casy": "00:00-06:00"},
                {"signal": "a3b7dp01", "datum": "bad-date", "casy": "00:00-06:00"},
            ]
        }
        schedules = hdo_split.extract_signal_schedules(payload, "a3b7dp01")
        self.assertEqual(
            schedules,
            {
                "2026-04-01": "00:00-06:00;22:00-24:00",
                "2026-04-02": "00:00-06:00",
            },
        )
        # Empty signal picks the first entry per day (same as sensor.py)
        self.assertEqual(
            hdo_split.extract_signal_schedules(payload, ""),
            {
                "2026-04-01": "00:00-06:00;22:00-24:00",
                "2026-04-02": "00:00-06:00",
            },
        )
        self.assertEqual(hdo_split.extract_signal_schedules(payload, "unknown"), {})

    def test_schedule_lookup_known_weekday_and_nearest(self) -> None:
        # 2026-04-06 is Monday, 2026-04-13 is also Monday
        lookup = hdo_split.ScheduleLookup({"2026-04-06": "00:00-06:00"})
        found = lookup.segments_for(date(2026, 4, 6))
        self.assertEqual(found, ([(0, 360)], hdo_split.SOURCE_KNOWN))

        found_w = lookup.segments_for(date(2026, 4, 13))
        self.assertEqual(found_w, ([(0, 360)], hdo_split.SOURCE_WEEKDAY))

        found_n = lookup.segments_for(date(2026, 4, 7))
        self.assertEqual(found_n, ([(0, 360)], hdo_split.SOURCE_NEAREST))

    def test_15min_split_boundary_precision(self) -> None:
        # NT starts at 22:35 local (CEST = UTC+2 on 2026-04-01)
        lookup = hdo_split.ScheduleLookup({"2026-04-01": "22:35-24:00"})
        hour_utc = datetime(2026, 4, 1, 20, 0, tzinfo=timezone.utc)  # 22:00 CEST
        # 4 quarter-hours in hour 22:00..23:00 CEST:
        # 22:00-22:15: 1.000 kWh -> 100% VT
        # 22:15-22:30: 1.000 kWh -> 100% VT
        # 22:30-22:45: 1.500 kWh -> 5/15 VT (0.500) + 10/15 NT (1.000)
        # 22:45-23:00: 2.000 kWh -> 100% NT
        points = [
            (hour_utc, 1.0),
            (hour_utc + timedelta(minutes=15), 1.0),
            (hour_utc + timedelta(minutes=30), 1.5),
            (hour_utc + timedelta(minutes=45), 2.0),
        ]
        split = hdo_split.split_interval_consumption(points, 15, lookup, _as_prague)
        self.assertIsNotNone(split)
        assert split is not None
        self.assertAlmostEqual(split.hourly_vt[hour_utc], 2.5, places=4)
        self.assertAlmostEqual(split.hourly_nt[hour_utc], 3.0, places=4)
        self.assertEqual(split.days_count(hdo_split.SOURCE_KNOWN), 1)

    def test_single_tariff_fallback_all_vt(self) -> None:
        empty_lookup = hdo_split.ScheduleLookup({})
        ts = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)
        points = [(ts, 1.2345), (ts + timedelta(minutes=15), 0.7655)]

        # Without fallback_all_vt -> None
        self.assertIsNone(
            hdo_split.split_interval_consumption(points, 15, empty_lookup, _as_prague)
        )
        # With fallback_all_vt -> 100% VT, 0% NT
        split = hdo_split.split_interval_consumption(
            points, 15, empty_lookup, _as_prague, fallback_all_vt=True
        )
        self.assertIsNotNone(split)
        assert split is not None
        self.assertAlmostEqual(split.hourly_vt[ts], 2.0, places=4)
        self.assertAlmostEqual(split.hourly_nt[ts], 0.0, places=4)

    def test_dst_transitions_prague(self) -> None:
        # Spring forward: 2026-03-29 (01:00 UTC -> 03:00 CEST, 02:00-03:00 skipped)
        # Fall back: 2026-10-25 (00:00 UTC = 02:00 CEST, 01:00 UTC = 02:00 CET)
        lookup = hdo_split.ScheduleLookup(
            {
                "2026-03-29": "00:00-06:00",
                "2026-10-25": "00:00-06:00",
            }
        )
        spring_points = [
            (datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc) + timedelta(hours=h), 1.0)
            for h in range(6)
        ]
        spring_split = hdo_split.split_hourly_consumption(spring_points, lookup, _as_prague)
        self.assertIsNotNone(spring_split)
        assert spring_split is not None
        # 00:00 UTC=01:00 CET (NT), 01:00 UTC=03:00 CEST (NT), 02:00 UTC=04:00 CEST (NT),
        # 03:00 UTC=05:00 CEST (NT), 04:00 UTC=06:00 CEST (VT), 05:00 UTC=07:00 CEST (VT)
        self.assertAlmostEqual(spring_split.nt_kwh, 4.0, places=4)
        self.assertAlmostEqual(spring_split.vt_kwh, 2.0, places=4)

        # Fall back: both 00:00 UTC (02:00 CEST) and 01:00 UTC (02:00 CET) are in 00:00-06:00 NT
        fall_points = [
            (datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc), 1.5),
            (datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc), 2.5),
        ]
        fall_split = hdo_split.split_hourly_consumption(fall_points, lookup, _as_prague)
        self.assertIsNotNone(fall_split)
        assert fall_split is not None
        self.assertAlmostEqual(fall_split.nt_kwh, 4.0, places=4)
        self.assertAlmostEqual(fall_split.vt_kwh, 0.0, places=4)

    def test_calculate_hourly_costs_exact_sum(self) -> None:
        h1 = datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc)
        h2 = datetime(2026, 4, 1, 11, 0, tzinfo=timezone.utc)
        vt = {h1: 1.3333, h2: 0.0}
        nt = {h1: 0.6667, h2: 2.5}
        c_vt, c_nt, c_tot = hdo_split.calculate_hourly_costs(vt, nt, 5.25, 2.75)
        for h in (h1, h2):
            self.assertAlmostEqual(c_vt[h] + c_nt[h], c_tot[h], places=4)
        self.assertAlmostEqual(c_vt[h1], round(1.3333 * 5.25, 4), places=4)
        self.assertAlmostEqual(c_nt[h2], round(2.5 * 2.75, 4), places=4)

    def test_merge_schedule_history_prunes_old_days(self) -> None:
        history = {
            "2024-01-01": "00:00-06:00",
            "2026-04-01": "00:00-06:00",
        }
        fresh = {"2026-04-02": "00:00-08:00"}
        changed = hdo_split.merge_schedule_history(history, fresh, today=date(2026, 4, 2))
        self.assertTrue(changed)
        self.assertNotIn("2024-01-01", history)
        self.assertIn("2026-04-01", history)
        self.assertEqual(history["2026-04-02"], "00:00-08:00")


if __name__ == "__main__":
    unittest.main()
