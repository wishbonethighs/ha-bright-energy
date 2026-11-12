"""Config flow for Bright Energy."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import BrightAuthError, BrightClient, BrightConnectionError, VirtualEntity
from .const import (
    CONF_RECHECK_DAYS,
    CONF_VIRTUAL_ENTITY_ID,
    DEFAULT_RECHECK_DAYS,
    DOMAIN,
    MAX_RECHECK_DAYS,
    MIN_RECHECK_DAYS,
)

_LOGGER = logging.getLogger(__name__)

CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)


def _site_label(site: VirtualEntity) -> str:
    parts = [p for p in (site.name, site.postal_code) if p]
    return " – ".join(parts) or site.ve_id


class BrightConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Bright Energy."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise."""
        self._credentials: dict[str, str] = {}
        self._sites: list[VirtualEntity] = []

    async def _validate(self, user_input: dict[str, Any]) -> tuple[list[VirtualEntity], str | None]:
        client = BrightClient(
            async_get_clientsession(self.hass),
            user_input[CONF_USERNAME],
            user_input[CONF_PASSWORD],
        )
        try:
            await client.authenticate()
            sites = await client.get_virtual_entities()
        except BrightAuthError:
            return [], "invalid_auth"
        except BrightConnectionError:
            return [], "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error logging in to Bright")
            return [], "unknown"
        if not sites:
            return [], "no_sites"
        return sites, None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for Bright credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            sites, error = await self._validate(user_input)
            if error:
                errors["base"] = error
            else:
                self._credentials = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                }
                self._sites = sites
                if len(sites) == 1:
                    return await self._create(sites[0])
                return await self.async_step_site()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(CREDENTIALS_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose a site when the account has more than one."""
        if user_input is not None:
            site = next(s for s in self._sites if s.ve_id == user_input[CONF_VIRTUAL_ENTITY_ID])
            return await self._create(site)
        options = [SelectOptionDict(value=s.ve_id, label=_site_label(s)) for s in self._sites]
        return self.async_show_form(
            step_id="site",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VIRTUAL_ENTITY_ID): SelectSelector(
                        SelectSelectorConfig(options=options)
                    )
                }
            ),
        )

    async def _create(self, site: VirtualEntity) -> ConfigFlowResult:
        await self.async_set_unique_id(site.ve_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Bright ({_site_label(site)})",
            data={**self._credentials, CONF_VIRTUAL_ENTITY_ID: site.ve_id},
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for an updated password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            credentials = {
                CONF_USERNAME: entry.data[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            sites, error = await self._validate(credentials)
            if error:
                errors["base"] = error
            elif not any(s.ve_id == entry.unique_id for s in sites):
                errors["base"] = "wrong_account"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=credentials)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD,
                            autocomplete="current-password",
                        )
                    )
                }
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BrightOptionsFlow:
        """Return the options flow."""
        return BrightOptionsFlow()


class BrightOptionsFlow(OptionsFlowWithReload):
    """Options for Bright Energy."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_RECHECK_DAYS: int(user_input[CONF_RECHECK_DAYS])}
            )
        current = self.config_entry.options.get(CONF_RECHECK_DAYS, DEFAULT_RECHECK_DAYS)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_RECHECK_DAYS, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_RECHECK_DAYS,
                            max=MAX_RECHECK_DAYS,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="days",
                        )
                    )
                }
            ),
        )
