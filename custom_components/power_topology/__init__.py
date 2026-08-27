"""Power Topology integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .learner import PowerTopologyLearner

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Power Topology from a config entry."""
    learner = PowerTopologyLearner(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = learner

    await learner.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Power Topology config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    learner: PowerTopologyLearner = hass.data[DOMAIN].pop(entry.entry_id)
    await learner.async_stop()

    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)
    return True
