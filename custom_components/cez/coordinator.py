"""Koordinátor aktualizací dat pro ČEZ Distribuce."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)

try:
    from homeassistant.components.recorder.statistics import StatisticMeanType
except ImportError:  # starší HA bez StatisticMeanType - has_mean/has_sum stačí
    StatisticMeanType = None  # type: ignore[assignment,misc]
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CezApiError,
    CezAuthError,
    CezDistribuceApiClient,
    CezInvalidCredentialsError,
    CezSupplyPointUidRejectedError,
)
from .const import (
    DATA_OUTAGES,
    DATA_READINGS,
    DATA_SIGNALS,
    DEFAULT_PRICE_NT,
    DEFAULT_PRICE_VT,
    DOMAIN,
    OM_TYPE_CONSUMPTION,
    OM_TYPES_PRODUCTION,
    PND_ASSEMBLY_15MIN,
    PND_ASSEMBLY_DAILY_REGISTERS,
    PND_ASSEMBLY_HOURLY,
    PND_GAP_REPAIR_INTERVAL_HOURS,
    PND_GAP_SCAN_DAYS,
    PND_GAP_ZERO_RUN_HOURS,
    PND_INTERVAL_MINUTES_BY_ASSEMBLY,
    PND_KNOWN_ASSEMBLY_CODES,
    PND_TRAILING_SAFETY_DAYS,
    STATISTIC_CONSUMPTION,
    STATISTIC_CONSUMPTION_NT,
    STATISTIC_CONSUMPTION_VT,
    STATISTIC_COST_NT,
    STATISTIC_COST_TOTAL,
    STATISTIC_COST_VT,
    STATISTIC_PRODUCTION,
    UPDATE_INTERVAL_SECONDS,
    pnd_assembly_code_for,
    pnd_max_interval_days_for,
)
from .hdo_split import (
    SOURCE_KNOWN,
    SOURCE_NEAREST,
    SOURCE_WEEKDAY,
    ScheduleLookup,
    calculate_hourly_costs,
    count_schedule_sources,
    extract_signal_schedules,
    merge_schedule_history,
    split_hourly_consumption,
    split_interval_consumption,
)
from .pnd_processing import (
    build_statistic_rows,
    fetch_pnd_chunked,
    find_statistic_gaps,
    infer_interval_minutes,
    process_pnd_points,
    reading_baseline,
    reading_registers,
    summarize_pnd_points,
)

_LOGGER = logging.getLogger(__name__)

# Úložiště zapamatovaných plánů HDO (issue #49) - portál vrací plán jen
# pro dnešek a pár dní dopředu, pro rozdělení spotřeby na VT/NT potřebujeme
# i plány dní, které už portál neukazuje.
HDO_HISTORY_STORAGE_VERSION = 1

# Jak daleko do minulosti se má integrace pokusit dohledat data při úplně
# prvním spuštění (kdy ještě neexistují žádné dlouhodobé statistiky).
# Pokud chytrý elektroměr existuje kratší dobu, starší okna jednoduše
# vrátí prázdno/chybu - to se jen zaloguje a přeskočí, neshodí zbytek
# aktualizace.
MAX_BACKFILL_YEARS = 3


class CezDistribuceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Koordinátor dat ČEZ Distribuce."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CezDistribuceApiClient,
        ean: str,
        uid: str,
        partner: str = "",
        anlage: str = "",
        om_type: str = OM_TYPE_CONSUMPTION,
        hdo_signal: str = "",
        price_vt: float = DEFAULT_PRICE_VT,
        price_nt: float = DEFAULT_PRICE_NT,
        on_uid_changed: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._hdo_signal = hdo_signal or ""
        self._price_vt = float(price_vt)
        self._price_nt = float(price_nt)
        # Plány HDO po dnech {'YYYY-MM-DD': casy} (issue #49) - načtou se
        # líně při první aktualizaci, viz _async_update_hdo_history.
        self._hdo_store: Store[dict[str, Any]] = Store(
            hass, HDO_HISTORY_STORAGE_VERSION, f"{DOMAIN}.hdo_history.{ean}"
        )
        self._hdo_history: dict[str, str] | None = None
        # Uloží nově dohledané UID do config entry (issue #43) - viz
        # _async_fetch_readings.
        self._on_uid_changed = on_uid_changed
        self._ean = ean
        self._uid = uid
        self._partner = partner
        self._anlage = anlage
        self._om_type = om_type or OM_TYPE_CONSUMPTION
        # Výroba/mikrozdroj (FVE) - dodávka aktivní energie DO sítě, ne
        # odběr z ní. HDO signály/tarify se jí netýkají a hodinová
        # statistika se pojmenovává jinak (viz issue #24).
        self._is_production = self._om_type in OM_TYPES_PRODUCTION
        # Spotřeba a dodávka mají u MEPAS gateway oddělené assemblyCode
        # řady (issue #24) - viz pnd_assembly_code_for()/const.py.
        self._assembly_code = pnd_assembly_code_for(self._om_type)
        self._assembly_interval_minutes = PND_INTERVAL_MINUTES_BY_ASSEMBLY[self._assembly_code]

        # Kdy se naposledy podařilo stáhnout úplně všechna data (bez fallbacku
        # na poslední známá data). Entity vypadají "živě" i při rozbitém
        # stahování (lokálně tikající odpočty), takže tohle je způsob, jak
        # obnovu skutečně ověřit – např. přes automatizaci nebo šablonu
        # (viz issue #21).
        self.last_successful_update: datetime | None = None

        # Časové razítko "data máme uložená až do..." (konec poslední
        # úspěšně naimportované hodiny spotřeby) - čte ho
        # CezConsumptionFreshnessSensor v sensor.py. None dokud neproběhne
        # první úspěšný import (nebo pokud tahle instalace 'partner'/'anlage'
        # vůbec nemá - viz __init__.py).
        self.last_pnd_timestamp: datetime | None = None

        # Kumulativní součet (kWh) hodinových dat naimportovaných do
        # dlouhodobé statistiky - tj. poslední hodnota 'sum' statistiky
        # "{DOMAIN}:<ean>_consumption/_production". U výroby/mikrozdroje
        # je to JEDINÝ použitelný zdroj pro entitu "Celková dodávka do
        # sítě" (issue #30): historie odečtů (get_readings) vrací i pro
        # výrobní EAN registry ODBĚRU (+E VT/NT, stejný fyzický
        # elektroměr), registr dodávky (-E) v ní portál vůbec nemá.
        # Počítá se od nejstarší hodiny, kterou se podařilo dohledat (viz
        # MAX_BACKFILL_YEARS), ne od instalace elektroměru - proto se
        # vedle toho drží i 'pnd_first_timestamp', aby entita uměla říct,
        # od kdy součet platí. None dokud statistika neexistuje.
        self.pnd_total_kwh: float | None = None
        self.pnd_first_timestamp: datetime | None = None

        # Diagnostika pnd/status (issue #30) - jednou za běh HA zjistíme a
        # zalogujeme, jaké assemblyCode kódy ČEZ pro tohoto partnera/EAN
        # vůbec nabízí, ať to nemusí uživatelé zjišťovat ručně přes
        # scripts/test_pnd_consumption.py --status. 'pnd_status_info' je
        # navíc vystavené jako atribut diagnostické entity (sensor.py), ať
        # je to vidět i bez čtení logu.
        self._pnd_status_checked = False
        self.pnd_status_info: dict[str, Any] | None = None

        # Odhad aktuálního stavu elektroměru (issue #40): poslední odečet
        # z portálu (VT+NT, typicky měsíční/fakturační) + hodinová
        # spotřeba z pnd/data od okamžiku odečtu. Čte ho
        # CezMeterEstimateSensor. None, dokud nejsou odečty i statistika,
        # nebo když statistika okamžik odečtu nepokrývá.
        self.meter_estimate: dict[str, Any] | None = None

        # Kdy naposledy proběhla kontrola děr ve statistice starších než
        # přeimportovací okno (issue #40) - viz _async_find_old_statistic_gap.
        self._last_gap_scan: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )

    @property
    def uid(self) -> str:
        """Aktuálně používané UID odběrného místa (může se změnit, issue #43)."""
        return self._uid

    def set_partner_and_anlage(self, partner: str, anlage: str) -> None:
        """Aktualizuje metadata potřebná pro hodinová data."""
        self._partner = partner
        self._anlage = anlage

    async def _async_update_data(self) -> dict[str, Any]:
        """Stáhne všechna potřebná data z ČEZ API."""
        previous_data = self.data or {}
        merged_data: dict[str, Any] = dict(previous_data)
        all_fresh = True

        async def _load_dataset(key: str, fetcher: Any) -> None:
            nonlocal all_fresh
            try:
                merged_data[key] = await fetcher()
            except CezInvalidCredentialsError as err:
                # Heslo se změnilo - HA nabídne reauth flow.
                raise ConfigEntryAuthFailed(str(err)) from err
            except CezAuthError as err:
                all_fresh = False
                if key in previous_data:
                    _LOGGER.warning(
                        "Nelze obnovit %s kvůli autentizaci (%s), ponechávám poslední známá data.",
                        key,
                        err,
                    )
                    return
                raise UpdateFailed(f"Chyba autentizace ČEZ: {err}") from err
            except CezApiError as err:
                all_fresh = False
                if key in previous_data:
                    _LOGGER.warning(
                        "Nelze obnovit %s (%s), ponechávám poslední známá data.",
                        key,
                        err,
                    )
                    return
                raise UpdateFailed(f"Chyba ČEZ API: {err}") from err
            except Exception as err:
                all_fresh = False
                if key in previous_data:
                    _LOGGER.warning(
                        "Neočekávaná chyba při obnově %s (%s), ponechávám poslední známá data.",
                        key,
                        err,
                    )
                    return
                raise UpdateFailed(f"Neočekávaná chyba: {err}") from err

        try:
            if not self._is_production:
                # Historie odečtů (stavVt/stavNt) a HDO spínací signály
                # dávají smysl jen u odběrného místa běžné spotřeby. U
                # výroby/mikrozdroje vrací get_readings registry ODBĚRU
                # téhož elektroměru (issue #30 - tři nezávislé účty,
                # hodnoty do kWh shodné se spotřebním EAN), takže by šlo
                # jen o zbytečné volání API a zavádějící entity; HDO
                # signály výrobní OM nemá vůbec.
                await _load_dataset(DATA_READINGS, self._async_fetch_readings)
                await _load_dataset(DATA_SIGNALS, lambda: self._client.get_signals(self._ean))
            await _load_dataset(DATA_OUTAGES, lambda: self._client.get_outages(self._ean))
        except (UpdateFailed, ConfigEntryAuthFailed):
            raise

        if not merged_data:
            raise UpdateFailed("ČEZ nevrátil žádná data.")

        if all_fresh:
            self.last_successful_update = dt_util.utcnow()

        if not self._is_production:
            try:
                await self._async_update_hdo_history(merged_data.get(DATA_SIGNALS))
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Uložení plánu HDO pro rozdělení spotřeby na VT/NT selhalo "
                    "(ostatní senzory tím nejsou dotčeny, issue #49)."
                )

        # Import hodinové/15min spotřeby do dlouhodobých statistik NIKDY
        # nesmí shodit zbytek aktualizace (odečty, HDO, odstávky mají dál
        # fungovat i kdyby se recorder API mezi verzemi HA změnilo, nebo
        # kdyby MEPAS přihlášení zrovna selhalo). Tahle část je nezávislá
        # na 'all_fresh' výše - last_successful_update sleduje portálová
        # data, last_pnd_timestamp jen čerstvost hodinové spotřeby.
        if self._partner and self._anlage:
            try:
                await self._async_update_pnd_statistics(merged_data.get(DATA_SIGNALS))
            except Exception:  # noqa: BLE001
                _LOGGER.exception(
                    "Import hodinové spotřeby (pnd/data) selhal "
                    "(ostatní senzory tím nejsou dotčeny)."
                )
            if not self._is_production:
                try:
                    await self._async_update_meter_estimate(merged_data.get(DATA_READINGS))
                except Exception:  # noqa: BLE001
                    self.meter_estimate = None
                    _LOGGER.exception(
                        "Výpočet odhadovaného stavu elektroměru selhal "
                        "(ostatní senzory tím nejsou dotčeny, issue #40)."
                    )
        else:
            _LOGGER.debug(
                "Přeskakuji import hodinové spotřeby - chybí 'partner'/'anlage' "
                "v config entry (stará instalace před touto funkcí, nebo "
                "odběratel bez chytrého elektroměru)."
            )

        return merged_data

    async def _async_fetch_readings(self) -> Any:
        """Historie odečtů s obnovou zastaralého UID (issue #43)."""
        return await self.async_call_with_uid_recovery(self._client.get_readings)

    async def async_call_with_uid_recovery(
        self, fetch: Callable[[str], Awaitable[Any]]
    ) -> Any:
        """Zavolá portálový endpoint adresovaný UID odběrného místa; při
        odmítnutém UID (ZDIP-360) dohledá aktuální UID podle EAN a dotaz
        zopakuje (issue #43).

        Portál může pro stejný EAN časem vrátit jiné UID, než je uložené
        v config entry. Nové UID se použije jen při jednoznačné shodě EAN
        a uloží se až po úspěšném dotazu s ním. Používá se pro historii
        odečtů (spotřeba) i pro detail OM při dohledání 'anlage' na
        pozadí (__init__.py) - výroba/mikrozdroj odečty nestahuje, takže
        by se jinak zastaralé UID u ní nikdy neobnovilo. Prázdné UID
        (neúplná config entry) se dohledá rovnou."""
        if self._uid:
            try:
                return await fetch(self._uid)
            except CezSupplyPointUidRejectedError as err:
                _LOGGER.info(
                    "Portál odmítl uložené UID odběrného místa (ZDIP-360), "
                    "dohledávám aktuální UID podle EAN (issue #43)."
                )
                rejected: Exception | None = err
        else:
            rejected = None
            _LOGGER.info("Config entry nemá UID odběrného místa, dohledávám ho podle EAN.")

        new_uid = await self._client.resolve_supply_point_uid(self._ean)
        if not new_uid:
            raise CezApiError(
                "Portál odmítl uložené UID odběrného místa (ZDIP-360) nebo "
                "chybí a podle EAN se nepodařilo jednoznačně dohledat nové. "
                "Zkontrolujte odběrné místo na dip.cezdistribuce.cz, "
                "případně integraci překonfigurujte (issue #43)."
            ) from rejected
        if new_uid == self._uid:
            _LOGGER.warning(
                "Portál vrací pro EAN stejné UID, a přesto ho odmítá "
                "(ZDIP-360) - nejspíš chybí oprávnění k odběrnému místu "
                "(issue #43)."
            )
            if rejected is not None:
                raise rejected
            raise CezApiError("Portál odmítl UID odběrného místa (issue #43).")
        result = await fetch(new_uid)
        self._uid = new_uid
        _LOGGER.warning(
            "UID odběrného místa se na portálu změnilo; dotaz s novým UID "
            "prošel a nové UID ukládám do konfigurace (issue #43)."
        )
        if self._on_uid_changed is not None:
            self._on_uid_changed(new_uid)
        return result

    # ------------------------------------------------------------------
    # 15min/hodinová spotřeba (appka Proud, MEPAS/AWS Gateway) jako
    # externí dlouhodobé statistiky pro HA Energy Dashboard:
    # - celková spotřeba (cez:<ean>_consumption)
    # - spotřeba VT / NT podle plánu HDO (cez:<ean>_consumption_vt / _nt)
    # - náklady VT / NT / celkem (cez:<ean>_cost_vt / _nt / _total)
    # - dodávka do sítě u FVE (cez:<ean>_production)
    # ------------------------------------------------------------------

    def statistic_id_for(self, suffix: str) -> str:
        """ID externí dlouhodobé statistiky pro danou příponu."""
        # DŮLEŽITÉ: statistic_id smí obsahovat jen [a-z0-9_] za dvojtečkou.
        safe_id = re.sub(r"[^a-z0-9_]", "_", self._ean.lower())
        return f"{DOMAIN}:{safe_id}_{suffix}"

    @property
    def statistic_id(self) -> str:
        """ID hlavní dlouhodobé statistiky tohoto odběrného místa
        ("cez:<ean>_consumption" / "cez:<ean>_production")."""
        return self._statistic_id()

    @property
    def statistic_ids(self) -> dict[str, str]:
        """Mapa všech externích statistik vytvářených pro toto odběrné místo."""
        if self._is_production:
            return {STATISTIC_PRODUCTION: self.statistic_id_for(STATISTIC_PRODUCTION)}
        return {
            STATISTIC_CONSUMPTION: self.statistic_id_for(STATISTIC_CONSUMPTION),
            STATISTIC_CONSUMPTION_VT: self.statistic_id_for(STATISTIC_CONSUMPTION_VT),
            STATISTIC_CONSUMPTION_NT: self.statistic_id_for(STATISTIC_CONSUMPTION_NT),
            STATISTIC_COST_VT: self.statistic_id_for(STATISTIC_COST_VT),
            STATISTIC_COST_NT: self.statistic_id_for(STATISTIC_COST_NT),
            STATISTIC_COST_TOTAL: self.statistic_id_for(STATISTIC_COST_TOTAL),
        }

    def _statistic_id(self) -> str:
        suffix = STATISTIC_PRODUCTION if self._is_production else STATISTIC_CONSUMPTION
        return self.statistic_id_for(suffix)

    def _currency(self) -> str:
        """Měna nastavená v Home Assistantu pro nákladové statistiky."""
        currency = getattr(getattr(self.hass, "config", None), "currency", None)
        return str(currency or "CZK").upper()

    def _build_metadata(
        self,
        statistic_id: str,
        stat_name: str,
        *,
        unit_of_measurement: str | None,
        unit_class: str | None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "has_mean": False,
            "has_sum": True,
            "name": stat_name,
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "unit_of_measurement": unit_of_measurement,
            # Vyžadováno novějšími verzemi HA (recorder od podzimu 2025 bez
            # tohohle pole import statistik odmítá). Pro měnu je None.
            "unit_class": unit_class,
        }
        if StatisticMeanType is not None:
            # Nahrazuje deprecated has_mean bool - HA 2026.11+ ho bude
            # vyžadovat povinně, se starším has_mean ale zatím funguje
            # obojí zároveň.
            metadata["mean_type"] = StatisticMeanType.NONE
        return metadata

    async def _async_has_statistic(self, recorder: Any, statistic_id: str) -> bool:
        """Zjistí, zda už v Recorderu existuje alespoň jeden řádek statistiky."""
        last_stats = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"start"}
        )
        return bool(last_stats and last_stats.get(statistic_id))

    async def _async_get_baseline_sum(
        self, recorder: Any, statistic_id: str, before_time: datetime
    ) -> float:
        """Kumulativní `sum` statistiky těsně před `before_time` (0.0 když nic není).

        Rychlá cesta čte jedinou předchozí hodinu; pokud tam je mezera, spadne
        na měsíční dotaz a poslední hodinu v nalezeném měsíci, takže nikdy
        nespadne chybně na 0.0 ani u starších děr.
        """
        prior = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            before_time - timedelta(hours=1),
            before_time,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = prior.get(statistic_id) if prior else None
        if rows and rows[-1].get("sum") is not None:
            return float(rows[-1]["sum"])

        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        end_before = before_time - timedelta(microseconds=1)
        if end_before <= epoch:
            return 0.0

        monthly = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            epoch,
            end_before,
            {statistic_id},
            "month",
            None,
            {"sum"},
        )
        month_rows = monthly.get(statistic_id) if monthly else None
        if not month_rows:
            return 0.0
        last_month_start = _stat_row_start(month_rows[-1])
        if last_month_start is not None:
            hourly = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                last_month_start,
                end_before,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            hour_rows = hourly.get(statistic_id) if hourly else None
            if hour_rows and hour_rows[-1].get("sum") is not None:
                return float(hour_rows[-1]["sum"])
        if month_rows[-1].get("sum") is not None:
            return float(month_rows[-1]["sum"])
        return 0.0

    async def _async_read_stat_rows(
        self,
        recorder: Any,
        statistic_id: str,
        start_time: datetime,
        end_time: datetime | None,
    ) -> list[tuple[datetime, float | None, float | None]]:
        """Načte řádky `(start_utc, state, sum)` z Recorderu pro dané období."""
        rows_by_id = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start_time,
            end_time,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )
        parsed: list[tuple[datetime, float | None, float | None]] = []
        for row in (rows_by_id or {}).get(statistic_id) or []:
            start = _stat_row_start(row)
            if start is None:
                continue
            state = row.get("state")
            total = row.get("sum")
            parsed.append(
                (
                    start,
                    float(state) if state is not None else None,
                    float(total) if total is not None else None,
                )
            )
        parsed.sort(key=lambda item: item[0])
        return parsed

    async def _async_write_external_statistic(
        self,
        recorder: Any,
        statistic_id: str,
        stat_name: str,
        hourly_deltas: dict[datetime, float],
        *,
        unit_of_measurement: str | None,
        unit_class: str | None,
        precision: int = 4,
    ) -> float | None:
        """Zapíše hodinové přírůstky do externí statistiky se zachováním
        monotonie, sloučením s existujícími body a posunem následných sum."""
        if not hourly_deltas:
            return None

        min_hour = min(hourly_deltas)
        max_hour = max(hourly_deltas)

        baseline_sum = await self._async_get_baseline_sum(
            recorder, statistic_id, min_hour
        )
        existing_in_window = await self._async_read_stat_rows(
            recorder,
            statistic_id,
            min_hour,
            max_hour + timedelta(seconds=1),
        )
        subsequent_rows = await self._async_read_stat_rows(
            recorder,
            statistic_id,
            max_hour + timedelta(seconds=1),
            None,
        )

        statistics, final_sum = build_statistic_rows(
            hourly_deltas,
            existing_in_window,
            baseline_sum,
            subsequent_rows,
            precision=precision,
        )
        if not statistics:
            return None

        metadata = self._build_metadata(
            statistic_id,
            stat_name,
            unit_of_measurement=unit_of_measurement,
            unit_class=unit_class,
        )
        _LOGGER.debug(
            "Importuji %d bodů statistiky %s pro %s (konečná suma=%.4f)",
            len(statistics),
            statistic_id,
            self._ean,
            final_sum,
        )
        async_add_external_statistics(self.hass, metadata, statistics)
        return final_sum

    async def _async_update_pnd_statistics(self, signals_data: Any = None) -> None:
        statistic_id = self._statistic_id()
        recorder = get_instance(self.hass)
        now = datetime.now(timezone.utc)

        # Diagnostika (issue #30) - nezávislá na zbytku metody, nikdy
        # nesmí zablokovat/shodit reálný import dat, proto vlastní try/except
        # navíc k tomu, co má metoda samotná.
        try:
            await self._async_log_pnd_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Diagnostika pnd/status selhala (%s), pokračuji.", err)

        last_stats = await recorder.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"start", "sum"}
        )
        has_existing_data = bool(last_stats and last_stats.get(statistic_id))

        # Zjistíme, zda máme k dispozici plán HDO (nebo jde o jednotarifní OM
        # bez HDO) a zda už v databázi existují tarifní a nákladové statistiky.
        lookup = ScheduleLookup(self._hdo_history or {})
        is_single_tariff = (
            not self._is_production
            and not self._hdo_signal
            and isinstance(signals_data, dict)
            and not (signals_data.get("signals") or [])
        )
        can_split_tariffs = (not self._is_production) and (bool(lookup) or is_single_tariff)
        fallback_all_vt = (not lookup) and is_single_tariff

        vt_stat_id = self.statistic_id_for(STATISTIC_CONSUMPTION_VT)
        nt_stat_id = self.statistic_id_for(STATISTIC_CONSUMPTION_NT)
        cost_vt_stat_id = self.statistic_id_for(STATISTIC_COST_VT)
        cost_nt_stat_id = self.statistic_id_for(STATISTIC_COST_NT)
        cost_total_stat_id = self.statistic_id_for(STATISTIC_COST_TOTAL)

        has_vt_stats = False
        has_nt_stats = False
        has_all_cost_stats = False
        if not self._is_production:
            has_vt_stats = await self._async_has_statistic(recorder, vt_stat_id)
            has_nt_stats = await self._async_has_statistic(recorder, nt_stat_id)
            has_all_cost_stats = (
                await self._async_has_statistic(recorder, cost_vt_stat_id)
                and await self._async_has_statistic(recorder, cost_nt_stat_id)
                and await self._async_has_statistic(recorder, cost_total_stat_id)
            )

        backfill_floor = (now - timedelta(days=365 * MAX_BACKFILL_YEARS)).replace(
            minute=0, second=0, microsecond=0
        )

        if has_existing_data:
            row = last_stats[statistic_id][0]
            # 'start' z get_last_statistics je epoch sekundy (float).
            last_known_start = datetime.fromtimestamp(row["start"], tz=timezone.utc)
            self.last_pnd_timestamp = last_known_start + timedelta(hours=1)
            # Kumulativní součet z DB hned po startu (a v cyklech, kdy
            # nepřijde nic nového) - entita "Celková dodávka do sítě"
            # nemá čekat na první nový hodinový bod (issue #30).
            if row.get("sum") is not None:
                self.pnd_total_kwh = float(row["sum"])
            if self.pnd_first_timestamp is None:
                self.pnd_first_timestamp = await self._async_find_first_statistic_start(
                    recorder, statistic_id, now
                )
            # Vždy se ohlédneme aspoň PND_TRAILING_SAFETY_DAYS zpátky, ne
            # jen od posledního naimportovaného bodu dál - ČEZ občas
            # doplňuje historii nespolehlivě (den vrátí prázdno, další den
            # rovnou jen nejnovější bod bez zpětného doplnění mezery), bez
            # tohohle by taková díra ve statistikách zůstala navždy.
            fetch_start = min(
                last_known_start + timedelta(hours=1),
                now - timedelta(days=PND_TRAILING_SAFETY_DAYS),
            )
            # Díry STARŠÍ než přeimportovací okno (issue #40) - ČEZ data
            # občas doplní později než za PND_TRAILING_SAFETY_DAYS. Chyba
            # kontroly nesmí shodit běžný import.
            try:
                gap_start = await self._async_find_old_statistic_gap(
                    recorder, statistic_id, now, fetch_start
                )
            except Exception as err:  # noqa: BLE001
                gap_start = None
                _LOGGER.debug("Kontrola děr ve statistice selhala (%s).", err)
            if gap_start is not None and gap_start < fetch_start:
                fetch_start = gap_start

            # Upgrade stávající instalace: celková spotřeba už v DB je, ale
            # rozdělené statistiky VT/NT ještě neexistují -> natáhneme jednou
            # 15min historii zpětně od začátku dostupné historie, aby měly VT/NT
            # i náklady stejnou historii a 15min přesnost.
            if can_split_tariffs and (not has_vt_stats or not has_nt_stats):
                tariff_backfill_start = (
                    max(self.pnd_first_timestamp, backfill_floor)
                    if self.pnd_first_timestamp is not None
                    else backfill_floor
                )
                if tariff_backfill_start < fetch_start:
                    fetch_start = tariff_backfill_start
                    _LOGGER.info(
                        "Statistiky VT/NT (%s, %s) zatím neexistují - spouštím "
                        "jednorázové zpětné doplnění 15min dat od %s.",
                        vt_stat_id,
                        nt_stat_id,
                        dt_util.as_local(fetch_start).isoformat(),
                    )
        else:
            fetch_start = backfill_floor
            _LOGGER.info(
                "Žádné dřívější statistiky pro %s - zkouším dohledat historii "
                "až %d let zpátky (kolik reálně existuje, závisí na tom, "
                "odkdy máš chytrý elektroměr).",
                statistic_id,
                MAX_BACKFILL_YEARS,
            )

        # Zarovnat na začátek celé hodiny, aby u 15min dat nebyly oříznuté
        # první čtvrthodiny a celá hraniční hodina se zbytečně nezahodila.
        fetch_start = fetch_start.replace(minute=0, second=0, microsecond=0)
        if fetch_start >= now:
            return

        used_assembly_code = self._assembly_code
        chunk_errors: list[Exception] = []

        def _log_chunk_error(chunk_start: datetime, chunk_end: datetime, err: Exception) -> None:
            chunk_errors.append(err)
            _LOGGER.debug(
                "pnd/data (assemblyCode=%s) pro okno %s .. %s selhalo (%s), přeskakuji.",
                used_assembly_code,
                chunk_start,
                chunk_end,
                err,
            )

        raw_points, unit = await fetch_pnd_chunked(
            self._client,
            self._partner,
            self._ean,
            fetch_start,
            now,
            used_assembly_code,
            pnd_max_interval_days_for(used_assembly_code),
            on_chunk_error=_log_chunk_error,
        )

        # Automatický fallback z 15min ("03") na hodinová data ("05"), pokud
        # pro dané odběrné místo 15min profil selhal na všech oknech.
        if (
            not raw_points
            and chunk_errors
            and not self._is_production
            and used_assembly_code == PND_ASSEMBLY_15MIN
        ):
            _LOGGER.warning(
                "Stažení 15min dat (assemblyCode=%s) pro %s selhalo (%s) - "
                "zkouším záložní hodinová data (assemblyCode=%s).",
                used_assembly_code,
                self._ean,
                chunk_errors[-1],
                PND_ASSEMBLY_HOURLY,
            )
            used_assembly_code = PND_ASSEMBLY_HOURLY
            chunk_errors.clear()
            raw_points, unit = await fetch_pnd_chunked(
                self._client,
                self._partner,
                self._ean,
                fetch_start,
                now,
                used_assembly_code,
                pnd_max_interval_days_for(used_assembly_code),
                on_chunk_error=_log_chunk_error,
            )

        if not raw_points:
            if chunk_errors:
                # Všechna okna selhala (typicky opakovaný 403 z MEPAS/AWS
                # Gateway) - statistika "{DOMAIN}:<ean>_consumption/_production"
                # se tedy tenhle cyklus nevytvoří/neaktualizuje vůbec (viz
                # issue #24 - "Nemam eventu"). Bez tohoto varování to bylo
                # vidět jen v DEBUG logu, takže si toho uživatel běžně nevšiml.
                _LOGGER.warning(
                    "Import hodinových dat (ean=%s, partner=%s, "
                    "assemblyCode=%s, %s) selhal pro všech %d stažených "
                    "oken - statistika '%s' se v tomto cyklu "
                    "nevytvoří/neaktualizuje. Poslední chyba: %s "
                    "(viz i pnd/status výše v logu - issue #30).",
                    self._ean,
                    self._partner,
                    used_assembly_code,
                    "výroba" if self._is_production else "spotřeba",
                    len(chunk_errors),
                    statistic_id,
                    chunk_errors[-1],
                )
            return

        # Bezpečnostní pojistka (issue #24): krok dat odvozujeme přímo
        # z časových značek odpovědi, ne jen z assemblyCode.
        expected_interval = PND_INTERVAL_MINUTES_BY_ASSEMBLY.get(
            used_assembly_code, self._assembly_interval_minutes
        )
        interval_minutes = infer_interval_minutes(raw_points, fallback=expected_interval)
        if interval_minutes != expected_interval:
            _LOGGER.warning(
                "pnd/data pro %s (assemblyCode=%s): krok dat podle časových "
                "značek je %d min, ne předpokládaných %d min - používám "
                "odvozenou hodnotu (issue #24).",
                self._ean,
                used_assembly_code,
                interval_minutes,
                expected_interval,
            )

        try:
            _LOGGER.debug(
                "pnd/data souhrn pro %s (assemblyCode=%s, okno %s .. %s): %s",
                self._ean,
                used_assembly_code,
                fetch_start.isoformat(),
                now.isoformat(),
                json.dumps(summarize_pnd_points(raw_points), ensure_ascii=False),
            )
        except Exception as err:  # noqa: BLE001 - diagnostika nesmí shodit import
            _LOGGER.debug("Souhrn pnd/data se nepodařil (%s).", err)

        complete_points, hourly_buckets, trimmed = process_pnd_points(
            raw_points, unit, interval_minutes, fetch_start, now
        )
        if trimmed:
            _LOGGER.debug(
                "Ořezán souvislý 'ocas' %d nulových (zatím neúplných) "
                "záznamů z konce pro %s.",
                trimmed,
                self._ean,
            )
        if not hourly_buckets:
            return

        newest_hour_start = max(hourly_buckets)
        oldest_hour_start = min(hourly_buckets)

        stat_name = (
            f"ČEZ dodávka do sítě {self._ean}"
            if self._is_production
            else f"ČEZ spotřeba {self._ean}"
        )
        # 'last_pnd_timestamp' (diagnostická entita) se aktualizuje AŽ PO
        # úspěšném zápisu, ne dřív - kdyby async_add_external_statistics
        # selhalo (výjimka), diagnostická entita nemá lhát, že máme data,
        # která ve skutečnosti neproběhla. Skutečný zdroj pravdy pro
        # fetch_start příštího běhu je vždy get_last_statistics().
        final_total = await self._async_write_external_statistic(
            recorder,
            statistic_id,
            stat_name,
            hourly_buckets,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            unit_class="energy",
        )
        self.last_pnd_timestamp = newest_hour_start + timedelta(hours=1)
        if final_total is not None:
            self.pnd_total_kwh = final_total
        if self.pnd_first_timestamp is None or oldest_hour_start < self.pnd_first_timestamp:
            self.pnd_first_timestamp = oldest_hour_start

        if self._is_production:
            return

        if not can_split_tariffs:
            _LOGGER.debug(
                "Přeskakuji zápis statistik VT/NT a nákladů pro %s - zatím "
                "chybí plán HDO z portálu.",
                self._ean,
            )
            return

        split = split_interval_consumption(
            complete_points,
            interval_minutes,
            lookup,
            dt_util.as_local,
            fallback_all_vt=fallback_all_vt,
        )
        if split is None:
            return

        hourly_vt = dict(split.hourly_vt)
        hourly_nt = dict(split.hourly_nt)

        # Pokud při prvním vytvoření VT/NT statistik existují v celkové
        # spotřebě ještě starší hodiny než nejstarší stažený 15min bod,
        # doplníme je hodinovým rozpadem podle HDO, aby se časová osa
        # všech statistik přesně kryla.
        if (
            (not has_vt_stats or not has_nt_stats)
            and self.pnd_first_timestamp is not None
            and self.pnd_first_timestamp < oldest_hour_start
        ):
            older_total_rows = await self._async_read_stat_rows(
                recorder,
                statistic_id,
                self.pnd_first_timestamp,
                oldest_hour_start - timedelta(seconds=1),
            )
            older_points = [
                (ts, max(state, 0.0))
                for ts, state, _ in older_total_rows
                if state is not None
            ]
            if older_points:
                older_split = split_interval_consumption(
                    older_points,
                    60,
                    lookup,
                    dt_util.as_local,
                    fallback_all_vt=fallback_all_vt,
                )
                if older_split is not None:
                    for ts, val in older_split.hourly_vt.items():
                        hourly_vt.setdefault(ts, val)
                    for ts, val in older_split.hourly_nt.items():
                        hourly_nt.setdefault(ts, val)

        await self._async_write_external_statistic(
            recorder,
            vt_stat_id,
            f"ČEZ spotřeba VT {self._ean}",
            hourly_vt,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            unit_class="energy",
        )
        await self._async_write_external_statistic(
            recorder,
            nt_stat_id,
            f"ČEZ spotřeba NT {self._ean}",
            hourly_nt,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            unit_class="energy",
        )

        # Nákladové statistiky VT / NT / celkem podle cen z konfigurace.
        cost_source_vt = dict(hourly_vt)
        cost_source_nt = dict(hourly_nt)
        min_tariff_hour = min(cost_source_vt) if cost_source_vt else oldest_hour_start
        if (
            not has_all_cost_stats
            and has_vt_stats
            and has_nt_stats
            and self.pnd_first_timestamp is not None
            and self.pnd_first_timestamp < min_tariff_hour
        ):
            prior_vt = await self._async_read_stat_rows(
                recorder,
                vt_stat_id,
                self.pnd_first_timestamp,
                min_tariff_hour - timedelta(seconds=1),
            )
            prior_nt = await self._async_read_stat_rows(
                recorder,
                nt_stat_id,
                self.pnd_first_timestamp,
                min_tariff_hour - timedelta(seconds=1),
            )
            for ts, state, _ in prior_vt:
                if state is not None:
                    cost_source_vt.setdefault(ts, max(state, 0.0))
            for ts, state, _ in prior_nt:
                if state is not None:
                    cost_source_nt.setdefault(ts, max(state, 0.0))

        costs_vt, costs_nt, costs_total = calculate_hourly_costs(
            cost_source_vt, cost_source_nt, self._price_vt, self._price_nt
        )
        currency = self._currency()
        await self._async_write_external_statistic(
            recorder,
            cost_vt_stat_id,
            f"ČEZ náklady VT {self._ean}",
            costs_vt,
            unit_of_measurement=currency,
            unit_class=None,
        )
        await self._async_write_external_statistic(
            recorder,
            cost_nt_stat_id,
            f"ČEZ náklady NT {self._ean}",
            costs_nt,
            unit_of_measurement=currency,
            unit_class=None,
        )
        await self._async_write_external_statistic(
            recorder,
            cost_total_stat_id,
            f"ČEZ celkové náklady {self._ean}",
            costs_total,
            unit_of_measurement=currency,
            unit_class=None,
        )

    async def _async_find_old_statistic_gap(
        self,
        recorder: Any,
        statistic_id: str,
        now: datetime,
        fetch_start: datetime,
    ) -> datetime | None:
        """Najde nejstarší díru ve statistice před `fetch_start` (issue #40).

        Díra = chybějící hodina nebo (jen u spotřeby) delší běh hodin
        s přesně 0 kWh. Projde se nejvýš PND_GAP_SCAN_DAYS dní zpátky a
        nejvýš jednou za PND_GAP_REPAIR_INTERVAL_HOURS - pokud ČEZ data
        pro díru opravdu nemá (výpadek, elektroměr bez dat), nemá se
        stahovat znovu každou hodinu.
        """
        if self._last_gap_scan is not None and now - self._last_gap_scan < timedelta(
            hours=PND_GAP_REPAIR_INTERVAL_HOURS
        ):
            return None
        self._last_gap_scan = now

        scan_start = now - timedelta(days=PND_GAP_SCAN_DAYS)
        if scan_start >= fetch_start:
            return None
        rows_by_id = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            scan_start,
            fetch_start,
            {statistic_id},
            "hour",
            None,
            {"state"},
        )
        hours: list[tuple[datetime, float | None]] = []
        for row in (rows_by_id or {}).get(statistic_id) or []:
            start = _stat_row_start(row)
            if start is None:
                continue
            state = row.get("state")
            hours.append((start, float(state) if state is not None else None))
        if not hours:
            return None
        hours.sort(key=lambda item: item[0])

        gap_start, missing, zero_hours = find_statistic_gaps(
            hours, None if self._is_production else PND_GAP_ZERO_RUN_HOURS
        )
        # Chybějící hodiny na konci kontrolovaného úseku (mezi posledním
        # řádkem a hodinou, ve které leží fetch_start - ta se při běžném
        # importu už nepřepisuje).
        last_expected = fetch_start.replace(minute=0, second=0, microsecond=0)
        tail_start = hours[-1][0] + timedelta(hours=1)
        if tail_start <= last_expected:
            missing += int((last_expected - tail_start).total_seconds() // 3600) + 1
            if gap_start is None or tail_start < gap_start:
                gap_start = tail_start

        if gap_start is None:
            return None
        _LOGGER.info(
            "Statistika %s má díru starší než %d dní (od %s: %d chybějících "
            "hodin, %d hodin s nulovou spotřebou) - stahuji hodinová data "
            "znovu od tohoto okamžiku (issue #40).",
            statistic_id,
            PND_TRAILING_SAFETY_DAYS,
            dt_util.as_local(gap_start).isoformat(),
            missing,
            zero_hours,
        )
        return gap_start

    async def _async_update_meter_estimate(self, readings: Any) -> None:
        """Dopočítá odhad aktuálního stavu elektroměru (issue #40).

        Historie odečtů z portálu se aktualizuje jen při odečtu (u
        většiny odběrných míst jednou měsíčně), takže "Stav elektroměru
        VT/NT" může být i několik týdnů starý. Statistiky
        cez:<ean>_consumption/_vt/_nt ale mají spotřebu až skoro do
        současnosti - součet hodinových hodnot od okamžiku odečtu do konce
        importovaných dat je spotřeba od odečtu. Rozdělení na VT/NT bere
        přednostně 15min-přesné statistiky VT/NT, se zálohou na
        _split_meter_estimate (issue #49)."""
        if not isinstance(readings, list) or not readings:
            self.meter_estimate = None
            return
        local_tz = (
            dt_util.get_default_time_zone()
            if hasattr(dt_util, "get_default_time_zone")
            else dt_util.DEFAULT_TIME_ZONE
        )
        baseline = reading_baseline(readings[0], local_tz)
        if baseline is None:
            _LOGGER.debug("Odhad stavu elektroměru: poslední odečet nemá datum/hodnoty.")
            self.meter_estimate = None
            return
        base_time, base_kwh = baseline

        if self.last_pnd_timestamp is None:
            self.meter_estimate = None
            return

        hours: list[tuple[datetime, float]] = []
        vt_hours: list[tuple[datetime, float]] = []
        nt_hours: list[tuple[datetime, float]] = []
        missing_hours = 0
        zero_hours = 0
        if base_time >= self.last_pnd_timestamp:
            # Odečet je novější než poslední hodinová data (ČEZ je
            # zveřejňuje zpětně, obvykle kolem 1:00 za celý předchozí den) -
            # nejlepší známý stav je prostě odečet sám.
            increment = 0.0
        else:
            # Přírůstek = součet hodinových hodnot ('state') od okamžiku
            # odečtu, ne rozdíl kumulativního 'sum' (issue #40) - díra ve
            # statistice se tak dá poznat a ukázat v atributech, místo aby
            # tiše snížila odhad.
            statistic_id = self._statistic_id()
            vt_stat_id = self.statistic_id_for(STATISTIC_CONSUMPTION_VT)
            nt_stat_id = self.statistic_id_for(STATISTIC_CONSUMPTION_NT)
            recorder = get_instance(self.hass)
            rows_by_id = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                base_time,
                self.last_pnd_timestamp,
                {statistic_id, vt_stat_id, nt_stat_id},
                "hour",
                None,
                {"state"},
            )
            for row in (rows_by_id or {}).get(statistic_id) or []:
                start = _stat_row_start(row)
                state = row.get("state")
                if start is None or state is None:
                    continue
                hours.append((start, max(float(state), 0.0)))
            hours.sort(key=lambda item: item[0])

            for row in (rows_by_id or {}).get(vt_stat_id) or []:
                start = _stat_row_start(row)
                state = row.get("state")
                if start is not None and state is not None:
                    vt_hours.append((start, max(float(state), 0.0)))
            vt_hours.sort(key=lambda item: item[0])

            for row in (rows_by_id or {}).get(nt_stat_id) or []:
                start = _stat_row_start(row)
                state = row.get("state")
                if start is not None and state is not None:
                    nt_hours.append((start, max(float(state), 0.0)))
            nt_hours.sort(key=lambda item: item[0])

            if not hours or hours[0][0] > base_time:
                # Statistika okamžik odečtu nepokrývá (začíná až po něm,
                # např. elektroměr/backfill novější než odečet) - přírůstek
                # by byl podhodnocený bez možnosti to poznat.
                _LOGGER.debug(
                    "Odhad stavu elektroměru: statistika %s nezačíná v okamžiku "
                    "odečtu (%s) - nelze dopočítat.",
                    statistic_id,
                    base_time.isoformat(),
                )
                self.meter_estimate = None
                return
            increment = sum(kwh for _, kwh in hours)
            expected = int((self.last_pnd_timestamp - base_time).total_seconds() // 3600)
            _, gap_missing, zero_hours = find_statistic_gaps(
                hours, None if self._is_production else PND_GAP_ZERO_RUN_HOURS
            )
            missing_hours = max(expected - len(hours), gap_missing)
            if missing_hours or zero_hours:
                _LOGGER.debug(
                    "Odhad stavu elektroměru: ve statistice od odečtu chybí %d h "
                    "a %d h má podezřele nulovou spotřebu - odhad může být "
                    "podhodnocený (issue #40).",
                    missing_hours,
                    zero_hours,
                )

        self.meter_estimate = {
            "value": base_kwh + increment,
            "base_kwh": base_kwh,
            "base_time": base_time,
            "increment_kwh": increment,
            "data_until": self.last_pnd_timestamp,
            "missing_hours": missing_hours,
            "zero_hours": zero_hours,
            "tariffs": None,
        }

        try:
            self.meter_estimate["tariffs"] = self._split_meter_estimate(
                readings[0], hours, increment, vt_hours=vt_hours, nt_hours=nt_hours
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Rozdělení odhadu stavu elektroměru na VT/NT selhalo "
                "(součtový odhad zůstává, issue #49)."
            )
        _LOGGER.debug(
            "Odhad stavu elektroměru: odečet %.3f kWh k %s + %.3f kWh "
            "z hodinových dat do %s = %.3f kWh.",
            base_kwh,
            base_time.isoformat(),
            increment,
            self.last_pnd_timestamp.isoformat(),
            base_kwh + increment,
        )

    async def _async_update_hdo_history(self, signals_data: Any) -> None:
        """Zapamatuje si plány HDO z portálu po dnech (issue #49)."""
        if self._hdo_history is None:
            stored = await self._hdo_store.async_load()
            history = stored.get("schedules") if isinstance(stored, dict) else None
            self._hdo_history = (
                {str(k): str(v) for k, v in history.items()}
                if isinstance(history, dict)
                else {}
            )
            if isinstance(stored, dict) and stored.get("signal", "") != self._hdo_signal:
                # Uživatel přenastavil HDO signál - staré plány patří jinému
                # signálu a výsledek by zkreslily.
                _LOGGER.debug(
                    "Plán HDO v úložišti patří jinému signálu, začínám znovu."
                )
                self._hdo_history = {}

        fresh = extract_signal_schedules(signals_data, self._hdo_signal)
        today = dt_util.as_local(dt_util.utcnow()).date()
        if merge_schedule_history(self._hdo_history, fresh, today):
            self._hdo_store.async_delay_save(
                lambda: {"signal": self._hdo_signal, "schedules": dict(self._hdo_history or {})},
                60,
            )
            _LOGGER.debug(
                "Plán HDO uložen: %d dní (%s .. %s).",
                len(self._hdo_history),
                min(self._hdo_history) if self._hdo_history else "-",
                max(self._hdo_history) if self._hdo_history else "-",
            )

    def _split_meter_estimate(
        self,
        reading: dict[str, Any],
        hours: list[tuple[datetime, float]],
        increment: float,
        *,
        vt_hours: list[tuple[datetime, float]] | None = None,
        nt_hours: list[tuple[datetime, float]] | None = None,
    ) -> dict[str, Any] | None:
        """Rozdělí odhad stavu elektroměru na VT a NT podle plánu HDO (issue #49).

        Pokud už v Recorderu existují 15min-přesné statistiky `consumption_vt`
        a `consumption_nt` pokrývající stejné hodiny od odečtu, použijí se přímo
        (aby odhad souhlasil s Energy panelem). Jinak se provede záložní
        hodinový rozpad podle plánu HDO.
        """
        base_vt, base_nt = reading_registers(reading)
        if base_vt is None or base_nt is None:
            # Jednotarifní sazba nebo odečet bez obou registrů.
            return None
        lookup = ScheduleLookup(self._hdo_history or {})
        if not lookup:
            _LOGGER.debug("Rozdělení na VT/NT: zatím žádný plán HDO.")
            return None

        result: dict[str, Any] = {
            "base_vt": base_vt,
            "base_nt": base_nt,
            "increment_vt": 0.0,
            "increment_nt": 0.0,
            "days_known": 0,
            "days_weekday": 0,
            "days_nearest": 0,
            "last_day": None,
        }
        if increment <= 0 or not hours:
            return result

        # Přednostně použít již uložené statistiky VT/NT (počítané z 15min dat).
        if vt_hours and nt_hours and len(vt_hours) == len(hours) and len(nt_hours) == len(hours):
            vt_map = dict(vt_hours)
            nt_map = dict(nt_hours)
            if all(ts in vt_map and ts in nt_map for ts, _ in hours):
                daily: dict[date, list[float]] = {}
                inc_vt = 0.0
                inc_nt = 0.0
                for ts, _ in hours:
                    vt = vt_map[ts]
                    nt = nt_map[ts]
                    inc_vt += vt
                    inc_nt += nt
                    day = daily.setdefault(dt_util.as_local(ts).date(), [0.0, 0.0])
                    day[0] += vt
                    day[1] += nt
                days_known, days_weekday, days_nearest = count_schedule_sources(
                    (ts for ts, _ in hours), lookup, dt_util.as_local
                )
                result["increment_vt"] = inc_vt
                result["increment_nt"] = inc_nt
                result["days_known"] = days_known
                result["days_weekday"] = days_weekday
                result["days_nearest"] = days_nearest
                if daily:
                    last_day = max(daily)
                    result["last_day"] = {
                        "date": last_day,
                        "vt": daily[last_day][0],
                        "nt": daily[last_day][1],
                    }
                return result

        split = split_hourly_consumption(hours, lookup, dt_util.as_local)
        if split is None or split.hours == 0:
            return None

        result["increment_vt"] = split.vt_kwh
        result["increment_nt"] = split.nt_kwh
        result["days_known"] = split.days_count(SOURCE_KNOWN)
        result["days_weekday"] = split.days_count(SOURCE_WEEKDAY)
        result["days_nearest"] = split.days_count(SOURCE_NEAREST)
        if split.daily:
            last_day = max(split.daily)
            vt, nt = split.daily[last_day]
            result["last_day"] = {"date": last_day, "vt": vt, "nt": nt}

        _LOGGER.debug(
            "Rozdělení na VT/NT: %d h, VT +%.3f kWh, NT +%.3f kWh (dny s plánem "
            "z portálu %d, podle dne v týdnu %d, nejbližší plán %d).",
            split.hours,
            result["increment_vt"],
            result["increment_nt"],
            result["days_known"],
            result["days_weekday"],
            result["days_nearest"],
        )
        return result

    async def _async_probe_assembly_code(self, code: str) -> str:
        """Jednorázově stáhne 3denní vzorek pnd/data pro assemblyCode, jehož
        význam zatím neznáme, a zaloguje strukturu i syrovou odpověď
        (issue #32, #40).

        Cíl: zjistit, která sestava nese denní stavy registrů VT/NT (web
        Portálu naměřených dat je ukazuje i u měření typu C, ale sestavu
        17 pro ně pnd/status nenabízí - #32). Hodnoty se nikam
        nepoužívají. Vrací krátké shrnutí pro diagnostickou entitu."""
        now = datetime.now(timezone.utc)
        try:
            data = await self._client.get_pnd_data(
                partner=self._partner,
                ean=self._ean,
                date_from=(now - timedelta(days=3)).strftime("%Y-%m-%dT00:00:00.000Z"),
                date_to=now.strftime("%Y-%m-%dT00:00:00.000Z"),
                assembly_code=code,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.info("pnd/data sonda assemblyCode=%s selhala: %s (issue #32).", code, err)
            return f"chyba: {str(err)[:80]}"

        series = data if isinstance(data, list) else [data]
        summary = []
        for item in series:
            if not isinstance(item, dict):
                summary.append(type(item).__name__)
                continue
            values = item.get("values")
            sample = values[:2] if isinstance(values, list) else None
            summary.append(
                {
                    "keys": sorted(item.keys()),
                    "name": item.get("name"),
                    "unit": item.get("unit"),
                    "values": len(values) if isinstance(values, list) else None,
                    "sample": sample,
                }
            )
        _LOGGER.info(
            "pnd/data sonda assemblyCode=%s (issue #32): %d řad, struktura=%s; "
            "syrová odpověď (zkráceno na 3000 znaků): %s",
            code,
            len(series),
            json.dumps(summary, ensure_ascii=False, default=str),
            json.dumps(data, ensure_ascii=False, default=str)[:3000],
        )
        parts = [
            f"{s.get('values')}x {s.get('unit') or '?'}" if isinstance(s, dict) else str(s)
            for s in summary
        ]
        return f"{len(series)} řad: " + ", ".join(parts)

    async def _async_find_first_statistic_start(
        self, recorder: Any, statistic_id: str, now: datetime
    ) -> datetime | None:
        """Začátek nejstarší hodiny uložené ve statistice (nebo None).

        Recorder nemá "první řádek" dotaz, a hodinových řádků za až
        MAX_BACKFILL_YEARS let jsou desítky tisíc - proto dvoukrokově:
        nejdřív měsíční agregace (pár desítek řádků), pak hodinové řádky
        jen v nalezeném prvním měsíci. Volá se jednou po startu, výsledek
        se drží v 'pnd_first_timestamp'. Slouží jen jako informace pro
        atribut entity (od kdy platí kumulativní součet dodávky, issue
        #30) - selhání se zaloguje a nic dalšího neovlivní."""
        search_start = now - timedelta(days=365 * MAX_BACKFILL_YEARS + 31)
        try:
            monthly = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                search_start,
                None,
                {statistic_id},
                "month",
                None,
                {"sum"},
            )
            month_rows = monthly.get(statistic_id) if monthly else None
            if not month_rows:
                return None
            month_start = _stat_row_start(month_rows[0])
            if month_start is None:
                return None
            hourly = await recorder.async_add_executor_job(
                statistics_during_period,
                self.hass,
                month_start,
                month_start + timedelta(days=32),
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            hour_rows = hourly.get(statistic_id) if hourly else None
            if not hour_rows:
                return month_start
            return _stat_row_start(hour_rows[0]) or month_start
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Nepodařilo se zjistit začátek statistiky %s (%s) - atribut "
                "'od' u kumulativního součtu zůstane prázdný.",
                statistic_id,
                err,
            )
            return None

    async def _async_log_pnd_status(self) -> None:
        """Jednou za běh HA zaloguje syrovou odpověď pnd/status/{partner}
        (issue #30).

        Cíl: zjistit, jaké assemblyCode kódy ČEZ pro tohoto partnera/EAN
        vůbec nabízí, přímo v HA logu - bez nutnosti ručně spouštět
        scripts/test_pnd_consumption.py --status. Nevoláme to každou
        hodinu (partner/EAN se v rámci běhu HA nemění), jen jednou po
        startu, a NEBLOKUJE to skutečný import dat - volá se před ním,
        ale jakákoliv chyba se jen zaloguje a pokračuje se dál.

        Neznáme přesnou strukturu odpovědi (žádný zachycený vzorek), proto
        se nepokoušíme nic automaticky rozhodovat - jen co nejvíc
        informací do logu a na diagnostickou entitu, ať to jde vyhodnotit
        z reportů uživatelů (viz issue #30 diskuze)."""
        if self._pnd_status_checked:
            return
        self._pnd_status_checked = True

        try:
            status = await self._client.get_pnd_status(self._partner)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "pnd/status/%s selhalo (%s) - nelze zjistit, jaké assemblyCode "
                "kódy ČEZ pro tohoto partnera nabízí (issue #30, EAN %s).",
                self._partner,
                err,
                self._ean,
            )
            self.pnd_status_info = {"error": str(err)}
            return

        codes_found = _extract_assembly_codes(status)
        code_offered = self._assembly_code in codes_found if codes_found else None
        self.pnd_status_info = {
            "partner": self._partner,
            "assembly_code_used": self._assembly_code,
            "codes_found": codes_found,
            "assembly_code_offered": code_offered,
        }

        _LOGGER.info(
            "pnd/status/%s (EAN=%s, %s): používaný "
            "assemblyCode=%s, nalezené kódy v odpovědi=%s, používaný kód "
            "nabízen=%s.",
            self._partner,
            self._ean,
            "výroba" if self._is_production else "spotřeba",
            self._assembly_code,
            codes_found if codes_found is not None else "nerozpoznáno ze schématu odpovědi",
            code_offered,
        )
        # Syrová odpověď zvlášť (i kdyby výše extrakce podle klíče
        # "assembly*" nic nenašla, protože neznáme přesné schéma) - ať má
        # člověk čtoucí log kompletní podklad, ne jen náš (možná chybný)
        # výklad (issue #30).
        _LOGGER.info(
            "pnd/status/%s syrová odpověď (zkráceno na 4000 znaků): %s",
            self._partner,
            json.dumps(status, ensure_ascii=False, default=str)[:4000],
        )

        daily_offered = bool(codes_found) and PND_ASSEMBLY_DAILY_REGISTERS in codes_found
        self.pnd_status_info["daily_registers_offered"] = daily_offered
        self.pnd_status_info["measurement_types"] = _extract_measurement_types(status)

        # Sondy sestav s neznámým významem (issue #32) - jedno volání na
        # kód, jen jednou za běh HA. Známé kódy (používaný hodinový a
        # 15min výkon) se přeskakují.
        probe_codes = [
            code
            for code in (codes_found or [])
            if code not in (self._assembly_code, *PND_KNOWN_ASSEMBLY_CODES)
        ]
        if probe_codes:
            probes: dict[str, str] = {}
            for code in probe_codes:
                probes[code] = await self._async_probe_assembly_code(code)
            self.pnd_status_info["probes"] = probes

        if codes_found and not code_offered:
            _LOGGER.warning(
                "ČEZ pro partnera %s (EAN %s) v pnd/status NEnabízí "
                "assemblyCode %s, který integrace používá pro %s - dálkový "
                "odečet pro tuhle granularitu/registr zřejmě není na tomto "
                "účtu aktivovaný (issue #30). Nabízené kódy: %s.",
                self._partner,
                self._ean,
                self._assembly_code,
                "výrobu" if self._is_production else "spotřebu",
                codes_found,
            )


def _extract_assembly_codes(raw: Any) -> list[str] | None:
    """Nejlepší odhad seznamu assemblyCode kódů z odpovědi pnd/status.

    Neznáme přesné schéma odpovědi (žádný zachycený vzorek) - hledáme
    proto obecně jakýkoliv klíč obsahující "assembly" (case-insensitive)
    kdekoliv ve vnořené struktuře. Vrací None, pokud se nic takového
    nenajde (to NEMUSÍ znamenat, že kódy nejsou nabízené - jen že odpověď
    má jiné schéma, než jsme čekali; proto se vždy loguje i syrová
    odpověď, viz _async_log_pnd_status)."""
    found: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if "assembly" in key.lower() and isinstance(value, (str, int)):
                    found.add(str(value))
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(raw)
    return sorted(found) if found else None


def _extract_measurement_types(raw: Any) -> list[str] | None:
    """Typ a kategorie měření z pnd/status (např. "C/C1") - issue #32:
    sestava 17 (denní registry) se zdá dostupná jen u některých typů."""
    found: set[str] = set()
    items = raw.get("supplyPointList") if isinstance(raw, dict) else None
    for item in items or []:
        if isinstance(item, dict) and (item.get("typMereni") or item.get("kategorieMereni")):
            found.add(f"{item.get('typMereni') or '?'}/{item.get('kategorieMereni') or '-'}")
    return sorted(found) or None


def _stat_row_start(row: dict[str, Any]) -> datetime | None:
    """'start' řádku statistiky jako tz-aware UTC datetime.

    statistics_during_period vrací 'start' v novějších verzích HA jako
    epoch sekundy (float), ve starších jako datetime - bereme obojí."""
    start = row.get("start")
    if isinstance(start, datetime):
        return start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    if isinstance(start, (int, float)):
        return datetime.fromtimestamp(start, tz=timezone.utc)
    return None
