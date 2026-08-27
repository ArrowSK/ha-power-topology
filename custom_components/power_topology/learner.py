"""Event-driven topology learner for Home Assistant."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from datetime import date, datetime, timedelta
import math
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONTAMINATION_WINDOW_SECONDS,
    EXCLUDED_PLATFORMS,
    MAX_MATCHING_PARENTS_PER_EVENT,
    MIN_CHILD_DELTA_W,
    NEW_RELATION_MAX_RATIO,
    NEW_RELATION_MIN_RATIO,
    REGISTRY_RESCAN_INTERVAL,
    RETENTION_DAYS,
    SETTLE_SECONDS,
    STATUS_WAITING,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .evidence import EvidenceBook, Relationship

Listener = Callable[[], None]


class PowerTopologyLearner:
    """Observe natural metered-switch transitions and learn nesting."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY}.{entry.entry_id}",
        )
        self._book = EvidenceBook()
        self._listeners: set[Listener] = set()
        self._state_unsubs: list[Callable[[], None]] = []
        self._interval_unsub: Callable[[], None] | None = None
        self._analysis_tasks: set[asyncio.Task[Any]] = set()
        self._save_task: asyncio.Task[Any] | None = None

        self._power_entity_by_device: dict[str, str] = {}
        self._switch_device_by_entity: dict[str, str] = {}
        self._power_values: dict[str, float] = {}
        self._device_names: dict[str, str] = {}
        self._switch_transitions: deque[tuple[datetime, str]] = deque()

        self._observed_transitions = 0
        self._ignored_transitions = 0
        self._last_observation: str | None = None
        self._running = False

    async def async_start(self) -> None:
        """Load persisted evidence and begin observing Home Assistant."""
        raw = await self._store.async_load()
        self._book = EvidenceBook.from_dict(raw)
        self._book.prune(dt_util.now().date())

        await self._async_rebuild_registry()
        self._interval_unsub = async_track_time_interval(
            self.hass,
            self._handle_registry_rescan,
            REGISTRY_RESCAN_INTERVAL,
        )
        self._running = True
        self._notify()

    async def async_stop(self) -> None:
        """Stop listeners and safely persist pending evidence."""
        self._running = False

        if self._interval_unsub is not None:
            self._interval_unsub()
            self._interval_unsub = None

        self._clear_state_subscriptions()

        for task in list(self._analysis_tasks):
            task.cancel()
        if self._analysis_tasks:
            await asyncio.gather(*self._analysis_tasks, return_exceptions=True)
        self._analysis_tasks.clear()

        if self._save_task is not None and not self._save_task.done():
            self._save_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._save_task
        self._save_task = None

        self._book.prune(dt_util.now().date())
        await self._store.async_save(self._book.to_dict())

    @callback
    def async_add_listener(self, listener: Listener) -> Callable[[], None]:
        """Register a state listener and return an unsubscribe callback."""
        self._listeners.add(listener)

        @callback
        def _remove() -> None:
            self._listeners.discard(listener)

        return _remove

    @callback
    def snapshot(self) -> dict[str, Any]:
        """Return a compact diagnostic snapshot for the sensor platform."""
        counts = self._book.counts()
        has_meters = bool(self._power_entity_by_device)
        has_switches = bool(self._switch_device_by_entity)

        status = self._book.overall_status()
        if not self._running or not has_meters or not has_switches:
            status = STATUS_WAITING

        return {
            "status": status,
            "retention_days": RETENTION_DAYS,
            "read_only": True,
            "affects_energy_totals": False,
            "physical_power_devices": len(self._power_entity_by_device),
            "metered_switches": len(self._switch_device_by_entity),
            "candidate_relationships": len(self._book.relationships),
            "suspected_relationships": counts["suspected"],
            "strong_relationships": counts["strong"],
            "confirmed_relationships": counts["confirmed"],
            "observed_transitions": self._observed_transitions,
            "ignored_transitions": self._ignored_transitions,
            "last_observation": self._last_observation,
            "relationships": self._book.top_relationships(),
        }

    @callback
    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    @callback
    def _handle_registry_rescan(self, _now: datetime) -> None:
        self.hass.async_create_task(self._async_rebuild_registry())

    async def _async_rebuild_registry(self) -> None:
        """Discover eligible physical power meters and metered switches."""
        self._clear_state_subscriptions()

        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        power_by_device: dict[str, list[str]] = {}
        switch_entries: list[tuple[str, str]] = []

        for entity_id, entry in entity_registry.entities.items():
            if entry.disabled_by is not None or not entry.device_id:
                continue

            state = self.hass.states.get(entity_id)
            if state is None:
                continue

            domain = entity_id.split(".", 1)[0]
            if domain == "sensor" and self._is_eligible_power_sensor(entry, state):
                power_by_device.setdefault(entry.device_id, []).append(entity_id)
            elif domain == "switch" and state.state in {STATE_ON, STATE_OFF}:
                switch_entries.append((entity_id, entry.device_id))

        # V0.1 is intentionally conservative: devices with multiple eligible
        # physical power sensors are ambiguous and are excluded until
        # channel-aware logic is added.
        self._power_entity_by_device = {
            device_id: entity_ids[0]
            for device_id, entity_ids in power_by_device.items()
            if len(entity_ids) == 1
        }

        eligible_devices = set(self._power_entity_by_device)
        self._switch_device_by_entity = {
            entity_id: device_id
            for entity_id, device_id in switch_entries
            if device_id in eligible_devices
        }

        self._device_names = {}
        for device_id in eligible_devices:
            device = device_registry.async_get(device_id)
            if device is None:
                self._device_names[device_id] = device_id
            else:
                self._device_names[device_id] = device.name_by_user or device.name or device_id

        self._power_values = {}
        for entity_id in self._power_entity_by_device.values():
            value = self._state_to_watts(self.hass.states.get(entity_id))
            if value is not None:
                self._power_values[entity_id] = value

        power_entities = list(self._power_entity_by_device.values())
        if power_entities:
            self._state_unsubs.append(
                async_track_state_change_event(
                    self.hass,
                    power_entities,
                    self._handle_power_state,
                )
            )

        switch_entities = list(self._switch_device_by_entity)
        if switch_entities:
            self._state_unsubs.append(
                async_track_state_change_event(
                    self.hass,
                    switch_entities,
                    self._handle_switch_state,
                )
            )

        self._notify()

    def _is_eligible_power_sensor(self, entry: er.RegistryEntry, state: State) -> bool:
        """Return whether an entity is a physical device power meter."""
        if entry.platform in EXCLUDED_PLATFORMS:
            return False
        if state.attributes.get("device_class") != "power":
            return False
        return self._state_to_watts(state) is not None

    @staticmethod
    def _state_to_watts(state: State | None) -> float | None:
        if state is None or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            return None

        unit = state.attributes.get("unit_of_measurement")
        if unit not in {"W", "kW"}:
            return None

        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None

        if not math.isfinite(value):
            return None
        if unit == "kW":
            value *= 1000.0
        return value

    @callback
    def _handle_power_state(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if not isinstance(entity_id, str):
            return

        value = self._state_to_watts(event.data.get("new_state"))
        if value is None:
            self._power_values.pop(entity_id, None)
        else:
            self._power_values[entity_id] = value

    @callback
    def _handle_switch_state(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        if (
            not isinstance(entity_id, str)
            or not isinstance(old_state, State)
            or not isinstance(new_state, State)
            or old_state.state not in {STATE_ON, STATE_OFF}
            or new_state.state not in {STATE_ON, STATE_OFF}
            or old_state.state == new_state.state
        ):
            return

        child_device_id = self._switch_device_by_entity.get(entity_id)
        if child_device_id is None:
            return

        self._observed_transitions += 1
        transition_time = event.time_fired
        self._switch_transitions.append((transition_time, entity_id))
        self._prune_transition_times(transition_time)

        before = {
            device_id: self._power_values.get(power_entity_id)
            for device_id, power_entity_id in self._power_entity_by_device.items()
        }
        direction = "on" if new_state.state == STATE_ON else "off"

        task = self.hass.async_create_task(
            self._async_analyze_transition(
                switch_entity_id=entity_id,
                child_device_id=child_device_id,
                direction=direction,
                transition_time=transition_time,
                before=before,
            )
        )
        self._analysis_tasks.add(task)
        task.add_done_callback(self._analysis_tasks.discard)
        self._notify()

    def _prune_transition_times(self, now: datetime) -> None:
        keep_after = now - timedelta(seconds=SETTLE_SECONDS + 30)
        while self._switch_transitions and self._switch_transitions[0][0] < keep_after:
            self._switch_transitions.popleft()

    async def _async_analyze_transition(
        self,
        *,
        switch_entity_id: str,
        child_device_id: str,
        direction: str,
        transition_time: datetime,
        before: dict[str, float | None],
    ) -> None:
        await asyncio.sleep(SETTLE_SECONDS)

        if self._is_contaminated(switch_entity_id, transition_time):
            self._ignored_transitions += 1
            self._notify()
            return

        after = {
            device_id: self._state_to_watts(self.hass.states.get(power_entity_id))
            for device_id, power_entity_id in self._power_entity_by_device.items()
        }

        deltas: dict[str, float] = {}
        for device_id, before_value in before.items():
            after_value = after.get(device_id)
            if before_value is None or after_value is None:
                continue
            deltas[device_id] = after_value - before_value

        child_delta = deltas.get(child_device_id)
        expected_sign = 1.0 if direction == "on" else -1.0
        if child_delta is None or expected_sign * child_delta < MIN_CHILD_DELTA_W:
            self._ignored_transitions += 1
            self._notify()
            return

        broad_candidates: list[tuple[str, float]] = []
        for parent_device_id, parent_delta in deltas.items():
            if parent_device_id == child_device_id or parent_delta * child_delta <= 0:
                continue
            ratio = parent_delta / child_delta
            if NEW_RELATION_MIN_RATIO <= ratio <= NEW_RELATION_MAX_RATIO:
                broad_candidates.append((parent_device_id, ratio))

        if len(broad_candidates) > MAX_MATCHING_PARENTS_PER_EVENT:
            self._ignored_transitions += 1
            self._notify()
            return

        day = dt_util.as_local(transition_time).date()
        observed_at = transition_time.isoformat()
        child_entity_id = self._power_entity_by_device.get(child_device_id)
        if child_entity_id is None:
            self._ignored_transitions += 1
            self._notify()
            return

        existing = {
            relation.parent_device_id: relation
            for relation in self._book.relations_for_child(child_device_id)
        }
        changed = False

        for parent_device_id, relation in existing.items():
            parent_delta = deltas.get(parent_device_id)
            if parent_delta is None:
                continue

            if parent_delta * child_delta <= 0:
                self._book.record_contradiction(
                    day=day,
                    observed_at=observed_at,
                    relation=relation,
                )
                changed = True
                continue

            ratio = parent_delta / child_delta
            if relation.ratio_is_consistent(ratio):
                self._record_match(
                    relation=relation,
                    ratio=ratio,
                    day=day,
                    observed_at=observed_at,
                    direction=direction,
                    child_device_id=child_device_id,
                    child_entity_id=child_entity_id,
                )
            else:
                self._book.record_contradiction(
                    day=day,
                    observed_at=observed_at,
                    relation=relation,
                )
            changed = True

        for parent_device_id, ratio in broad_candidates:
            if parent_device_id in existing:
                continue
            self._record_match(
                relation=None,
                ratio=ratio,
                day=day,
                observed_at=observed_at,
                direction=direction,
                child_device_id=child_device_id,
                child_entity_id=child_entity_id,
                parent_device_id=parent_device_id,
            )
            changed = True

        self._last_observation = observed_at
        if changed:
            self._book.prune(dt_util.now().date())
            self._schedule_save()
        self._notify()

    def _record_match(
        self,
        *,
        relation: Relationship | None,
        ratio: float,
        day: date,
        observed_at: str,
        direction: str,
        child_device_id: str,
        child_entity_id: str,
        parent_device_id: str | None = None,
    ) -> None:
        actual_parent_id = relation.parent_device_id if relation is not None else parent_device_id
        if actual_parent_id is None:
            return

        parent_entity_id = self._power_entity_by_device.get(actual_parent_id)
        if parent_entity_id is None:
            return

        self._book.record_match(
            day=day,
            observed_at=observed_at,
            direction=direction,
            ratio=ratio,
            parent_device_id=actual_parent_id,
            child_device_id=child_device_id,
            parent_entity_id=parent_entity_id,
            child_entity_id=child_entity_id,
            parent_name=self._device_names.get(actual_parent_id, actual_parent_id),
            child_name=self._device_names.get(child_device_id, child_device_id),
        )

    def _is_contaminated(self, switch_entity_id: str, transition_time: datetime) -> bool:
        for observed_time, entity_id in self._switch_transitions:
            if entity_id == switch_entity_id and observed_time == transition_time:
                continue
            if (
                entity_id != switch_entity_id
                and abs((observed_time - transition_time).total_seconds())
                <= CONTAMINATION_WINDOW_SECONDS
            ):
                return True
        return False

    @callback
    def _schedule_save(self) -> None:
        if self._save_task is not None and not self._save_task.done():
            return
        self._save_task = self.hass.async_create_task(self._async_delayed_save())

    async def _async_delayed_save(self) -> None:
        await asyncio.sleep(30)
        self._book.prune(dt_util.now().date())
        await self._store.async_save(self._book.to_dict())

    def _clear_state_subscriptions(self) -> None:
        for unsubscribe in self._state_unsubs:
            unsubscribe()
        self._state_unsubs.clear()
