"""Pure evidence model for Power Topology.

This module deliberately has no Home Assistant imports so the confidence,
retention, and graph logic can be tested independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .const import (
    EXISTING_RELATION_MAX_RATIO,
    EXISTING_RELATION_MIN_RATIO,
    EXISTING_RELATION_TOLERANCE,
    MAX_RELATIONSHIPS,
    RETENTION_DAYS,
    STATUS_CONFIRMED,
    STATUS_LEARNING,
    STATUS_ORDER,
    STATUS_STRONG,
    STATUS_SUSPECTED,
)


@dataclass(slots=True)
class DailyEvidence:
    """Compact evidence retained for one relationship on one calendar day."""

    matches: int = 0
    contradictions: int = 0
    on_matches: int = 0
    off_matches: int = 0
    ratio_sum: float = 0.0
    ratio_count: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DailyEvidence":
        """Create daily evidence from persisted JSON-compatible data."""
        return cls(
            matches=max(0, int(raw.get("matches", 0))),
            contradictions=max(0, int(raw.get("contradictions", 0))),
            on_matches=max(0, int(raw.get("on_matches", 0))),
            off_matches=max(0, int(raw.get("off_matches", 0))),
            ratio_sum=float(raw.get("ratio_sum", 0.0)),
            ratio_count=max(0, int(raw.get("ratio_count", 0))),
        )

    def to_dict(self) -> dict[str, int | float]:
        """Return JSON-compatible persisted data."""
        return {
            "matches": self.matches,
            "contradictions": self.contradictions,
            "on_matches": self.on_matches,
            "off_matches": self.off_matches,
            "ratio_sum": round(self.ratio_sum, 6),
            "ratio_count": self.ratio_count,
        }


@dataclass(slots=True)
class Relationship:
    """Evidence that one metered device is electrically upstream of another."""

    parent_device_id: str
    child_device_id: str
    parent_entity_id: str
    child_entity_id: str
    parent_name: str
    child_name: str
    daily: dict[str, DailyEvidence] = field(default_factory=dict)
    current_streak: int = 0
    best_streak: int = 0
    last_observed: str | None = None
    direct: bool = True

    @property
    def key(self) -> str:
        """Stable relationship key that survives entity renames."""
        return f"{self.parent_device_id}|{self.child_device_id}"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Relationship | None":
        """Load a relationship, returning None for unusable data."""
        parent_device_id = str(raw.get("parent_device_id", "")).strip()
        child_device_id = str(raw.get("child_device_id", "")).strip()
        if not parent_device_id or not child_device_id or parent_device_id == child_device_id:
            return None

        daily_raw = raw.get("daily", {})
        daily: dict[str, DailyEvidence] = {}
        if isinstance(daily_raw, dict):
            for day_key, day_value in daily_raw.items():
                if isinstance(day_key, str) and isinstance(day_value, dict):
                    daily[day_key] = DailyEvidence.from_dict(day_value)

        return cls(
            parent_device_id=parent_device_id,
            child_device_id=child_device_id,
            parent_entity_id=str(raw.get("parent_entity_id", "")),
            child_entity_id=str(raw.get("child_entity_id", "")),
            parent_name=str(raw.get("parent_name", parent_device_id)),
            child_name=str(raw.get("child_name", child_device_id)),
            daily=daily,
            current_streak=max(0, int(raw.get("current_streak", 0))),
            best_streak=max(0, int(raw.get("best_streak", 0))),
            last_observed=(
                str(raw["last_observed"]) if raw.get("last_observed") is not None else None
            ),
            direct=bool(raw.get("direct", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible persisted relationship data."""
        return {
            "parent_device_id": self.parent_device_id,
            "child_device_id": self.child_device_id,
            "parent_entity_id": self.parent_entity_id,
            "child_entity_id": self.child_entity_id,
            "parent_name": self.parent_name,
            "child_name": self.child_name,
            "daily": {key: value.to_dict() for key, value in sorted(self.daily.items())},
            "current_streak": self.current_streak,
            "best_streak": self.best_streak,
            "last_observed": self.last_observed,
            "direct": self.direct,
        }

    def prune(self, today: date) -> None:
        """Keep at most RETENTION_DAYS calendar-day buckets, including today."""
        cutoff = today - timedelta(days=RETENTION_DAYS - 1)
        for day_key in list(self.daily):
            try:
                evidence_day = date.fromisoformat(day_key)
            except ValueError:
                del self.daily[day_key]
                continue
            if evidence_day < cutoff or evidence_day > today:
                del self.daily[day_key]

    def totals(self) -> tuple[int, int, int, int, float, int]:
        """Return aggregate counters over the retained window."""
        matches = sum(day.matches for day in self.daily.values())
        contradictions = sum(day.contradictions for day in self.daily.values())
        on_matches = sum(day.on_matches for day in self.daily.values())
        off_matches = sum(day.off_matches for day in self.daily.values())
        ratio_sum = sum(day.ratio_sum for day in self.daily.values())
        ratio_count = sum(day.ratio_count for day in self.daily.values())
        return matches, contradictions, on_matches, off_matches, ratio_sum, ratio_count

    @property
    def matches(self) -> int:
        """Number of supporting observations."""
        return self.totals()[0]

    @property
    def contradictions(self) -> int:
        """Number of contradictory observations."""
        return self.totals()[1]

    @property
    def factor(self) -> float | None:
        """Learned upstream/downstream wattage ratio."""
        _, _, _, _, ratio_sum, ratio_count = self.totals()
        if ratio_count <= 0:
            return None
        return ratio_sum / ratio_count

    @property
    def support(self) -> float:
        """Supporting fraction among observations that could be evaluated."""
        total = self.matches + self.contradictions
        if total <= 0:
            return 0.0
        return self.matches / total

    @property
    def status(self) -> str:
        """Conservative promotion state."""
        matches, _, on_matches, off_matches, _, _ = self.totals()
        support = self.support

        if matches >= 8 and support >= 0.90 and on_matches > 0 and off_matches > 0:
            return STATUS_CONFIRMED
        if matches >= 5 and support >= 0.85:
            return STATUS_STRONG
        if matches >= 3 and support >= 0.75 and self.best_streak >= 3:
            return STATUS_SUSPECTED
        return STATUS_LEARNING

    @property
    def confidence(self) -> float:
        """Bounded confidence score combining support and evidence volume."""
        evidence_factor = min(1.0, self.matches / 8.0)
        return self.support * evidence_factor

    def ratio_is_consistent(self, ratio: float) -> bool:
        """Return whether a ratio agrees with an established candidate."""
        if not EXISTING_RELATION_MIN_RATIO <= ratio <= EXISTING_RELATION_MAX_RATIO:
            return False

        factor = self.factor
        if factor is None or self.matches < 3:
            return True
        if factor <= 0:
            return False
        return abs(ratio - factor) / factor <= EXISTING_RELATION_TOLERANCE

    def summary(self) -> dict[str, Any]:
        """Return compact state-attribute data."""
        return {
            "parent": self.parent_name,
            "child": self.child_name,
            "parent_entity_id": self.parent_entity_id,
            "child_entity_id": self.child_entity_id,
            "status": self.status,
            "confidence_percent": round(self.confidence * 100),
            "support_percent": round(self.support * 100),
            "matches": self.matches,
            "contradictions": self.contradictions,
            "factor": round(self.factor, 3) if self.factor is not None else None,
            "direct": self.direct,
            "last_observed": self.last_observed,
        }


class EvidenceBook:
    """Bounded collection of candidate relationships."""

    def __init__(self, relationships: dict[str, Relationship] | None = None) -> None:
        self.relationships: dict[str, Relationship] = relationships or {}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "EvidenceBook":
        """Load persisted evidence defensively."""
        relationships: dict[str, Relationship] = {}
        if not isinstance(raw, dict):
            return cls()

        candidates = raw.get("relationships", {})
        if not isinstance(candidates, dict):
            return cls()

        for value in candidates.values():
            if not isinstance(value, dict):
                continue
            relation = Relationship.from_dict(value)
            if relation is not None:
                relationships[relation.key] = relation
        return cls(relationships)

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible persisted evidence."""
        return {
            "relationships": {
                key: relation.to_dict()
                for key, relation in sorted(self.relationships.items())
            }
        }

    def prune(self, today: date) -> None:
        """Apply the hard retention limit and drop empty candidates."""
        for key, relation in list(self.relationships.items()):
            relation.prune(today)
            if not relation.daily:
                del self.relationships[key]
        self._cap()
        self.recompute_directness()

    def get(self, parent_device_id: str, child_device_id: str) -> Relationship | None:
        """Return a relationship by stable device IDs."""
        return self.relationships.get(f"{parent_device_id}|{child_device_id}")

    def relations_for_child(self, child_device_id: str) -> list[Relationship]:
        """Return candidates already known for a child device."""
        return [
            relation
            for relation in self.relationships.values()
            if relation.child_device_id == child_device_id
        ]

    def record_match(
        self,
        *,
        day: date,
        observed_at: str,
        direction: str,
        ratio: float,
        parent_device_id: str,
        child_device_id: str,
        parent_entity_id: str,
        child_entity_id: str,
        parent_name: str,
        child_name: str,
    ) -> Relationship:
        """Add supporting evidence, creating the relationship if needed."""
        key = f"{parent_device_id}|{child_device_id}"
        relation = self.relationships.get(key)
        if relation is None:
            relation = Relationship(
                parent_device_id=parent_device_id,
                child_device_id=child_device_id,
                parent_entity_id=parent_entity_id,
                child_entity_id=child_entity_id,
                parent_name=parent_name,
                child_name=child_name,
            )
            self.relationships[key] = relation
        else:
            relation.parent_entity_id = parent_entity_id
            relation.child_entity_id = child_entity_id
            relation.parent_name = parent_name
            relation.child_name = child_name

        bucket = relation.daily.setdefault(day.isoformat(), DailyEvidence())
        bucket.matches += 1
        bucket.ratio_sum += ratio
        bucket.ratio_count += 1
        if direction == "on":
            bucket.on_matches += 1
        elif direction == "off":
            bucket.off_matches += 1

        relation.current_streak += 1
        relation.best_streak = max(relation.best_streak, relation.current_streak)
        relation.last_observed = observed_at
        self._cap()
        self.recompute_directness()
        return relation

    def record_contradiction(
        self,
        *,
        day: date,
        observed_at: str,
        relation: Relationship,
    ) -> None:
        """Add contradictory evidence to an existing relationship."""
        bucket = relation.daily.setdefault(day.isoformat(), DailyEvidence())
        bucket.contradictions += 1
        relation.current_streak = 0
        relation.last_observed = observed_at
        self.recompute_directness()

    def recompute_directness(self) -> None:
        """Mark confirmed transitive ancestors as non-direct."""
        confirmed = [
            relation
            for relation in self.relationships.values()
            if relation.status == STATUS_CONFIRMED
        ]
        for relation in self.relationships.values():
            relation.direct = True

        confirmed_pairs = {
            (relation.parent_device_id, relation.child_device_id) for relation in confirmed
        }
        for relation in confirmed:
            parent = relation.parent_device_id
            child = relation.child_device_id
            intermediates = {
                right
                for left, right in confirmed_pairs
                if left == parent and right != child
            }
            if any((intermediate, child) in confirmed_pairs for intermediate in intermediates):
                relation.direct = False

    def overall_status(self) -> str:
        """Return the strongest current relationship state."""
        if not self.relationships:
            return STATUS_LEARNING
        return max(
            (relation.status for relation in self.relationships.values()),
            key=lambda status: STATUS_ORDER[status],
        )

    def counts(self) -> dict[str, int]:
        """Count relationships by promotion state."""
        counts = {
            STATUS_LEARNING: 0,
            STATUS_SUSPECTED: 0,
            STATUS_STRONG: 0,
            STATUS_CONFIRMED: 0,
        }
        for relation in self.relationships.values():
            counts[relation.status] += 1
        return counts

    def top_relationships(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return strongest non-learning relationships for sensor attributes."""
        ranked = sorted(
            self.relationships.values(),
            key=lambda relation: (
                STATUS_ORDER[relation.status],
                relation.matches,
                relation.support,
                relation.last_observed or "",
            ),
            reverse=True,
        )
        return [
            relation.summary()
            for relation in ranked
            if relation.status != STATUS_LEARNING
        ][:limit]

    def _cap(self) -> None:
        """Keep the evidence file bounded even in a noisy installation."""
        if len(self.relationships) <= MAX_RELATIONSHIPS:
            return

        ranked = sorted(
            self.relationships.items(),
            key=lambda item: (
                STATUS_ORDER[item[1].status],
                item[1].matches,
                item[1].support,
                item[1].last_observed or "",
            ),
            reverse=True,
        )
        self.relationships = dict(ranked[:MAX_RELATIONSHIPS])
