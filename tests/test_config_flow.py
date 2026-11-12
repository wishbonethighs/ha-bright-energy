"""Config flow tests."""

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.bright_energy.const import CONF_VIRTUAL_ENTITY_ID, DOMAIN

from .conftest import VE_ID


async def test_user_flow(hass: HomeAssistant, glow) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "me@example.com", CONF_PASSWORD: "nope"}
    )
    assert bad["errors"] == {"base": "invalid_auth"}

    good = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "me@example.com", CONF_PASSWORD: "secret"}
    )
    assert good["type"] is FlowResultType.CREATE_ENTRY
    assert good["data"][CONF_VIRTUAL_ENTITY_ID] == VE_ID
    assert good["title"] == "Bright (Home – IG10)"
    await hass.async_block_till_done()
