"""Binary senzory pro ČEZ – poruchy/odstávky a stav tarifů HDO (NT/VT)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_EAN,
    CONF_HDO_SIGNAL,
    CONF_OM_TYPE,
    DATA_OUTAGES,
    DATA_SIGNALS,
    DEVICE_GROUP_HDO,
    DOMAIN,
    HDO_STATE_NT,
    HDO_STATE_UNKNOWN,
    HDO_STATE_VT,
    OM_TYPE_CONSUMPTION,
    OM_TYPES_PRODUCTION,
)
from .coordinator import CezDistribuceCoordinator
from .sensor import (
    _current_hdo_state_from_windows,
    _current_tariff_window_absolute,
    _get_nt_windows_around_now,
    _get_todays_intervals,
    _local_iso,
    _local_now,
    _next_tariff_window_absolute,
    build_device_info,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Nastaví binární senzory z config entry."""
    coordinator: CezDistribuceCoordinator = hass.data[DOMAIN][entry.entry_id]
    ean = entry.data[CONF_EAN]
    hdo_signal = entry.options.get(
        CONF_HDO_SIGNAL, entry.data.get(CONF_HDO_SIGNAL, "")
    )
    om_type = entry.data.get(CONF_OM_TYPE) or OM_TYPE_CONSUMPTION
    is_production = om_type in OM_TYPES_PRODUCTION

    entities: list[BinarySensorEntity] = []
    if coordinator.enable_outages:
        entities.append(CezOutageSensor(coordinator, entry, ean))

    if not is_production and coordinator.enable_hdo:
        signals_data = (coordinator.data or {}).get(DATA_SIGNALS)
        is_single_tariff = (
            not hdo_signal
            and isinstance(signals_data, dict)
            and not (signals_data.get("signals") or [])
        )
        if not is_single_tariff:
            entities.extend(
                [
                    CezTariffBinarySensor(
                        coordinator, entry, ean, hdo_signal, HDO_STATE_NT
                    ),
                    CezTariffBinarySensor(
                        coordinator, entry, ean, hdo_signal, HDO_STATE_VT
                    ),
                ]
            )

    _remove_disabled_binary_sensor_entities(
        hass, ean, {entity.unique_id for entity in entities if entity.unique_id}
    )
    async_add_entities(entities)


def _remove_disabled_binary_sensor_entities(
    hass: HomeAssistant, ean: str, active_unique_ids: set[str]
) -> None:
    """Odstraní z registru binárních senzorů entity, jejichž funkce uživatel vypnul."""
    registry = er.async_get(hass)
    for uid in (f"{ean}_outage", f"{ean}_low_tariff", f"{ean}_high_tariff"):
        if uid in active_unique_ids:
            continue
        entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, uid)
        if entity_id:
            _LOGGER.info("Odstraňuji vypnutou binární entitu %s z registru entit.", entity_id)
            registry.async_remove(entity_id)


class CezOutageSensor(CoordinatorEntity[CezDistribuceCoordinator], BinarySensorEntity):
    """Indikátor plánované nebo aktuální odstávky/poruchy v síti."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:power-plug-off"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
    ) -> None:
        super().__init__(coordinator)
        self._ean = ean
        self._attr_unique_id = f"{ean}_outage"
        self._attr_name = "Porucha / odstávka"
        self._attr_device_info = build_device_info(entry, ean)

    @property
    def is_on(self) -> bool:
        """True pokud je hlášena porucha nebo odstávka."""
        outages = self.coordinator.data.get(DATA_OUTAGES) if self.coordinator.data else None
        if not outages:
            return False

        # Odpověď může být list výpadků nebo dict s klíčem se seznamem
        outage_list = outages if isinstance(outages, list) else outages.get("outages") or outages.get("shutdowns") or []
        return len(outage_list) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        outages = self.coordinator.data.get(DATA_OUTAGES) if self.coordinator.data else {}
        outage_list = outages if isinstance(outages, list) else outages.get("outages") or outages.get("shutdowns") or []
        return {
            "poruchy_odstavky": outage_list,
            "pocet": len(outage_list),
        }


class CezTariffBinarySensor(
    CoordinatorEntity[CezDistribuceCoordinator], BinarySensorEntity
):
    """Binární indikátor aktivního tarifu HDO (Nízký tarif / Vysoký tarif)."""

    _attr_has_entity_name = True
    _unsub_time_listener: CALLBACK_TYPE | None = None

    def __init__(
        self,
        coordinator: CezDistribuceCoordinator,
        entry: ConfigEntry,
        ean: str,
        hdo_signal: str,
        tariff: str,
    ) -> None:
        super().__init__(coordinator)
        self._ean = ean
        self._hdo_signal = hdo_signal
        self._tariff = tariff

        if tariff == HDO_STATE_NT:
            self._attr_unique_id = f"{ean}_low_tariff"
            self._attr_name = "Nízký tarif"
            self._attr_icon = "mdi:flash-outline"
        else:
            self._attr_unique_id = f"{ean}_high_tariff"
            self._attr_name = "Vysoký tarif"
            self._attr_icon = "mdi:flash"

        self._attr_device_info = build_device_info(entry, ean, DEVICE_GROUP_HDO)

    async def async_added_to_hass(self) -> None:
        """Po přidání do HA spustí minutový přepočet stavu."""
        await super().async_added_to_hass()
        self._unsub_time_listener = async_track_time_interval(
            self.hass, self._handle_time_change, timedelta(minutes=1)
        )

    async def async_will_remove_from_hass(self) -> None:
        """Uklidí minutový listener při odebrání entity."""
        if self._unsub_time_listener is not None:
            self._unsub_time_listener()
            self._unsub_time_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_time_change(self, _: datetime) -> None:
        """Zapíše aktuální stav při minutovém tiknutí."""
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        """True pokud je právě aktivní sledovaný tarif (NT nebo VT)."""
        windows = _get_nt_windows_around_now(self.coordinator.data, self._hdo_signal)
        if windows is None:
            return None
        return _current_hdo_state_from_windows(windows) == self._tariff

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        windows = _get_nt_windows_around_now(self.coordinator.data, self._hdo_signal)
        hdo_state = (
            _current_hdo_state_from_windows(windows)
            if windows is not None
            else HDO_STATE_UNKNOWN
        )
        now = _local_now()
        intervals = _get_todays_intervals(self.coordinator.data, self._hdo_signal) or []
        current_window = (
            _current_tariff_window_absolute(intervals, hdo_state, now)
            if hdo_state in (HDO_STATE_NT, HDO_STATE_VT)
            else None
        )
        next_window = _next_tariff_window_absolute(intervals, self._tariff, now)
        return {
            "hdo_signal": self._hdo_signal,
            "tarif": self._tariff,
            "stav_hdo": hdo_state,
            "od": _local_iso(current_window[0]) if current_window else None,
            "do": _local_iso(current_window[1]) if current_window else None,
            "dalsi_sepnuti": _local_iso(next_window[0]) if next_window else None,
        }
