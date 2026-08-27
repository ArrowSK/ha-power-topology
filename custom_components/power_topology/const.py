"""Constants for Power Topology."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "power_topology"
NAME = "Power Topology"

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.evidence"

RETENTION_DAYS = 100
MAX_RELATIONSHIPS = 200

SETTLE_SECONDS = 22
CONTAMINATION_WINDOW_SECONDS = 6
REGISTRY_RESCAN_INTERVAL = timedelta(hours=6)
MIN_CHILD_DELTA_W = 5.0
MAX_MATCHING_PARENTS_PER_EVENT = 6

NEW_RELATION_MIN_RATIO = 0.75
NEW_RELATION_MAX_RATIO = 1.30
EXISTING_RELATION_MIN_RATIO = 0.70
EXISTING_RELATION_MAX_RATIO = 1.35
EXISTING_RELATION_TOLERANCE = 0.15

EXCLUDED_PLATFORMS = frozenset(
    {
        "derivative",
        "filter",
        "group",
        "history_stats",
        "integration",
        "min_max",
        "powercalc",
        "statistics",
        "template",
        "threshold",
        "utility_meter",
    }
)

STATUS_WAITING = "waiting"
STATUS_LEARNING = "learning"
STATUS_SUSPECTED = "suspected"
STATUS_STRONG = "strong"
STATUS_CONFIRMED = "confirmed"

STATUS_ORDER = {
    STATUS_LEARNING: 0,
    STATUS_SUSPECTED: 1,
    STATUS_STRONG: 2,
    STATUS_CONFIRMED: 3,
}
