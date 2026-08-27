# Power Topology for Home Assistant

Power Topology is a lightweight, read-only Home Assistant custom integration that learns likely electrical nesting relationships between metered devices from normal day-to-day switching.

The initial goal is deliberately narrow: detect cases where one metered device is electrically upstream of another so future energy accounting can avoid double counting. It does **not** change existing Home Assistant energy entities, switch devices, or rewrite automations.

## Design principles

- Event-driven; no fast polling loop.
- Observes only natural switch transitions.
- Never deliberately power-cycles devices.
- Uses physical device power sensors only; template, Powercalc, utility-meter, statistics and other accounting entities are excluded from parent inference.
- Stores compact daily evidence only, with a hard maximum of 100 calendar days.
- Raw power samples are not persisted.
- Relationship storage is capped at 200 candidates.
- Existing Home Assistant power and energy calculations remain untouched.
- Relationships are promoted only after repeated supporting evidence and can be weakened by contradictions.

## Current scope — v0.1

The integration discovers devices that have both:

1. a switch entity, and
2. exactly one eligible physical power sensor on the same Home Assistant device.

When one of those switches naturally changes state, Power Topology records the current power values, waits for the load to settle, then compares the change seen on the switched device with changes seen on other physical meters. Repeated proportional changes become candidate parent/child relationships.

The integration exposes one diagnostic sensor, `sensor.power_topology`, showing its current learning state and the strongest relationships in its attributes.

No deduplication or automatic accounting changes are performed in v0.1.

## Relationship confidence

Relationships progress conservatively:

- **learning** — insufficient evidence
- **suspected** — at least 3 matches with at least 75% support
- **strong** — at least 5 matches with at least 85% support
- **confirmed** — at least 8 matches, at least 90% support, with evidence from both ON and OFF transitions

A confirmed indirect ancestor is marked non-direct when a confirmed intermediate relationship exists.

## Installation

This repository is structured as a HACS custom integration. While the repository is private, install it manually for development or make it public before adding it as a normal HACS custom repository.

For a manual development install, copy:

```text
custom_components/power_topology/
```

into your Home Assistant configuration directory under:

```text
/config/custom_components/power_topology/
```

Restart Home Assistant, then go to **Settings → Devices & services → Add integration → Power Topology**.

## Safety boundary

Power Topology is an observer. It does not call switch services and does not alter your existing energy setup. Any future feature that changes accounting will be separate, explicit, and opt-in.

## Status

Early development. The first milestone is a low-overhead topology learner that can run for weeks without affecting normal Home Assistant behavior.
