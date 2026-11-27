"""ČEZ integrace pro Home Assistant."""
from __future__ import annotations

import logging
from typing import Any, Coroutine

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)

try:
    from homeassistant.util.ssl import get_default_context
except ImportError:  # unit testy bez plného HA nebo starší prostředí
    get_default_context = None  # type: ignore[assignment]

from .api import (
    CezApiError,
    CezDistribuceApiClient,
    CezInvalidCredentialsError,
    extract_supply_points,
)
from .const import (
    CONF_ANLAGE,
    CONF_EAN,
    CONF_HDO_SIGNAL,
    CONF_OM_TYPE,
    CONF_PARTNER,
    CONF_PASSWORD,
    CONF_PORTAL_ENVIRONMENT,
    CONF_PORTAL_PARTNER,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    CONF_USERNAME,
    DEFAULT_PRICE_NT,
    DEFAULT_PRICE_VT,
    DOMAIN,
    MAX_BACKFILL_YEARS,
    OM_TYPE_CONSUMPTION,
    SERVICE_RECALCULATE_STATISTICS,
)
from .coordinator import CezDistribuceCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

SERVICE_RECALCULATE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_EAN): str,
        vol.Optional("days", default=30): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=365 * MAX_BACKFILL_YEARS)
        ),
        vol.Optional("recalculate_costs_only", default=False): bool,
    }
)


def _async_create_background_task(
    hass: HomeAssistant, coro: Coroutine[Any, Any, Any], name: str
) -> Any:
    """Spustí úlohu na pozadí tak, aby neblokovala start Home Assistantu."""
    create_bg = getattr(hass, "async_create_background_task", None)
    if callable(create_bg):
        return create_bg(coro, name)
    return hass.async_create_task(coro, name=name)


def _async_register_services(hass: HomeAssistant) -> None:
    """Zaregistruje doménové služby ČEZ (pokud ještě nejsou registrované)."""
    if hass.services.has_service(DOMAIN, SERVICE_RECALCULATE_STATISTICS):
        return

    async def _handle_recalculate(call: ServiceCall) -> None:
        target_ean = (call.data.get(CONF_EAN) or "").strip()
        days = int(call.data.get("days", 30))
        recalculate_costs_only = bool(call.data.get("recalculate_costs_only", False))
        coordinators = [
            coord
            for coord in (hass.data.get(DOMAIN) or {}).values()
            if isinstance(coord, CezDistribuceCoordinator)
        ]
        matched = 0
        for coordinator in coordinators:
            if target_ean and coordinator.ean != target_ean:
                continue
            if not target_ean and coordinator.is_production:
                continue
            matched += 1
            await coordinator.async_recalculate_statistics(
                days=days, recalculate_costs_only=recalculate_costs_only
            )
        if target_ean and matched == 0:
            _LOGGER.warning(
                "Služba %s.%s: nebylo nalezeno žádné odběrné místo s EAN '%s'.",
                DOMAIN,
                SERVICE_RECALCULATE_STATISTICS,
                target_ean,
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RECALCULATE_STATISTICS,
        _handle_recalculate,
        schema=SERVICE_RECALCULATE_SCHEMA,
    )


async def _async_enrich_partner_and_anlage(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: CezDistribuceApiClient,
    coordinator: CezDistribuceCoordinator,
    uid: str,
) -> None:
    """Na pozadí doplní metadata pro volitelná hodinová data."""
    partner = entry.data.get(CONF_PARTNER, "")
    anlage = entry.data.get(CONF_ANLAGE, "")

    if partner and anlage:
        return
    # První refresh mohl UID obnovit (issue #43).
    uid = coordinator.uid or uid
    ean = entry.data.get(CONF_EAN, "")

    try:
        if not partner:
            # Podle UID, se zálohou podle EAN - uložené UID může být
            # zastaralé (issue #43).
            points = extract_supply_points(await client.get_supply_points())
            match = next((p for p in points if uid and p.get("uid") == uid), None)
            if match is None:
                match = next((p for p in points if p.get("ean") == ean), None)
            if match is not None:
                partner = match.get("partner", "")

        if not anlage:
            _LOGGER.debug("Dohledávám technické číslo odběrného místa na pozadí.")
            # Výroba/mikrozdroj odečty nestahuje, takže tohle je u ní
            # jediné místo, kde se zastaralé UID projeví - proto i tady
            # obnova UID (issue #43, review).
            detail = await coordinator.async_call_with_uid_recovery(
                client.get_supply_point_detail
            )
            if isinstance(detail, dict):
                anlage = (
                    (detail.get("anlage_Dist") or {}).get("cislo")
                    or detail.get("anlage", "")
                )

        updated_data = dict(entry.data)
        if partner:
            updated_data[CONF_PARTNER] = partner
        if anlage:
            updated_data[CONF_ANLAGE] = anlage
        if updated_data != entry.data:
            hass.config_entries.async_update_entry(entry, data=updated_data)
        if partner and anlage:
            coordinator.set_partner_and_anlage(partner, anlage)
            _LOGGER.info(
                "Doplnil jsem metadata pro hodinová data; aktivuji jejich načítání."
            )
            await coordinator.async_refresh()
        else:
            _LOGGER.debug(
                "Metadata pro hodinová data zatím nejsou kompletní "
                "(partner=%s, anlage=%s).",
                bool(partner),
                bool(anlage),
            )
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Nepodařilo se doplnit metadata pro hodinová data; "
            "ostatní senzory zůstávají funkční."
        )


async def _async_ensure_portal_environment(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: CezDistribuceApiClient,
    uid: str,
    ean: str,
) -> None:
    """Zajistí, že je vybrané prostředí portálu s odběrným místem entry (issue #37).

    Týká se jen účtů, které po přihlášení vybírají prostředí. Entries
    založené před touto verzí prostředí uložené nemají a výchozí volba
    (Domácnost) nemusí obsahovat jejich EAN; stejně tak když uložené
    prostředí z portálu zmizelo.
    """
    if not client.environment_options:
        return
    if entry.data.get(CONF_PORTAL_ENVIRONMENT) and not client.preferred_environment_missing:
        return

    _LOGGER.info("Dohledávám prostředí portálu ČEZ s odběrným místem této integrace.")
    try:
        environment = await client.find_environment_for_supply_point(uid, ean)
    except (CezApiError, aiohttp.ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            f"Nepodařilo se dohledat prostředí portálu ČEZ: {err}"
        ) from err
    if environment is None:
        raise ConfigEntryError(
            "Odběrné místo této integrace není vidět v žádném prostředí portálu "
            "ČEZ Distribuce. Překonfigurujte integraci, nebo ji přidejte znovu."
        )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_PORTAL_ENVIRONMENT: environment[0],
            CONF_PORTAL_PARTNER: environment[1],
        },
    )
    _LOGGER.info("Prostředí portálu ČEZ uloženo do konfigurace (%s).", environment[0])


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Nastaví integraci z config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    ean = entry.data[CONF_EAN]
    uid = entry.data.get("uid", "")
    if not (entry.title or "").strip():
        hass.config_entries.async_update_entry(
            entry, title=f"ČEZ {ean}".strip() or "ČEZ"
        )
        _LOGGER.warning(
            "Config entry neměl název; nastavil jsem bezpečný náhradní název."
        )
    # Chybí u config entries založených před přidáním rozlišení OM typu -
    # bere se jako běžná spotřeba, aby se chování neměnilo (viz const.py).
    om_type = entry.data.get(CONF_OM_TYPE) or OM_TYPE_CONSUMPTION

    _LOGGER.debug(
        "Nastavuji ČEZ config entry (EAN uložen=%s, UID uložen=%s).",
        bool(ean),
        bool(uid),
    )
    portal_environment = (
        (entry.data[CONF_PORTAL_ENVIRONMENT], entry.data[CONF_PORTAL_PARTNER])
        if entry.data.get(CONF_PORTAL_ENVIRONMENT) and entry.data.get(CONF_PORTAL_PARTNER)
        else None
    )
    ssl_context = get_default_context() if get_default_context is not None else None
    client = CezDistribuceApiClient(
        username=username,
        password=password,
        portal_environment=portal_environment,
        ssl_context=ssl_context,
    )

    try:
        await client.login()
    except CezInvalidCredentialsError as err:
        await client.async_close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:
        # Síť, timeout, změna přihlašovacího toku nebo mezikrok portálu
        # (podmínky, smlouva) - HA to zkusí znovu s rostoucím odstupem.
        await client.async_close()
        raise ConfigEntryNotReady(f"Přihlášení do ČEZ selhalo: {err}") from err

    entry.async_on_unload(client.async_close)
    try:
        await _async_ensure_portal_environment(hass, entry, client, uid, ean)
    except Exception:
        await client.async_close()
        raise
    _LOGGER.debug("Přihlášení config entry dokončeno; pokračuji načtením dat.")
    partner = entry.data.get(CONF_PARTNER, "")
    anlage = entry.data.get(CONF_ANLAGE, "")
    hdo_signal = entry.options.get(
        CONF_HDO_SIGNAL, entry.data.get(CONF_HDO_SIGNAL, "")
    )
    price_vt = float(
        entry.options.get(CONF_PRICE_VT, entry.data.get(CONF_PRICE_VT, DEFAULT_PRICE_VT))
    )
    price_nt = float(
        entry.options.get(CONF_PRICE_NT, entry.data.get(CONF_PRICE_NT, DEFAULT_PRICE_NT))
    )

    def _store_new_uid(new_uid: str) -> None:
        # Portál pro stejný EAN vrátil jiné UID (issue #43) - coordinator
        # ho volá až po úspěšném načtení odečtů s novým UID.
        hass.config_entries.async_update_entry(entry, data={**entry.data, "uid": new_uid})

    coordinator = CezDistribuceCoordinator(
        hass,
        client,
        ean=ean,
        uid=uid,
        partner=partner,
        anlage=anlage,
        om_type=om_type,
        hdo_signal=hdo_signal,
        price_vt=price_vt,
        price_nt=price_nt,
        on_uid_changed=_store_new_uid,
        defer_initial_pnd_sync=bool(partner and anlage),
    )
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug("První načtení dat config entry dokončeno.")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.debug("Platformy ČEZ byly načteny.")
    if partner and anlage:
        pnd_task = _async_create_background_task(
            hass,
            coordinator.async_sync_pnd_in_background(),
            name=f"{DOMAIN} initial pnd sync {ean}",
        )
        entry.async_on_unload(pnd_task.cancel)
        _LOGGER.debug("Synchronizace hodinových dat (MEPAS) běží na pozadí.")
    else:
        task = _async_create_background_task(
            hass,
            _async_enrich_partner_and_anlage(hass, entry, client, coordinator, uid),
            name=f"{DOMAIN} enrich supply point metadata",
        )
        entry.async_on_unload(task.cancel)
        _LOGGER.debug("Dohledání metadat hodinových dat běží na pozadí.")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Odstraní integraci."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN) and hass.services.has_service(
            DOMAIN, SERVICE_RECALCULATE_STATISTICS
        ):
            hass.services.async_remove(DOMAIN, SERVICE_RECALCULATE_STATISTICS)
    return unload_ok
