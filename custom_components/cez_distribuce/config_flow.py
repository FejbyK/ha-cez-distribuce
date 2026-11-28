"""Config flow pro ČEZ."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.util import dt as dt_util

try:
    from homeassistant.data_entry_flow import section as flow_section
except ImportError:
    flow_section = None  # type: ignore[assignment]

try:
    from homeassistant.util.ssl import get_default_context
except ImportError:
    get_default_context = None  # type: ignore[assignment]

from .api import (
    CezAuthError,
    CezDistribuceApiClient,
    CezInvalidCredentialsError,
    CezPortalNoDataAccessError,
    CezPortalUnavailableError,
    CezUnexpectedLoginPageError,
    extract_supply_points,
)
from .const import (
    CONF_ANLAGE,
    CONF_EAN,
    CONF_ENABLE_COSTS,
    CONF_ENABLE_HDO,
    CONF_ENABLE_OUTAGES,
    CONF_ENABLE_PND,
    CONF_ENABLE_READINGS,
    CONF_HDO_SIGNAL,
    CONF_OM_TYPE,
    CONF_PARTNER,
    CONF_PASSWORD,
    CONF_PORTAL_ENVIRONMENT,
    CONF_PORTAL_PARTNER,
    CONF_PRICE_NT,
    CONF_PRICE_VT,
    CONF_USERNAME,
    DATA_SIGNALS,
    DEFAULT_ENABLE_COSTS,
    DEFAULT_ENABLE_HDO,
    DEFAULT_ENABLE_OUTAGES,
    DEFAULT_ENABLE_PND,
    DEFAULT_ENABLE_READINGS,
    DEFAULT_PRICE_NT,
    DEFAULT_PRICE_VT,
    DOMAIN,
    OM_TYPE_CONSUMPTION,
    OM_TYPES_PRODUCTION,
    PORTAL_ENVIRONMENT_NAMES,
)
from .hdo_split import build_hdo_signal_options

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_CONSUMPTION_FEATURE_DEFAULTS: tuple[tuple[str, bool], ...] = (
    (CONF_ENABLE_HDO, DEFAULT_ENABLE_HDO),
    (CONF_ENABLE_READINGS, DEFAULT_ENABLE_READINGS),
    (CONF_ENABLE_OUTAGES, DEFAULT_ENABLE_OUTAGES),
    (CONF_ENABLE_PND, DEFAULT_ENABLE_PND),
    (CONF_ENABLE_COSTS, DEFAULT_ENABLE_COSTS),
)
_PRODUCTION_FEATURE_DEFAULTS: tuple[tuple[str, bool], ...] = (
    (CONF_ENABLE_OUTAGES, DEFAULT_ENABLE_OUTAGES),
    (CONF_ENABLE_PND, DEFAULT_ENABLE_PND),
)
SECTION_FEATURES = "features"


def _hass_language(hass: Any) -> str:
    config = getattr(hass, "config", None)
    lang = getattr(config, "language", None) or "cs"
    return "cs" if str(lang).lower().startswith("cs") else "en"


def _entry_feature_enabled(
    entry: config_entries.ConfigEntry, key: str, default: bool = True
) -> bool:
    return bool(entry.options.get(key, entry.data.get(key, default)))


def _has_any_enabled_feature(
    values: Mapping[str, Any], is_production: bool, *, include_hdo: bool = True
) -> bool:
    if is_production:
        return any(bool(values.get(k, d)) for k, d in _PRODUCTION_FEATURE_DEFAULTS)
    keys = [
        (k, d)
        for k, d in _CONSUMPTION_FEATURE_DEFAULTS
        if include_hdo or k != CONF_ENABLE_HDO
    ]
    return any(bool(values.get(k, d)) for k, d in keys)


def _extract_feature_inputs(user_input: Mapping[str, Any]) -> Mapping[str, Any]:
    """Podporuje jak vnořenou sekci 'features' (HA 2024.4+), tak ploché klíče."""
    nested = user_input.get(SECTION_FEATURES)
    if isinstance(nested, Mapping):
        return nested
    return user_input


def _build_features_schema_fields(
    feature_defs: tuple[tuple[str, bool], ...],
    defaults: Mapping[str, Any],
    fallback_values: Mapping[str, bool] | None = None,
) -> dict[Any, Any]:
    feature_inputs = _extract_feature_inputs(defaults)
    inner: dict[Any, Any] = {}
    for key, default in feature_defs:
        fallback = (
            fallback_values[key]
            if fallback_values is not None and key in fallback_values
            else default
        )
        inner[vol.Required(key, default=bool(feature_inputs.get(key, fallback)))] = bool
    if flow_section is not None:
        return {
            vol.Required(SECTION_FEATURES): flow_section(
                vol.Schema(inner), {"collapsed": False}
            )
        }
    return inner


class CezDistribuceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Průvodce nastavením integrace ČEZ."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._supply_points: list[dict] = []
        self._selected_ean: str = ""
        self._selected_uid: str = ""
        self._selected_partner: str = ""
        self._selected_anlage: str = ""
        self._selected_title: str = ""
        self._selected_om_type: str = ""
        self._selected_environment: tuple[str, str] | None = None
        self._hdo_signals: list[str] = []
        self._signals_data: Any = None
        self._client: CezDistribuceApiClient | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Volba „Konfigurovat“ - ceny VT/NT (issue #44)."""
        return CezOptionsFlow()

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: config_entries.ConfigEntry) -> bool:
        """Možnosti integrace (funkce, u spotřeby i HDO a ceny) jsou dostupné pro všechna OM."""
        return True

    @staticmethod
    def _login_error_key(err: Exception) -> str:
        """Převede výjimku z přihlášení na klíč chyby formuláře."""
        if isinstance(err, CezInvalidCredentialsError):
            return "invalid_auth"
        if isinstance(err, CezPortalUnavailableError):
            _LOGGER.warning("%s", err)
            return "portal_unavailable"
        if isinstance(err, CezUnexpectedLoginPageError):
            return "portal_action_required"
        if isinstance(err, CezPortalNoDataAccessError):
            _LOGGER.warning("%s", err)
            return "portal_no_data_access"
        if not isinstance(err, CezAuthError):
            _LOGGER.exception("Neočekávaná chyba při přihlašování", exc_info=err)
        return "cannot_connect"

    async def _async_close_client(self) -> None:
        """Uzavře HTTP session sdíleného klienta průvodce."""
        if self._client is not None:
            client = self._client
            self._client = None
            await client.async_close()

    async def _async_get_client(self) -> CezDistribuceApiClient:
        """Přihlásí se nejvýše jednou a vrací klienta sdíleného pro celý flow."""
        if self._client is None:
            _LOGGER.debug("Config flow: vytvářím klienta a přihlašuji se k ČEZ.")
            ssl_context = get_default_context() if get_default_context is not None else None
            client = CezDistribuceApiClient(
                username=self._username,
                password=self._password,
                ssl_context=ssl_context,
            )
            try:
                await client.login()
            except Exception:
                await client.async_close()
                raise
            self._client = client
        return self._client

    # ------------------------------------------------------------------
    # Krok 1 – přihlašovací údaje
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Zadání přihlašovacích údajů."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            # Nové odeslání formuláře = možná jiné údaje, přihlásit znovu.
            await self._async_close_client()

            try:
                client = await self._async_get_client()
                _LOGGER.debug("Config flow: načítám odběrná místa.")
                self._supply_points = await self._async_load_supply_points(client)
                _LOGGER.debug(
                    "Config flow: načteno odběrných míst=%d.",
                    len(self._supply_points),
                )
            except Exception as err:  # noqa: BLE001
                errors["base"] = self._login_error_key(err)

            if not errors:
                if not self._supply_points:
                    errors["base"] = "no_supply_points"
                elif len(self._supply_points) == 1:
                    self._select_point(self._supply_points[0])
                    return await self.async_step_select_hdo_signal()
                else:
                    return await self.async_step_select_supply_point()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Krok 2 – výběr odběrného místa (pokud je jich více)
    # ------------------------------------------------------------------

    async def async_step_select_supply_point(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Výběr odběrného místa."""
        if user_input is not None:
            selected_ean = user_input[CONF_EAN]
            point = next(
                (p for p in self._supply_points if p.get("ean") == selected_ean), None
            )
            if point:
                self._select_point(point)
                return await self.async_step_select_hdo_signal()

        show_environment = (
            len({p.get("_portal_environment") for p in self._supply_points}) > 1
        )
        options = {
            p["ean"]: self._supply_point_label(p, show_environment)
            for p in self._supply_points
            if "ean" in p
        }

        return self.async_show_form(
            step_id="select_supply_point",
            data_schema=vol.Schema({vol.Required(CONF_EAN): vol.In(options)}),
        )

    # ------------------------------------------------------------------
    # Krok 3 – výběr HDO signálu
    # ------------------------------------------------------------------

    async def async_step_select_hdo_signal(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Výběr kódu HDO signálu se spínacími časy, cen a funkcí integrace."""
        errors: dict[str, str] = {}

        is_production = (
            self._selected_om_type or OM_TYPE_CONSUMPTION
        ) in OM_TYPES_PRODUCTION

        # HDO/tarify se týkají jen běžné spotřeby ("S") - odběrné místo
        # výroby/mikrozdroje (FVE, typ "V"/"M"/"B") žádný spínací signál nemá,
        # pokračujeme rovnou na výběr funkcí (issue #24).
        if is_production:
            self._hdo_signals = []
            self._signals_data = None
            return await self.async_step_configure_features()

        if self._signals_data is None and not self._hdo_signals:
            try:
                client = await self._async_get_client()
                if self._selected_environment:
                    await client.select_environment(*self._selected_environment)
                self._signals_data = await client.get_signals(self._selected_ean)
                options_loaded, _ = build_hdo_signal_options(
                    self._signals_data,
                    today=dt_util.now().date(),
                    language=_hass_language(self.hass),
                )
                self._hdo_signals = list(options_loaded.keys())
                _LOGGER.debug(
                    "Config flow: načteno HDO signálů=%d.",
                    len(self._hdo_signals),
                )
            except Exception:
                _LOGGER.exception("Nepodařilo se načíst HDO signály.")

        # Jednotarifní OM bez HDO signálů -> výběr funkcí a ceny VT
        if not self._hdo_signals:
            return await self.async_step_configure_features()

        options, signals_overview = build_hdo_signal_options(
            self._signals_data,
            today=dt_util.now().date(),
            language=_hass_language(self.hass),
            extra_codes=self._hdo_signals,
        )

        if user_input is not None:
            feature_inputs = _extract_feature_inputs(user_input)
            features = {
                k: bool(feature_inputs.get(k, d))
                for k, d in _CONSUMPTION_FEATURE_DEFAULTS
            }
            if not _has_any_enabled_feature(features, is_production=False):
                errors["base"] = "no_features_enabled"
            else:
                return await self._async_create_entry(
                    user_input[CONF_HDO_SIGNAL],
                    user_input[CONF_PRICE_VT],
                    user_input[CONF_PRICE_NT],
                    features=features,
                )

        _LOGGER.debug(
            "Config flow: zobrazuji výběr HDO signálu, možností=%d.",
            len(options),
        )
        defaults = user_input or {}
        return self.async_show_form(
            step_id="select_hdo_signal",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_HDO_SIGNAL,
                    default=defaults.get(CONF_HDO_SIGNAL, self._hdo_signals[0]),
                ): vol.In(options),
                vol.Required(
                    CONF_PRICE_VT,
                    default=float(defaults.get(CONF_PRICE_VT, DEFAULT_PRICE_VT)),
                ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                vol.Required(
                    CONF_PRICE_NT,
                    default=float(defaults.get(CONF_PRICE_NT, DEFAULT_PRICE_NT)),
                ): vol.All(vol.Coerce(float), vol.Range(min=0)),
                **_build_features_schema_fields(
                    _CONSUMPTION_FEATURE_DEFAULTS, defaults
                ),
            }),
            description_placeholders={
                "signal_count": str(len(options)),
                "signals_overview": signals_overview,
            },
            errors=errors,
        )

    async def async_step_configure_features(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Výběr aktivních funkcí pro jednotarifní nebo výrobní (FVE) odběrné místo."""
        errors: dict[str, str] = {}
        is_production = (
            self._selected_om_type or OM_TYPE_CONSUMPTION
        ) in OM_TYPES_PRODUCTION
        feature_defs = (
            _PRODUCTION_FEATURE_DEFAULTS
            if is_production
            else tuple(
                (k, d) for k, d in _CONSUMPTION_FEATURE_DEFAULTS if k != CONF_ENABLE_HDO
            )
        )

        if user_input is not None:
            feature_inputs = _extract_feature_inputs(user_input)
            features = {k: bool(feature_inputs.get(k, d)) for k, d in feature_defs}
            if not _has_any_enabled_feature(
                features, is_production=is_production, include_hdo=False
            ):
                errors["base"] = "no_features_enabled"
            else:
                price_vt = (
                    float(user_input.get(CONF_PRICE_VT, DEFAULT_PRICE_VT))
                    if not is_production
                    else DEFAULT_PRICE_VT
                )
                return await self._async_create_entry(
                    "", price_vt, DEFAULT_PRICE_NT, features=features
                )

        defaults = user_input or {}
        schema_fields: dict[Any, Any] = {}
        if not is_production:
            schema_fields[
                vol.Required(
                    CONF_PRICE_VT,
                    default=float(defaults.get(CONF_PRICE_VT, DEFAULT_PRICE_VT)),
                )
            ] = vol.All(vol.Coerce(float), vol.Range(min=0))
        schema_fields.update(_build_features_schema_fields(feature_defs, defaults))

        return self.async_show_form(
            step_id="configure_features",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Reauth (změněné heslo) a reconfigure (údaje, prostředí portálu)
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """Spustí HA, když portál odmítne uložené heslo."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Zadání nového hesla ke stávajícímu účtu."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            ssl_context = get_default_context() if get_default_context is not None else None
            async with CezDistribuceApiClient(
                username=entry.data[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                ssl_context=ssl_context,
            ) as client:
                try:
                    await client.login()
                except Exception as err:  # noqa: BLE001
                    errors["base"] = self._login_error_key(err)
                else:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                    )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Změna přihlašovacích údajů a znovu dohledání prostředí portálu.

        Odběrné místo (EAN) se nemění - je na něm unique_id entry, entity
        i dlouhodobé statistiky. Jiné odběrné místo = nová integrace.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data_updates: dict[str, Any] = {
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            environment = None
            ssl_context = get_default_context() if get_default_context is not None else None
            async with CezDistribuceApiClient(
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                ssl_context=ssl_context,
            ) as client:
                try:
                    await client.login()
                    if client.environment_options:
                        environment = await client.find_environment_for_supply_point(
                            entry.data.get("uid", ""), entry.data[CONF_EAN]
                        )
                        if environment is None:
                            errors["base"] = "supply_point_not_found"
                    elif not any(
                        p.get("ean") == entry.data[CONF_EAN]
                        for p in extract_supply_points(await client.get_supply_points())
                    ):
                        errors["base"] = "supply_point_not_found"
                    if not errors:
                        # Portál může pro stejný EAN časem vrátit jiné UID
                        # (issue #43) - překonfigurování ho rovnou obnoví.
                        new_uid = await client.resolve_supply_point_uid(entry.data[CONF_EAN])
                        if new_uid and new_uid != entry.data.get("uid"):
                            data_updates["uid"] = new_uid
                except Exception as err:  # noqa: BLE001
                    errors["base"] = self._login_error_key(err)
            if not errors:
                if environment:
                    data_updates[CONF_PORTAL_ENVIRONMENT] = environment[0]
                    data_updates[CONF_PORTAL_PARTNER] = environment[1]
                return self.async_update_reload_and_abort(entry, data_updates=data_updates)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"ean": entry.data.get(CONF_EAN, "")},
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Pomocné metody
    # ------------------------------------------------------------------

    async def _async_load_supply_points(
        self, client: CezDistribuceApiClient
    ) -> list[dict]:
        """Načte odběrná místa ze všech prostředí portálu (issue #37).

        Účet bez výběru prostředí (běžný případ) má environment_options
        prázdné a načte se jen jednou. Jinak se postupně přepne do každé
        kombinace prostředí/partner a místa se označí, odkud pocházejí.
        """
        if len(client.environment_options) <= 1:
            points = extract_supply_points(await client.get_supply_points())
            for point in points:
                point["_portal_environment"] = client.portal_environment
            return points

        points: list[dict] = []
        seen_eans: set[str] = set()
        for option in client.environment_options:
            environment = (option["environment_id"], option["partner_id"])
            try:
                await client.select_environment(*environment)
                found = extract_supply_points(await client.get_supply_points())
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Config flow: nepodařilo se načíst odběrná místa v prostředí %s.",
                    environment[0],
                    exc_info=True,
                )
                continue
            _LOGGER.debug(
                "Config flow: prostředí %s, odběrných míst=%d.", environment[0], len(found)
            )
            for point in found:
                ean = point.get("ean")
                if ean and ean in seen_eans:
                    continue
                seen_eans.add(ean)
                point["_portal_environment"] = environment
                points.append(point)
        return points

    def _supply_point_label(self, point: dict, show_environment: bool) -> str:
        details = [point["ean"], point.get("typText") or point.get("typ") or "?"]
        environment = point.get("_portal_environment")
        if show_environment and environment:
            language = "cs" if (self.hass.config.language or "").startswith("cs") else "en"
            names = PORTAL_ENVIRONMENT_NAMES[language]
            details.append(names.get(environment[0], environment[0]))
        address = point.get("adresa", {}).get("adresaComplete") or point["ean"]
        return f"{address} ({', '.join(details)})"

    def _select_point(self, point: dict) -> None:
        """Uloží vybrané odběrné místo."""
        self._selected_ean = point.get("ean", "")
        self._selected_uid = point.get("uid", "")
        self._selected_partner = point.get("partner", "")
        self._selected_environment = point.get("_portal_environment")
        self._selected_om_type = point.get("typ") or OM_TYPE_CONSUMPTION
        adresa = point.get("adresa") or {}
        base_title = (
            adresa.get("adresaComplete")
            if isinstance(adresa, dict)
            else None
        ) or f"ČEZ {self._selected_ean}"
        type_text = point.get("typText")
        self._selected_title = (
            f"{base_title} ({type_text})"
            if type_text and type_text != "Spotřeba"
            else base_title
        ).strip() or f"ČEZ {self._selected_ean}".strip() or "ČEZ"

    async def _async_create_entry(
        self,
        hdo_signal: str,
        price_vt: float,
        price_nt: float,
        features: Mapping[str, bool] | None = None,
    ) -> FlowResult:
        """Vytvoří config entry."""
        title = self._selected_title.strip() or f"ČEZ {self._selected_ean}".strip() or "ČEZ"
        _LOGGER.debug(
            "Config flow: vytvářím config entry (název vyplněn=%s, EAN uložen=%s).",
            bool(title),
            bool(self._selected_ean),
        )
        try:
            await self.async_set_unique_id(self._selected_ean)
            self._abort_if_unique_id_configured()
        finally:
            await self._async_close_client()
        return self.async_create_entry(
            title=title,
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_EAN: self._selected_ean,
                "uid": self._selected_uid,
                CONF_PARTNER: self._selected_partner,
                CONF_ANLAGE: self._selected_anlage,
                **(
                    {
                        CONF_PORTAL_ENVIRONMENT: self._selected_environment[0],
                        CONF_PORTAL_PARTNER: self._selected_environment[1],
                    }
                    if self._selected_environment
                    else {}
                ),
                CONF_OM_TYPE: self._selected_om_type or OM_TYPE_CONSUMPTION,
                CONF_HDO_SIGNAL: hdo_signal,
                CONF_PRICE_VT: price_vt,
                CONF_PRICE_NT: price_nt,
                **(dict(features) if features else {}),
            },
        )


class CezOptionsFlow(config_entries.OptionsFlowWithReload):
    """Změna cen VT/NT a HDO signálu bez nového přidání odběrného místa (issue #44).

    Hodnoty se ukládají do entry.options; koordinátor i senzory je čtou
    přednostně odtud, se zálohou na entry.data (hodnoty zadané při přidání).
    OptionsFlowWithReload po uložení integraci sám znovu načte."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}
        is_production = (
            entry.data.get(CONF_OM_TYPE) or OM_TYPE_CONSUMPTION
        ) in OM_TYPES_PRODUCTION
        current_hdo = entry.options.get(
            CONF_HDO_SIGNAL, entry.data.get(CONF_HDO_SIGNAL, "")
        )
        current_features = {
            k: _entry_feature_enabled(entry, k, d)
            for k, d in (
                _PRODUCTION_FEATURE_DEFAULTS
                if is_production
                else _CONSUMPTION_FEATURE_DEFAULTS
            )
        }

        if user_input is not None:
            feature_inputs = _extract_feature_inputs(user_input)
            resolved_features = {
                k: bool(feature_inputs.get(k, current_features[k]))
                for k in current_features
            }
            if not _has_any_enabled_feature(resolved_features, is_production=is_production):
                errors["base"] = "no_features_enabled"
            elif is_production:
                return self.async_create_entry(data=resolved_features)
            else:
                options_data: dict[str, Any] = {
                    CONF_PRICE_VT: user_input[CONF_PRICE_VT],
                    CONF_PRICE_NT: user_input[CONF_PRICE_NT],
                    **resolved_features,
                }
                if CONF_HDO_SIGNAL in user_input:
                    options_data[CONF_HDO_SIGNAL] = user_input[CONF_HDO_SIGNAL]
                elif current_hdo:
                    options_data[CONF_HDO_SIGNAL] = current_hdo
                return self.async_create_entry(data=options_data)

        defaults = user_input or {}
        schema_fields: dict[Any, Any] = {}
        signals_overview = ""

        if is_production:
            schema_fields.update(
                _build_features_schema_fields(
                    _PRODUCTION_FEATURE_DEFAULTS, defaults, current_features
                )
            )
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(schema_fields),
                description_placeholders={"signals_overview": ""},
                errors=errors,
            )

        current_vt = entry.options.get(
            CONF_PRICE_VT, entry.data.get(CONF_PRICE_VT, DEFAULT_PRICE_VT)
        )
        current_nt = entry.options.get(
            CONF_PRICE_NT, entry.data.get(CONF_PRICE_NT, DEFAULT_PRICE_NT)
        )

        coordinator = (self.hass.data.get(DOMAIN) or {}).get(entry.entry_id)
        signals_data = (
            (coordinator.data or {}).get(DATA_SIGNALS)
            if coordinator is not None
            else None
        )
        signal_options, raw_overview = build_hdo_signal_options(
            signals_data,
            today=dt_util.now().date(),
            language=_hass_language(self.hass),
            extra_codes=(current_hdo,) if current_hdo else (),
        )
        if raw_overview:
            signals_overview = f"\n\n{raw_overview}"

        if signal_options:
            first_signal = next(iter(signal_options))
            schema_fields[
                vol.Required(
                    CONF_HDO_SIGNAL,
                    default=defaults.get(CONF_HDO_SIGNAL, current_hdo or first_signal),
                )
            ] = vol.In(signal_options)
        schema_fields[
            vol.Required(
                CONF_PRICE_VT, default=float(defaults.get(CONF_PRICE_VT, current_vt))
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0))
        schema_fields[
            vol.Required(
                CONF_PRICE_NT, default=float(defaults.get(CONF_PRICE_NT, current_nt))
            )
        ] = vol.All(vol.Coerce(float), vol.Range(min=0))
        schema_fields.update(
            _build_features_schema_fields(
                _CONSUMPTION_FEATURE_DEFAULTS, defaults, current_features
            )
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={"signals_overview": signals_overview},
            errors=errors,
        )
