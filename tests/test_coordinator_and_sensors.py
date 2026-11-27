"""Integration-style unit tests for coordinator, sensors, services, and API client sessions.

Uses lightweight stubs for Home Assistant, aiohttp, bs4, and voluptuous so the
entire test suite runs with the Python standard library (`python3 -m unittest`)
as well as `pytest` without requiring a full Home Assistant installation.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

PRAGUE = ZoneInfo("Europe/Prague")


class _FakeRecorder:
    """Minimal in-memory Recorder backing external statistics for tests."""

    def __init__(self) -> None:
        self.stats: dict[str, dict[datetime, dict[str, Any]]] = {}
        self.write_calls: list[tuple[str, int]] = []
        self.executor_jobs = 0
        self.period_queries = 0

    async def async_add_executor_job(self, func, *args):
        self.executor_jobs += 1
        return func(*args)

    def add_external_statistics(self, metadata: dict[str, Any], statistics: list[dict[str, Any]]) -> None:
        stat_id = metadata["statistic_id"]
        self.write_calls.append((stat_id, len(statistics)))
        bucket = self.stats.setdefault(stat_id, {})
        for row in statistics:
            bucket[row["start"]] = dict(row)

    def get_last_statistics(self, _hass, number_of_stats: int, statistic_id: str, *_args):
        bucket = self.stats.get(statistic_id)
        if not bucket:
            return {}
        ordered = [
            {**bucket[ts], "start": ts.timestamp()}
            for ts in sorted(bucket)[-number_of_stats:]
        ]
        return {statistic_id: ordered}

    def statistics_during_period(
        self,
        _hass,
        start_time: datetime,
        end_time: datetime | None,
        statistic_ids: set[str],
        period: str,
        *_args,
    ):
        self.period_queries += 1
        result: dict[str, list[dict[str, Any]]] = {}
        for stat_id in statistic_ids:
            bucket = self.stats.get(stat_id, {})
            rows = []
            for ts in sorted(bucket):
                if ts < start_time:
                    continue
                if end_time is not None and ts >= end_time:
                    continue
                rows.append({**bucket[ts], "start": ts.timestamp()})
            if rows:
                if period == "month":
                    result[stat_id] = [rows[-1]]
                else:
                    result[stat_id] = rows
        return result


class _FakeServices:
    def __init__(self) -> None:
        self._services: dict[tuple[str, str], Any] = {}

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self._services

    def async_register(self, domain: str, service: str, handler, schema=None) -> None:
        self._services[(domain, service)] = (handler, schema)

    def async_remove(self, domain: str, service: str) -> None:
        self._services.pop((domain, service), None)

    async def async_call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        handler, _ = self._services[(domain, service)]
        call = types.SimpleNamespace(data=data)
        await handler(call)


class _FakeHass:
    def __init__(self, recorder: _FakeRecorder, currency: str = "CZK") -> None:
        self.recorder = recorder
        self.config = types.SimpleNamespace(currency=currency, language="cs")
        self.data: dict[str, Any] = {}
        self.services = _FakeServices()


class _FakeEntry:
    def __init__(
        self,
        entry_id: str = "entry-1",
        title: str = "Doma",
        data: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        self.entry_id = entry_id
        self.title = title
        self.data = data or {}
        self.options = options or {}


def _install_stubs() -> dict[str, Any]:
    """Install lightweight stubs into sys.modules and return originals to restore."""
    saved: dict[str, Any] = {}

    def _put(name: str, mod: types.ModuleType) -> None:
        if name not in saved:
            saved[name] = sys.modules.get(name)
        sys.modules[name] = mod

    # Clear previously imported custom_components.cez_distribuce modules so they bind fresh stubs
    for key in list(sys.modules):
        if key == "custom_components" or key.startswith("custom_components.cez_distribuce"):
            saved.setdefault(key, sys.modules.pop(key))

    # --- aiohttp stub ---
    aiohttp_mod = types.ModuleType("aiohttp")

    class _FakeClientSession:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            self.kwargs = kwargs

        async def close(self) -> None:
            self.closed = True

    class _FakeCookieJar:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class _FakeClientTimeout:
        def __init__(self, total=None) -> None:
            self.total = total

    class _FakeClientError(Exception):
        pass

    aiohttp_mod.ClientSession = _FakeClientSession
    aiohttp_mod.CookieJar = _FakeCookieJar
    aiohttp_mod.DummyCookieJar = _FakeCookieJar
    aiohttp_mod.ClientTimeout = _FakeClientTimeout
    aiohttp_mod.ClientError = _FakeClientError
    _put("aiohttp", aiohttp_mod)

    # --- bs4 stub ---
    bs4_mod = types.ModuleType("bs4")
    bs4_mod.BeautifulSoup = object
    _put("bs4", bs4_mod)

    # --- voluptuous stub ---
    vol_mod = types.ModuleType("voluptuous")
    vol_mod.Schema = lambda schema: schema
    vol_mod.Optional = lambda key, default=None: key
    vol_mod.Required = lambda key, default=None: key
    vol_mod.All = lambda *validators: validators
    vol_mod.Coerce = lambda t: t
    vol_mod.Range = lambda **kwargs: kwargs
    vol_mod.In = lambda opts: opts
    _put("voluptuous", vol_mod)

    # --- homeassistant stubs ---
    ha = types.ModuleType("homeassistant")
    _put("homeassistant", ha)

    ha_const = types.ModuleType("homeassistant.const")

    class _Platform(StrEnum):
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"

    class _UnitOfEnergy(StrEnum):
        KILO_WATT_HOUR = "kWh"

    ha_const.Platform = _Platform
    ha_const.UnitOfEnergy = _UnitOfEnergy
    _put("homeassistant.const", ha_const)

    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = object
    ha_core.ServiceCall = object
    ha_core.CALLBACK_TYPE = Any
    ha_core.callback = lambda fn: fn
    _put("homeassistant.core", ha_core)

    ha_ce = types.ModuleType("homeassistant.config_entries")
    ha_ce.ConfigEntry = _FakeEntry
    ha_ce.ConfigFlow = type("ConfigFlow", (), {"__init_subclass__": classmethod(lambda cls, **kw: None)})
    ha_ce.OptionsFlow = type("OptionsFlow", (), {})
    ha_ce.OptionsFlowWithReload = type("OptionsFlowWithReload", (), {})
    _put("homeassistant.config_entries", ha_ce)

    ha_def = types.ModuleType("homeassistant.data_entry_flow")
    ha_def.FlowResult = dict
    _put("homeassistant.data_entry_flow", ha_def)

    ha_exc = types.ModuleType("homeassistant.exceptions")
    ha_exc.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    ha_exc.ConfigEntryError = type("ConfigEntryError", (Exception,), {})
    ha_exc.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    _put("homeassistant.exceptions", ha_exc)

    ha_util = types.ModuleType("homeassistant.util")
    _put("homeassistant.util", ha_util)

    ha_dt = types.ModuleType("homeassistant.util.dt")
    ha_dt._now_override = None
    ha_dt.DEFAULT_TIME_ZONE = PRAGUE
    ha_dt.get_default_time_zone = lambda: PRAGUE
    ha_dt.as_local = lambda dt: dt.astimezone(PRAGUE)
    ha_dt.now = lambda: (
        ha_dt._now_override
        if ha_dt._now_override is not None
        else datetime.now(PRAGUE)
    )
    ha_dt.utcnow = lambda: (
        ha_dt._now_override.astimezone(timezone.utc)
        if ha_dt._now_override is not None
        else datetime.now(timezone.utc)
    )
    ha_util.dt = ha_dt
    _put("homeassistant.util.dt", ha_dt)

    # Recorder stubs
    ha_comp = types.ModuleType("homeassistant.components")
    _put("homeassistant.components", ha_comp)

    ha_rec = types.ModuleType("homeassistant.components.recorder")
    ha_rec.get_instance = lambda hass: hass.recorder
    _put("homeassistant.components.recorder", ha_rec)

    ha_rec_models = types.ModuleType("homeassistant.components.recorder.models")
    ha_rec_models.StatisticData = dict
    ha_rec_models.StatisticMeanType = types.SimpleNamespace(NONE="none")
    ha_rec_models.StatisticMetaData = dict
    _put("homeassistant.components.recorder.models", ha_rec_models)

    ha_rec_stats = types.ModuleType("homeassistant.components.recorder.statistics")
    ha_rec_stats.async_add_external_statistics = lambda hass, meta, stats: hass.recorder.add_external_statistics(meta, stats)
    ha_rec_stats.get_last_statistics = lambda hass, n, sid, *a: hass.recorder.get_last_statistics(hass, n, sid, *a)
    ha_rec_stats.statistics_during_period = lambda hass, s, e, sids, p, *a: hass.recorder.statistics_during_period(hass, s, e, sids, p, *a)
    _put("homeassistant.components.recorder.statistics", ha_rec_stats)

    # Sensor stubs
    ha_sensor = types.ModuleType("homeassistant.components.sensor")
    ha_sensor.SensorDeviceClass = types.SimpleNamespace(
        ENERGY="energy", MONETARY="monetary", TIMESTAMP="timestamp"
    )
    ha_sensor.SensorStateClass = types.SimpleNamespace(
        TOTAL="total", TOTAL_INCREASING="total_increasing"
    )

    class _SensorEntity:
        @property
        def name(self):
            return getattr(self, "_attr_name", None)

        @property
        def unique_id(self):
            return getattr(self, "_attr_unique_id", None)

        @property
        def device_class(self):
            return getattr(self, "_attr_device_class", None)

        @property
        def state_class(self):
            return getattr(self, "_attr_state_class", None)

        @property
        def native_unit_of_measurement(self):
            return getattr(self, "_attr_native_unit_of_measurement", None)

    ha_sensor.SensorEntity = _SensorEntity
    _put("homeassistant.components.sensor", ha_sensor)

    # Binary sensor stubs
    ha_bsensor = types.ModuleType("homeassistant.components.binary_sensor")
    ha_bsensor.BinarySensorDeviceClass = types.SimpleNamespace(PROBLEM="problem")

    class _BinarySensorEntity:
        @property
        def name(self):
            return getattr(self, "_attr_name", None)

        @property
        def unique_id(self):
            return getattr(self, "_attr_unique_id", None)

        @property
        def device_class(self):
            return getattr(self, "_attr_device_class", None)

        @property
        def icon(self):
            return getattr(self, "_attr_icon", None)

    ha_bsensor.BinarySensorEntity = _BinarySensorEntity
    _put("homeassistant.components.binary_sensor", ha_bsensor)

    # Helpers stubs
    ha_helpers = types.ModuleType("homeassistant.helpers")
    _put("homeassistant.helpers", ha_helpers)

    ha_er = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_er.async_get = lambda hass: types.SimpleNamespace(
        async_get_entity_id=lambda *a: None, async_remove=lambda *a: None
    )
    ha_helpers.entity_registry = ha_er
    _put("homeassistant.helpers.entity_registry", ha_er)

    ha_entity = types.ModuleType("homeassistant.helpers.entity")
    ha_entity.DeviceInfo = dict
    ha_entity.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic")
    _put("homeassistant.helpers.entity", ha_entity)

    ha_ep = types.ModuleType("homeassistant.helpers.entity_platform")
    ha_ep.AddEntitiesCallback = Any
    _put("homeassistant.helpers.entity_platform", ha_ep)

    ha_event = types.ModuleType("homeassistant.helpers.event")
    ha_event.async_track_time_interval = lambda *a, **kw: (lambda: None)
    _put("homeassistant.helpers.event", ha_event)

    ha_storage = types.ModuleType("homeassistant.helpers.storage")

    class _FakeStore:
        def __init__(self, hass, _version, key) -> None:
            self._hass = hass
            self._key = key
            self._data = None

        async def async_load(self):
            backing = getattr(self._hass, "store_backing", None)
            if backing is not None and self._key in backing:
                return backing[self._key]
            return self._data

        async def async_save(self, data) -> None:
            self._data = data
            backing = getattr(self._hass, "store_backing", None)
            if backing is not None:
                backing[self._key] = data

        def async_delay_save(self, data_func, _delay: float = 0) -> None:
            data = data_func()
            self._data = data
            backing = getattr(self._hass, "store_backing", None)
            if backing is not None:
                backing[self._key] = data

    ha_storage.Store = _FakeStore
    _put("homeassistant.helpers.storage", ha_storage)

    ha_uc = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _FakeCoordinator:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, hass, logger, name, update_interval=None) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None

        def async_set_updated_data(self, data) -> None:
            self.data = data

        async def async_refresh(self) -> None:
            self.data = await self._async_update_data()

    class _FakeCoordinatorEntity:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

        @property
        def available(self) -> bool:
            return True

    ha_uc.DataUpdateCoordinator = _FakeCoordinator
    ha_uc.CoordinatorEntity = _FakeCoordinatorEntity
    ha_uc.UpdateFailed = type("UpdateFailed", (Exception,), {})
    _put("homeassistant.helpers.update_coordinator", ha_uc)

    return saved


def _restore_stubs(saved: dict[str, Any]) -> None:
    for key in list(sys.modules):
        if key == "custom_components" or key.startswith("custom_components.cez_distribuce"):
            sys.modules.pop(key, None)
    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig


class _FakeApiClient:
    """Fake API client returning deterministic 15-min PND data."""

    def __init__(
        self,
        pnd_values: list[dict[str, Any]],
        *,
        status_codes: list[str] | None = None,
        readings: list[dict[str, Any]] | None = None,
        signals: dict[str, Any] | None = None,
        outages: list[dict[str, Any]] | None = None,
    ) -> None:
        self._pnd_values = pnd_values
        self._status_codes = status_codes if status_codes is not None else ["03", "05"]
        self._readings = readings if readings is not None else []
        self._signals = signals if signals is not None else {"signals": []}
        self._outages = outages if outages is not None else []
        self.pnd_calls = 0
        self.pnd_status_calls = 0
        self.requested_assembly_codes: list[str] = []

    async def get_readings(self, _uid: str):
        return list(self._readings)

    async def get_signals(self, _ean: str):
        return dict(self._signals)

    async def get_outages(self, _ean: str):
        return list(self._outages)

    async def get_pnd_data(self, **kwargs):
        self.pnd_calls += 1
        if "assembly_code" in kwargs:
            self.requested_assembly_codes.append(kwargs["assembly_code"])
        return [{"unit": "kW", "values": list(self._pnd_values)}]

    async def get_pnd_status(self, _partner: str):
        self.pnd_status_calls += 1
        return {"assemblyCodes": list(self._status_codes)}


def _make_day_15min_values(local_date_str: str, kw_value: float = 2.0) -> list[dict[str, Any]]:
    """Generate 96 quarter-hour kW points covering one full Europe/Prague calendar day.

    MEPAS `pnd/data` returns `time` as the END of each interval in UTC.
    """
    day = datetime.fromisoformat(local_date_str).replace(tzinfo=PRAGUE)
    next_day = day + timedelta(days=1)
    cur = day.astimezone(timezone.utc)
    end = next_day.astimezone(timezone.utc)
    values = []
    while cur < end:
        cur += timedelta(minutes=15)
        values.append({"time": cur.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "value": kw_value})
    return values


class TestCoordinatorAndSensors(unittest.IsolatedAsyncioTestCase):
    """End-to-end tests of deduplication, throttling, recalculation, sessions, and sensors."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._saved_modules = _install_stubs()
        cls.cez_init = importlib.import_module("custom_components.cez_distribuce")
        cls.cez_api = importlib.import_module("custom_components.cez_distribuce.api")
        cls.cez_const = importlib.import_module("custom_components.cez_distribuce.const")
        cls.cez_flow = importlib.import_module("custom_components.cez_distribuce.config_flow")
        cls.cez_coord = importlib.import_module("custom_components.cez_distribuce.coordinator")
        cls.cez_sensor = importlib.import_module("custom_components.cez_distribuce.sensor")
        cls.cez_bsensor = importlib.import_module("custom_components.cez_distribuce.binary_sensor")
        cls.dt_util = sys.modules["homeassistant.util.dt"]

    @classmethod
    def tearDownClass(cls) -> None:
        _restore_stubs(cls._saved_modules)

    def tearDown(self) -> None:
        self.dt_util._now_override = None

    async def test_dedup_throttling_monthly_and_last_day_sensors(self) -> None:
        # Pretend "now" is 2026-04-15 10:00 Prague (CEST = UTC+2)
        fixed_now = datetime(2026, 4, 15, 10, 0, tzinfo=PRAGUE)
        self.dt_util._now_override = fixed_now

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

        orig_dt = self.cez_coord.datetime
        self.cez_coord.datetime = _FixedDatetime
        self.addCleanup(setattr, self.cez_coord, "datetime", orig_dt)
        recorder = _FakeRecorder()
        hass = _FakeHass(recorder, currency="CZK")

        # Provide 2 full local days: 2026-04-13 and 2026-04-14 (up to midnight 2026-04-15 00:00 CEST)
        # Each 15-min interval is 2.0 kW -> 0.5 kWh -> 2.0 kWh/hour -> 48.0 kWh/day
        # NT is 00:00-06:00 (6 hours = 12.0 kWh/day), VT is 18 hours = 36.0 kWh/day
        pnd_values = (
            _make_day_15min_values("2026-04-13", kw_value=2.0)
            + _make_day_15min_values("2026-04-14", kw_value=2.0)
        )
        client = _FakeApiClient(pnd_values)
        signals_data = {
            "signals": [
                {"signal": "a3b7dp01", "datum": "13.04.2026", "casy": "00:00-06:00"},
                {"signal": "a3b7dp01", "datum": "14.04.2026", "casy": "00:00-06:00"},
            ]
        }

        coordinator = self.cez_coord.CezDistribuceCoordinator(
            hass,
            client,
            ean="859182400100000001",
            uid="uid-1",
            partner="p-1",
            anlage="a-1",
            om_type="S",
            hdo_signal="a3b7dp01",
            price_vt=5.0,
            price_nt=2.0,
        )
        coordinator.data = {self.cez_const.DATA_SIGNALS: signals_data}
        await coordinator._async_update_hdo_history(signals_data)

        # First import writes all 6 external statistics (consumption, vt, nt, cost_vt, cost_nt, cost_total)
        await coordinator._async_update_pnd_statistics(signals_data)
        self.assertEqual(len(recorder.write_calls), 6)
        first_pnd_calls = client.pnd_calls
        self.assertGreater(first_pnd_calls, 0)

        # Second call immediately afterwards should be throttled (data already reaches today's local midnight)
        await coordinator._async_update_pnd_statistics(signals_data)
        self.assertEqual(client.pnd_calls, first_pnd_calls)
        self.assertEqual(len(recorder.write_calls), 6)

        # Force fetch with identical data -> fetches from MEPAS, but dedup skips ALL 6 Recorder writes!
        await coordinator._async_update_pnd_statistics(
            signals_data,
            force_fetch_start=datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc),
        )
        self.assertGreater(client.pnd_calls, first_pnd_calls)
        self.assertEqual(len(recorder.write_calls), 6)

        # Compute monthly & last-day summaries
        await coordinator._async_update_monthly_consumption(signals_data)
        entry = _FakeEntry(data={self.cez_const.CONF_EAN: coordinator.ean})

        # Monthly consumption: 2 days * 48 kWh = 96 kWh (VT=72, NT=24)
        m_tot = self.cez_sensor.CezMonthlyConsumptionSensor(coordinator, entry, coordinator.ean)
        m_vt = self.cez_sensor.CezMonthlyConsumptionSensor(coordinator, entry, coordinator.ean, "VT")
        m_nt = self.cez_sensor.CezMonthlyConsumptionSensor(coordinator, entry, coordinator.ean, "NT")
        self.assertAlmostEqual(m_tot.native_value, 96.0, places=3)
        self.assertAlmostEqual(m_vt.native_value, 72.0, places=3)
        self.assertAlmostEqual(m_nt.native_value, 24.0, places=3)

        # Monthly cost: VT = 72 * 5 = 360 CZK, NT = 24 * 2 = 48 CZK, total = 408 CZK
        mc_tot = self.cez_sensor.CezMonthlyCostSensor(coordinator, entry, coordinator.ean)
        mc_vt = self.cez_sensor.CezMonthlyCostSensor(coordinator, entry, coordinator.ean, "VT")
        mc_nt = self.cez_sensor.CezMonthlyCostSensor(coordinator, entry, coordinator.ean, "NT")
        self.assertEqual(mc_tot.device_class, "monetary")
        self.assertEqual(mc_tot.state_class, "total")
        self.assertEqual(mc_tot.native_unit_of_measurement, "CZK")
        self.assertEqual(mc_tot.last_reset, datetime(2026, 4, 1, 0, 0, tzinfo=PRAGUE))
        self.assertAlmostEqual(mc_tot.native_value, 408.0, places=2)
        self.assertAlmostEqual(mc_vt.native_value, 360.0, places=2)
        self.assertAlmostEqual(mc_nt.native_value, 48.0, places=2)

        # Last complete day (2026-04-14): 48 kWh total, 36 VT, 12 NT
        ld_tot = self.cez_sensor.CezLastDayConsumptionSensor(coordinator, entry, coordinator.ean)
        ld_vt = self.cez_sensor.CezLastDayConsumptionSensor(coordinator, entry, coordinator.ean, "VT")
        ld_nt = self.cez_sensor.CezLastDayConsumptionSensor(coordinator, entry, coordinator.ean, "NT")
        self.assertEqual(ld_tot.last_reset, datetime(2026, 4, 14, 0, 0, tzinfo=PRAGUE))
        self.assertAlmostEqual(ld_tot.native_value, 48.0, places=3)
        self.assertAlmostEqual(ld_vt.native_value, 36.0, places=3)
        self.assertAlmostEqual(ld_nt.native_value, 12.0, places=3)
        self.assertEqual(ld_tot.extra_state_attributes["datum"], "2026-04-14")
        self.assertAlmostEqual(ld_tot.extra_state_attributes["naklady"], 204.0, places=2)

        # Now change prices and run recalculate_costs_only=True via coordinator -> NO MEPAS call, costs updated!
        coordinator._price_vt = 10.0
        coordinator._price_nt = 4.0
        pnd_before_recalc = client.pnd_calls
        writes_before_recalc = len(recorder.write_calls)

        await coordinator.async_recalculate_statistics(days=30, recalculate_costs_only=True)
        self.assertEqual(client.pnd_calls, pnd_before_recalc)
        # All 3 cost stats changed so exactly 3 writes occurred
        self.assertEqual(len(recorder.write_calls) - writes_before_recalc, 3)
        self.assertAlmostEqual(mc_tot.native_value, 816.0, places=2)
        self.assertAlmostEqual(mc_vt.native_value, 720.0, places=2)
        self.assertAlmostEqual(mc_nt.native_value, 96.0, places=2)

        # Full recalculation (recalculate_costs_only=False) after changing HDO signal
        # Switch to a different schedule with 12h NT (00:00-12:00) -> 24 kWh NT + 24 kWh VT per day
        signals_data["signals"] = [
            {"signal": "b2c3dp02", "datum": "13.04.2026", "casy": "00:00-12:00"},
            {"signal": "b2c3dp02", "datum": "14.04.2026", "casy": "00:00-12:00"},
        ]
        coordinator._hdo_signal = "b2c3dp02"
        coordinator._hdo_history = None
        await coordinator._async_update_hdo_history(signals_data)
        await coordinator.async_recalculate_statistics(days=30, recalculate_costs_only=False)
        self.assertAlmostEqual(m_vt.native_value, 48.0, places=3)
        self.assertAlmostEqual(m_nt.native_value, 48.0, places=3)

    async def test_single_tariff_hides_hdo_sensors_and_exposes_price(self) -> None:
        recorder = _FakeRecorder()
        hass = _FakeHass(recorder)
        ean = "859182400100000002"
        entry = _FakeEntry(
            entry_id="entry-single",
            data={
                self.cez_const.CONF_EAN: ean,
                self.cez_const.CONF_HDO_SIGNAL: "",
                self.cez_const.CONF_PRICE_VT: 6.5,
                self.cez_const.CONF_PRICE_NT: 3.0,
            },
        )
        coordinator = self.cez_coord.CezDistribuceCoordinator(
            hass, _FakeApiClient([]), ean=ean, uid="u", hdo_signal="", price_vt=6.5, price_nt=3.0
        )
        coordinator.data = {self.cez_const.DATA_SIGNALS: {"signals": []}}
        hass.data[self.cez_const.DOMAIN] = {entry.entry_id: coordinator}

        added: list[Any] = []
        await self.cez_sensor.async_setup_entry(hass, entry, added.extend)

        added_types = {type(ent) for ent in added}
        self.assertNotIn(self.cez_sensor.CezHdoStateSensor, added_types)
        self.assertNotIn(self.cez_sensor.CezHdoScheduleSensor, added_types)
        self.assertNotIn(self.cez_sensor.CezTariffBoundarySensor, added_types)
        self.assertNotIn(self.cez_sensor.CezTariffCountdownSensor, added_types)
        self.assertIn(self.cez_sensor.CezMonthlyCostSensor, added_types)
        self.assertIn(self.cez_sensor.CezLastDayConsumptionSensor, added_types)

        price_sensor = next(
            ent for ent in added if isinstance(ent, self.cez_sensor.CezCurrentPriceSensor)
        )
        self.assertAlmostEqual(price_sensor.native_value, 6.5, places=2)
        self.assertEqual(price_sensor.extra_state_attributes["stav_hdo"], "VT")

    async def test_api_client_reuses_and_closes_sessions(self) -> None:
        client = self.cez_api.CezDistribuceApiClient(username="u", password="p")
        async with client:
            auth1 = client._get_auth_session()
            auth2 = client._get_auth_session()
            anon1 = client._get_anon_session()
            anon2 = client._get_anon_session()
            mepas1 = client._get_mepas_session()
            mepas2 = client._get_mepas_session()
            self.assertIs(auth1, auth2)
            self.assertIs(anon1, anon2)
            self.assertIs(mepas1, mepas2)
            self.assertIsNot(auth1, anon1)
            self.assertIsNot(auth1, mepas1)
            self.assertFalse(auth1.closed)
            self.assertFalse(anon1.closed)
            self.assertFalse(mepas1.closed)

        self.assertTrue(auth1.closed)
        self.assertTrue(anon1.closed)
        self.assertTrue(mepas1.closed)
        self.assertIsNone(client._auth_session)
        self.assertIsNone(client._anon_session)
        self.assertIsNone(client._mepas_session)

    async def test_service_registration_and_filtering(self) -> None:
        recorder = _FakeRecorder()
        hass = _FakeHass(recorder)
        called: list[tuple[str, int, bool]] = []

        c_cons = self.cez_coord.CezDistribuceCoordinator(
            hass, _FakeApiClient([]), ean="859182400100000010", uid="u1", om_type="S"
        )
        c_prod = self.cez_coord.CezDistribuceCoordinator(
            hass, _FakeApiClient([]), ean="859182400100000020", uid="u2", om_type="V"
        )

        async def _rec_cons(*, days=30, recalculate_costs_only=False):
            called.append((c_cons.ean, days, recalculate_costs_only))

        async def _rec_prod(*, days=30, recalculate_costs_only=False):
            called.append((c_prod.ean, days, recalculate_costs_only))

        c_cons.async_recalculate_statistics = _rec_cons  # type: ignore[method-assign]
        c_prod.async_recalculate_statistics = _rec_prod  # type: ignore[method-assign]

        hass.data[self.cez_const.DOMAIN] = {"e1": c_cons, "e2": c_prod}
        self.cez_init._async_register_services(hass)
        self.assertTrue(
            hass.services.has_service(
                self.cez_const.DOMAIN, self.cez_const.SERVICE_RECALCULATE_STATISTICS
            )
        )

        # Without EAN -> skips production coordinator
        await hass.services.async_call(
            self.cez_const.DOMAIN,
            self.cez_const.SERVICE_RECALCULATE_STATISTICS,
            {"days": 14, "recalculate_costs_only": True},
        )
        self.assertEqual(called, [("859182400100000010", 14, True)])

        # With non-existent EAN -> calls nothing
        called.clear()
        await hass.services.async_call(
            self.cez_const.DOMAIN,
            self.cez_const.SERVICE_RECALCULATE_STATISTICS,
            {"ean": "000000000000000000", "days": 7, "recalculate_costs_only": False},
        )
        self.assertEqual(called, [])

    async def test_options_flow_includes_hdo_signal_when_available(self) -> None:
        recorder = _FakeRecorder()
        hass = _FakeHass(recorder)
        entry = _FakeEntry(
            entry_id="entry-opt",
            data={
                self.cez_const.CONF_EAN: "859182400100000099",
                self.cez_const.CONF_HDO_SIGNAL: "a3b7dp01",
                self.cez_const.CONF_PRICE_VT: 5.0,
                self.cez_const.CONF_PRICE_NT: 2.5,
            },
        )
        coordinator = self.cez_coord.CezDistribuceCoordinator(
            hass, _FakeApiClient([]), ean="859182400100000099", uid="u"
        )
        coordinator.data = {
            self.cez_const.DATA_SIGNALS: {
                "signals": [
                    {"signal": "a3b7dp01", "datum": "01.04.2026", "casy": "00:00-06:00"},
                    {"signal": "b2c3dp02", "datum": "01.04.2026", "casy": "00:00-08:00"},
                ]
            }
        }
        hass.data[self.cez_const.DOMAIN] = {entry.entry_id: coordinator}

        flow = self.cez_flow.CezOptionsFlow()
        flow.hass = hass
        flow.config_entry = entry
        flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
        flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}

        form = await flow.async_step_init()
        self.assertIn(self.cez_const.CONF_HDO_SIGNAL, form["data_schema"])
        self.assertIn(self.cez_const.CONF_PRICE_VT, form["data_schema"])
        self.assertIn(self.cez_const.CONF_PRICE_NT, form["data_schema"])

        saved = await flow.async_step_init(
            {self.cez_const.CONF_HDO_SIGNAL: "b2c3dp02", self.cez_const.CONF_PRICE_VT: 6.0, self.cez_const.CONF_PRICE_NT: 3.0}
        )
        self.assertEqual(saved["data"][self.cez_const.CONF_HDO_SIGNAL], "b2c3dp02")
        self.assertEqual(saved["data"][self.cez_const.CONF_PRICE_VT], 6.0)
        self.assertEqual(saved["data"][self.cez_const.CONF_PRICE_NT], 3.0)

    async def test_tariff_binary_sensors_invert_and_respect_single_tariff_and_production(self) -> None:
        recorder = _FakeRecorder()
        hass = _FakeHass(recorder)
        ean = "859182400100000055"
        entry = _FakeEntry(
            entry_id="entry-dual",
            data={
                self.cez_const.CONF_EAN: ean,
                self.cez_const.CONF_HDO_SIGNAL: "a3b7dp01",
            },
        )
        coordinator = self.cez_coord.CezDistribuceCoordinator(
            hass, _FakeApiClient([]), ean=ean, uid="u", hdo_signal="a3b7dp01"
        )
        coordinator.data = {
            self.cez_const.DATA_SIGNALS: {
                "signals": [
                    {"signal": "a3b7dp01", "datum": "15.04.2026", "casy": "00:00-06:00;22:00-24:00"},
                ]
            }
        }
        hass.data[self.cez_const.DOMAIN] = {entry.entry_id: coordinator}

        added: list[Any] = []
        await self.cez_bsensor.async_setup_entry(hass, entry, added.extend)
        self.assertEqual(len(added), 3)
        nt_bin = next(ent for ent in added if ent.unique_id == f"{ean}_low_tariff")
        vt_bin = next(ent for ent in added if ent.unique_id == f"{ean}_high_tariff")
        self.assertEqual(nt_bin.name, "Nízký tarif")
        self.assertEqual(vt_bin.name, "Vysoký tarif")

        # Inside NT window (03:00 Prague) -> NT=True, VT=False
        self.dt_util._now_override = datetime(2026, 4, 15, 3, 0, tzinfo=PRAGUE)
        self.assertTrue(nt_bin.is_on)
        self.assertFalse(vt_bin.is_on)
        self.assertEqual(nt_bin.extra_state_attributes["stav_hdo"], "NT")
        self.assertEqual(vt_bin.extra_state_attributes["stav_hdo"], "NT")

        # Inside VT window (12:00 Prague) -> NT=False, VT=True
        self.dt_util._now_override = datetime(2026, 4, 15, 12, 0, tzinfo=PRAGUE)
        self.assertFalse(nt_bin.is_on)
        self.assertTrue(vt_bin.is_on)
        self.assertEqual(nt_bin.extra_state_attributes["stav_hdo"], "VT")
        self.assertEqual(vt_bin.extra_state_attributes["stav_hdo"], "VT")

        # Missing HDO data -> both unknown (None)
        coordinator.data = {}
        self.assertIsNone(nt_bin.is_on)
        self.assertIsNone(vt_bin.is_on)

        # Single-tariff OM -> only outage binary sensor
        entry_single = _FakeEntry(
            entry_id="entry-single-bin",
            data={self.cez_const.CONF_EAN: ean, self.cez_const.CONF_HDO_SIGNAL: ""},
        )
        coord_single = self.cez_coord.CezDistribuceCoordinator(
            hass, _FakeApiClient([]), ean=ean, uid="u", hdo_signal=""
        )
        coord_single.data = {self.cez_const.DATA_SIGNALS: {"signals": []}}
        hass.data[self.cez_const.DOMAIN][entry_single.entry_id] = coord_single
        added_single: list[Any] = []
        await self.cez_bsensor.async_setup_entry(hass, entry_single, added_single.extend)
        self.assertEqual([type(e) for e in added_single], [self.cez_bsensor.CezOutageSensor])

        # Production / FVE OM -> only outage binary sensor
        entry_prod = _FakeEntry(
            entry_id="entry-prod-bin",
            data={
                self.cez_const.CONF_EAN: ean,
                self.cez_const.CONF_OM_TYPE: "V",
                self.cez_const.CONF_HDO_SIGNAL: "a3b7dp01",
            },
        )
        hass.data[self.cez_const.DOMAIN][entry_prod.entry_id] = coordinator
        added_prod: list[Any] = []
        await self.cez_bsensor.async_setup_entry(hass, entry_prod, added_prod.extend)
        self.assertEqual([type(e) for e in added_prod], [self.cez_bsensor.CezOutageSensor])

    async def test_deferred_initial_pnd_sync_hydrates_locally_and_skips_unknown_probes(self) -> None:
        fixed_now = datetime(2026, 4, 15, 10, 0, tzinfo=PRAGUE)
        self.dt_util._now_override = fixed_now

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

        orig_dt = self.cez_coord.datetime
        self.cez_coord.datetime = _FixedDatetime
        self.addCleanup(setattr, self.cez_coord, "datetime", orig_dt)

        recorder = _FakeRecorder()
        hass = _FakeHass(recorder, currency="CZK")
        pnd_values = (
            _make_day_15min_values("2026-04-13", kw_value=2.0)
            + _make_day_15min_values("2026-04-14", kw_value=2.0)
        )
        signals_data = {
            "signals": [
                {"signal": "a3b7dp01", "datum": "13.04.2026", "casy": "00:00-06:00"},
                {"signal": "a3b7dp01", "datum": "14.04.2026", "casy": "00:00-06:00"},
            ]
        }
        # Seed Recorder with existing data from a previous HA run
        seed_client = _FakeApiClient(pnd_values, signals=signals_data)
        seed_coord = self.cez_coord.CezDistribuceCoordinator(
            hass,
            seed_client,
            ean="859182400100000077",
            uid="uid-77",
            partner="p-77",
            anlage="a-77",
            hdo_signal="a3b7dp01",
            price_vt=5.0,
            price_nt=2.0,
        )
        await seed_coord._async_update_hdo_history(signals_data)
        await seed_coord._async_update_pnd_statistics(signals_data)

        # Now simulate HA startup with defer_initial_pnd_sync=True and many unknown codes in pnd/status
        client = _FakeApiClient(
            pnd_values,
            status_codes=["01", "03", "05", "07", "09", "11", "17"],
            signals=signals_data,
        )
        coordinator = self.cez_coord.CezDistribuceCoordinator(
            hass,
            client,
            ean="859182400100000077",
            uid="uid-77",
            partner="p-77",
            anlage="a-77",
            hdo_signal="a3b7dp01",
            price_vt=5.0,
            price_nt=2.0,
            defer_initial_pnd_sync=True,
        )
        data = await coordinator._async_update_data()
        coordinator.data = data

        # Startup refresh must NOT touch MEPAS at all, yet local Recorder sensors are ready immediately
        self.assertEqual(client.pnd_calls, 0)
        self.assertEqual(client.pnd_status_calls, 0)
        self.assertIsNotNone(coordinator.last_pnd_timestamp)
        self.assertIsNotNone(coordinator.monthly_consumption)
        self.assertAlmostEqual(coordinator.monthly_consumption["total_kwh"], 96.0, places=3)
        self.assertIsNotNone(coordinator.last_day_consumption)
        self.assertAlmostEqual(coordinator.last_day_consumption["total_kwh"], 48.0, places=3)

        # Background sync runs MEPAS once and NEVER probes unknown assembly codes (01/07/09/11/17)
        await coordinator.async_sync_pnd_in_background()
        self.assertEqual(client.pnd_status_calls, 1)
        self.assertGreater(client.pnd_calls, 0)
        self.assertEqual(set(client.requested_assembly_codes), {"03"})
        self.assertIsNotNone(coordinator._pnd_store._data)
        self.assertIsNotNone(coordinator._pnd_store._data["last_pnd_fetch"])

    async def test_persisted_pnd_state_skips_mepas_after_restart(self) -> None:
        fixed_now = datetime(2026, 4, 15, 10, 0, tzinfo=PRAGUE)
        self.dt_util._now_override = fixed_now

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

        orig_dt = self.cez_coord.datetime
        self.cez_coord.datetime = _FixedDatetime
        self.addCleanup(setattr, self.cez_coord, "datetime", orig_dt)

        recorder = _FakeRecorder()
        hass = _FakeHass(recorder, currency="CZK")
        pnd_values = (
            _make_day_15min_values("2026-04-13", kw_value=2.0)
            + _make_day_15min_values("2026-04-14", kw_value=2.0)
        )
        signals_data = {
            "signals": [
                {"signal": "a3b7dp01", "datum": "13.04.2026", "casy": "00:00-06:00"},
                {"signal": "a3b7dp01", "datum": "14.04.2026", "casy": "00:00-06:00"},
            ]
        }
        first_client = _FakeApiClient(pnd_values, signals=signals_data)
        first_coord = self.cez_coord.CezDistribuceCoordinator(
            hass,
            first_client,
            ean="859182400100000088",
            uid="uid-88",
            partner="p-88",
            anlage="a-88",
            hdo_signal="a3b7dp01",
        )
        await first_coord._async_update_hdo_history(signals_data)
        await first_coord._async_update_pnd_statistics(signals_data)
        saved_pnd_state = dict(first_coord._pnd_store._data)

        # New coordinator instance (simulating HA restart 30 minutes later) with persisted store
        restarted_client = _FakeApiClient(pnd_values, signals=signals_data)
        restarted_coord = self.cez_coord.CezDistribuceCoordinator(
            hass,
            restarted_client,
            ean="859182400100000088",
            uid="uid-88",
            partner="p-88",
            anlage="a-88",
            hdo_signal="a3b7dp01",
        )
        restarted_coord._pnd_store._data = saved_pnd_state
        await restarted_coord._async_update_hdo_history(signals_data)
        recorder.period_queries = 0
        await restarted_coord._async_update_pnd_statistics(signals_data)

        # Fresh data + recent persisted fetch -> 0 MEPAS calls and 0 period DB scans!
        self.assertEqual(restarted_client.pnd_calls, 0)
        self.assertEqual(restarted_client.pnd_status_calls, 0)
        self.assertEqual(recorder.period_queries, 0)
        self.assertIsNotNone(restarted_coord.pnd_first_timestamp)

    async def test_parallel_portal_fetches_and_batched_recorder_prefetch(self) -> None:
        fixed_now = datetime(2026, 4, 15, 10, 0, tzinfo=PRAGUE)
        self.dt_util._now_override = fixed_now

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

        orig_dt = self.cez_coord.datetime
        self.cez_coord.datetime = _FixedDatetime
        self.addCleanup(setattr, self.cez_coord, "datetime", orig_dt)

        in_flight = 0
        max_in_flight = 0

        class _SlowParallelClient(_FakeApiClient):
            async def _track(self, result):
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1
                return result

            async def get_readings(self, _uid: str):
                return await self._track(list(self._readings))

            async def get_signals(self, _ean: str):
                return await self._track(dict(self._signals))

            async def get_outages(self, _ean: str):
                return await self._track(list(self._outages))

        recorder = _FakeRecorder()
        hass = _FakeHass(recorder, currency="CZK")
        day1 = _make_day_15min_values("2026-04-13", kw_value=2.0)
        day2 = _make_day_15min_values("2026-04-14", kw_value=2.0)
        signals_data = {
            "signals": [
                {"signal": "a3b7dp01", "datum": "13.04.2026", "casy": "00:00-06:00"},
                {"signal": "a3b7dp01", "datum": "14.04.2026", "casy": "00:00-06:00"},
            ]
        }
        client = _SlowParallelClient(day1 + day2, signals=signals_data)
        coordinator = self.cez_coord.CezDistribuceCoordinator(
            hass,
            client,
            ean="859182400100000066",
            uid="uid-66",
            partner="p-66",
            anlage="a-66",
            hdo_signal="a3b7dp01",
        )
        await coordinator._async_update_data()
        # All 3 independent portal requests must run concurrently
        self.assertEqual(max_in_flight, 3)

        # Now re-import only day 2 (where min_hour - 1h exists from day 1):
        # all 6 statistics must be prefetched in ONE single statistics_during_period query!
        client._pnd_values = day2
        recorder.period_queries = 0
        await coordinator._async_update_pnd_statistics(
            signals_data,
            force_fetch_start=datetime(2026, 4, 14, 0, 0, tzinfo=PRAGUE).astimezone(timezone.utc),
        )
        self.assertEqual(recorder.period_queries, 1)

    async def test_concurrent_relogin_and_mepas_login_deduplication(self) -> None:
        client = self.cez_api.CezDistribuceApiClient(username="u", password="p")
        login_calls = 0
        mepas_calls = 0

        async def _fake_login() -> None:
            nonlocal login_calls
            login_calls += 1
            await asyncio.sleep(0.02)
            client._api_token = f"tok-{login_calls}"
            client._auth_generation += 1

        async def _fake_mepas_login() -> None:
            nonlocal mepas_calls
            mepas_calls += 1
            await asyncio.sleep(0.02)
            client._mepas_access_token = f"mepas-{mepas_calls}"
            client._mepas_generation += 1

        client.login = _fake_login  # type: ignore[method-assign]
        client.login_mepas = _fake_mepas_login  # type: ignore[method-assign]

        # Two concurrent 401 failures on generation 0 trigger only ONE login()
        await asyncio.gather(
            client._ensure_auth_relogin(0),
            client._ensure_auth_relogin(0),
        )
        self.assertEqual(login_calls, 1)
        self.assertEqual(client._auth_generation, 1)

        # Two concurrent initial MEPAS requests trigger only ONE login_mepas()
        await asyncio.gather(
            client._ensure_mepas_token(),
            client._ensure_mepas_token(),
        )
        self.assertEqual(mepas_calls, 1)

    async def test_async_setup_entry_defers_pnd_to_background_task(self) -> None:
        recorder = _FakeRecorder()
        hass = _FakeHass(recorder, currency="CZK")
        bg_tasks: list[asyncio.Task[Any]] = []
        unload_callbacks: list[Any] = []

        def _create_bg_task(coro, name):
            task = asyncio.create_task(coro, name=name)
            bg_tasks.append(task)
            return task

        hass.async_create_background_task = _create_bg_task

        async def _forward(_entry, _platforms):
            return True

        hass.config_entries = types.SimpleNamespace(
            async_forward_entry_setups=_forward,
            async_update_entry=lambda *a, **kw: None,
        )

        entry = _FakeEntry(
            entry_id="entry-bg",
            title="Doma",
            data={
                self.cez_const.CONF_USERNAME: "u",
                self.cez_const.CONF_PASSWORD: "p",
                self.cez_const.CONF_EAN: "859182400100000044",
                "uid": "uid-44",
                self.cez_const.CONF_PARTNER: "p-44",
                self.cez_const.CONF_ANLAGE: "a-44",
                self.cez_const.CONF_HDO_SIGNAL: "a3b7dp01",
            },
        )
        entry.async_on_unload = unload_callbacks.append

        fake_client = _FakeApiClient(
            _make_day_15min_values("2026-04-14", kw_value=2.0),
            signals={
                "signals": [
                    {"signal": "a3b7dp01", "datum": "14.04.2026", "casy": "00:00-06:00"},
                ]
            },
        )
        fake_client.environment_options = []
        fake_client.preferred_environment_missing = False
        fake_client.login = lambda: asyncio.sleep(0)
        fake_client.async_close = lambda: asyncio.sleep(0)

        orig_client_cls = self.cez_init.CezDistribuceApiClient
        orig_first_refresh = getattr(
            self.cez_coord.CezDistribuceCoordinator, "async_config_entry_first_refresh", None
        )

        async def _first_refresh(coord_self):
            coord_self.data = await coord_self._async_update_data()

        self.cez_init.CezDistribuceApiClient = lambda **_kw: fake_client
        self.cez_coord.CezDistribuceCoordinator.async_config_entry_first_refresh = _first_refresh
        self.addCleanup(setattr, self.cez_init, "CezDistribuceApiClient", orig_client_cls)
        if orig_first_refresh is None:
            self.addCleanup(
                delattr,
                self.cez_coord.CezDistribuceCoordinator,
                "async_config_entry_first_refresh",
            )
        else:
            self.addCleanup(
                setattr,
                self.cez_coord.CezDistribuceCoordinator,
                "async_config_entry_first_refresh",
                orig_first_refresh,
            )

        ok = await self.cez_init.async_setup_entry(hass, entry)
        self.assertTrue(ok)
        # Setup finished without blocking on MEPAS!
        self.assertEqual(fake_client.pnd_calls, 0)
        self.assertEqual(fake_client.pnd_status_calls, 0)
        self.assertEqual(len(bg_tasks), 1)

        # Let the background task finish -> MEPAS sync completes and writes statistics
        await asyncio.gather(*bg_tasks)
        self.assertEqual(fake_client.pnd_status_calls, 1)
        self.assertGreater(fake_client.pnd_calls, 0)

    async def test_async_setup_entry_uses_portal_cache_and_defers_login_to_background(self) -> None:
        fixed_now = datetime(2026, 4, 15, 10, 0, tzinfo=PRAGUE)
        self.dt_util._now_override = fixed_now

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

        orig_dt = self.cez_coord.datetime
        self.cez_coord.datetime = _FixedDatetime
        self.addCleanup(setattr, self.cez_coord, "datetime", orig_dt)

        recorder = _FakeRecorder()
        hass = _FakeHass(recorder, currency="CZK")
        hass.store_backing = {}
        bg_tasks: list[asyncio.Task[Any]] = []

        def _create_bg_task(coro, name):
            task = asyncio.create_task(coro, name=name)
            bg_tasks.append(task)
            return task

        hass.async_create_background_task = _create_bg_task

        async def _forward(_entry, _platforms):
            return True

        hass.config_entries = types.SimpleNamespace(
            async_forward_entry_setups=_forward,
            async_update_entry=lambda *a, **kw: None,
        )

        ean = "859182400100000033"
        signals_data = {
            "signals": [
                {"signal": "a3b7dp01", "datum": "13.04.2026", "casy": "00:00-06:00"},
                {"signal": "a3b7dp01", "datum": "14.04.2026", "casy": "00:00-06:00"},
            ]
        }
        pnd_values = (
            _make_day_15min_values("2026-04-13", kw_value=2.0)
            + _make_day_15min_values("2026-04-14", kw_value=2.0)
        )

        class _TrackingClient(_FakeApiClient):
            def __init__(self) -> None:
                super().__init__(
                    pnd_values,
                    readings=[{"datumOdectu": "2026-04-12", "stavVt": "100", "stavNt": "50"}],
                    signals=signals_data,
                    outages=[],
                )
                self.environment_options = []
                self.preferred_environment_missing = False
                self.login_calls = 0
                self.portal_calls = 0

            async def login(self) -> None:
                self.login_calls += 1

            async def async_close(self) -> None:
                return None

            async def get_readings(self, uid: str):
                self.portal_calls += 1
                return await super().get_readings(uid)

            async def get_signals(self, e: str):
                self.portal_calls += 1
                return await super().get_signals(e)

            async def get_outages(self, e: str):
                self.portal_calls += 1
                return await super().get_outages(e)

        clients: list[_TrackingClient] = []

        def _make_client(**_kw):
            c = _TrackingClient()
            clients.append(c)
            return c

        orig_client_cls = self.cez_init.CezDistribuceApiClient
        orig_first_refresh = getattr(
            self.cez_coord.CezDistribuceCoordinator, "async_config_entry_first_refresh", None
        )

        async def _first_refresh(coord_self):
            coord_self.data = await coord_self._async_update_data()

        self.cez_init.CezDistribuceApiClient = _make_client
        self.cez_coord.CezDistribuceCoordinator.async_config_entry_first_refresh = _first_refresh
        self.addCleanup(setattr, self.cez_init, "CezDistribuceApiClient", orig_client_cls)
        if orig_first_refresh is None:
            self.addCleanup(
                delattr,
                self.cez_coord.CezDistribuceCoordinator,
                "async_config_entry_first_refresh",
            )
        else:
            self.addCleanup(
                setattr,
                self.cez_coord.CezDistribuceCoordinator,
                "async_config_entry_first_refresh",
                orig_first_refresh,
            )

        entry = _FakeEntry(
            entry_id="entry-cached",
            title="Doma",
            data={
                self.cez_const.CONF_USERNAME: "u",
                self.cez_const.CONF_PASSWORD: "p",
                self.cez_const.CONF_EAN: ean,
                "uid": "uid-33",
                self.cez_const.CONF_PARTNER: "p-33",
                self.cez_const.CONF_ANLAGE: "a-33",
                self.cez_const.CONF_HDO_SIGNAL: "a3b7dp01",
            },
        )
        entry.async_on_unload = lambda _cb: None

        # 1st startup (cold, empty cache): performs synchronous login + portal fetch, saves cache
        self.assertTrue(await self.cez_init.async_setup_entry(hass, entry))
        self.assertEqual(clients[0].login_calls, 1)
        self.assertEqual(clients[0].portal_calls, 3)
        await asyncio.gather(*bg_tasks)
        bg_tasks.clear()
        self.assertIn(f"{self.cez_const.DOMAIN}.portal_cache.{ean}", hass.store_backing)

        # 2nd startup (simulated HA restart with warm cache): ZERO network calls during setup!
        self.assertTrue(await self.cez_init.async_setup_entry(hass, entry))
        restarted_client = clients[1]
        restarted_coord = hass.data[self.cez_const.DOMAIN][entry.entry_id]
        self.assertEqual(restarted_client.login_calls, 0)
        self.assertEqual(restarted_client.portal_calls, 0)
        self.assertEqual(restarted_client.pnd_calls, 0)
        # Entities already have valid data from cache + local Recorder!
        self.assertIsNotNone(restarted_coord.data)
        self.assertIn(self.cez_const.DATA_SIGNALS, restarted_coord.data)
        self.assertIsNotNone(restarted_coord.last_successful_update)
        self.assertIsNotNone(restarted_coord.monthly_consumption)
        self.assertAlmostEqual(restarted_coord.monthly_consumption["total_kwh"], 96.0, places=3)
        self.assertEqual(len(bg_tasks), 1)

        # Background startup completes login + fresh portal refresh (and skips MEPAS via persisted throttle)
        await asyncio.gather(*bg_tasks)
        self.assertEqual(restarted_client.login_calls, 1)
        self.assertEqual(restarted_client.portal_calls, 3)
        self.assertEqual(restarted_client.pnd_calls, 0)

    async def test_cached_startup_background_auth_error_triggers_reauth_and_outage_keeps_cache(self) -> None:
        recorder = _FakeRecorder()
        hass = _FakeHass(recorder, currency="CZK")
        ean = "859182400100000034"
        hass.store_backing = {
            f"{self.cez_const.DOMAIN}.portal_cache.{ean}": {
                "om_type": "S",
                "last_successful_update": "2026-04-15T07:00:00+00:00",
                "data": {
                    self.cez_const.DATA_READINGS: [],
                    self.cez_const.DATA_SIGNALS: {"signals": []},
                    self.cez_const.DATA_OUTAGES: [],
                },
            }
        }
        bg_tasks: list[asyncio.Task[Any]] = []
        hass.async_create_background_task = lambda coro, name: (
            bg_tasks.append(t := asyncio.create_task(coro, name=name)) or t
        )

        async def _forward(_entry, _platforms):
            return True

        hass.config_entries = types.SimpleNamespace(
            async_forward_entry_setups=_forward,
            async_update_entry=lambda *a, **kw: None,
        )
        reauth_called: list[Any] = []
        entry = _FakeEntry(
            entry_id="entry-reauth",
            title="Doma",
            data={
                self.cez_const.CONF_USERNAME: "u",
                self.cez_const.CONF_PASSWORD: "bad",
                self.cez_const.CONF_EAN: ean,
                "uid": "uid-34",
            },
        )
        entry.async_on_unload = lambda _cb: None
        entry.async_start_reauth = reauth_called.append

        login_exc: Exception = self.cez_api.CezInvalidCredentialsError("bad pass")

        class _FailingLoginClient(_FakeApiClient):
            def __init__(self) -> None:
                super().__init__([])
                self.environment_options = []
                self.preferred_environment_missing = False

            async def login(self) -> None:
                raise login_exc

            async def async_close(self) -> None:
                return None

        orig_client_cls = self.cez_init.CezDistribuceApiClient
        self.cez_init.CezDistribuceApiClient = lambda **_kw: _FailingLoginClient()
        self.addCleanup(setattr, self.cez_init, "CezDistribuceApiClient", orig_client_cls)

        # Case 1: Invalid credentials in background -> triggers reauth
        self.assertTrue(await self.cez_init.async_setup_entry(hass, entry))
        await asyncio.gather(*bg_tasks)
        bg_tasks.clear()
        self.assertEqual(len(reauth_called), 1)

        # Case 2: Transient outage in background -> does NOT trigger reauth, keeps cached data
        reauth_called.clear()
        login_exc = self.cez_api.CezPortalUnavailableError("night maintenance")
        self.assertTrue(await self.cez_init.async_setup_entry(hass, entry))
        await asyncio.gather(*bg_tasks)
        self.assertEqual(len(reauth_called), 0)
        coord = hass.data[self.cez_const.DOMAIN][entry.entry_id]
        self.assertIn(self.cez_const.DATA_SIGNALS, coord.data)

        # Verify lazy _ensure_auth_login in CezDistribuceApiClient when _api_token is None
        api_client = self.cez_api.CezDistribuceApiClient(username="u", password="p")
        lazy_logins = 0

        async def _fake_login() -> None:
            nonlocal lazy_logins
            lazy_logins += 1
            api_client._api_token = "fresh-tok"
            api_client._auth_generation += 1

        api_client.login = _fake_login  # type: ignore[method-assign]
        await asyncio.gather(api_client._ensure_auth_login(), api_client._ensure_auth_login())
        self.assertEqual(lazy_logins, 1)


if __name__ == "__main__":
    unittest.main()
