"""Sensor platform for Power Topology."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, NAME
from .learner import PowerTopologyLearner


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Power Topology diagnostic sensor."""
    learner: PowerTopologyLearner = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PowerTopologySensor(learner)])


class PowerTopologySensor(SensorEntity):
    """Summary of learned electrical nesting relationships."""

    _attr_name = NAME
    _attr_unique_id = "power_topology_summary"
    _attr_icon = "mdi:transmission-tower"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, learner: PowerTopologyLearner) -> None:
        self._learner = learner

    async def async_added_to_hass(self) -> None:
        """Subscribe to learner updates."""
        self.async_on_remove(
            self._learner.async_add_listener(self.async_write_ha_state)
        )

    @property
    def native_value(self) -> str:
        """Return the strongest current learning state."""
        return str(self._learner.snapshot()["status"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return bounded topology diagnostics."""
        snapshot = self._learner.snapshot()
        return {key: value for key, value in snapshot.items() if key != "status"}
