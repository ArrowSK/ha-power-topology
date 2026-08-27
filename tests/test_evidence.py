"""Unit tests for the pure evidence model."""

from __future__ import annotations

from datetime import date, timedelta
import importlib.util
from pathlib import Path
import sys
import types


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "power_topology"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(ROOT / "custom_components")]
sys.modules.setdefault("custom_components", custom_components)

package = types.ModuleType("custom_components.power_topology")
package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("custom_components.power_topology", package)

_load_module("custom_components.power_topology.const", COMPONENT / "const.py")
evidence = _load_module(
    "custom_components.power_topology.evidence",
    COMPONENT / "evidence.py",
)

EvidenceBook = evidence.EvidenceBook


def _match(
    book: EvidenceBook,
    *,
    day: date,
    parent: str = "parent",
    child: str = "child",
    direction: str = "on",
    ratio: float = 1.05,
) -> None:
    book.record_match(
        day=day,
        observed_at=f"{day.isoformat()}T12:00:00+00:00",
        direction=direction,
        ratio=ratio,
        parent_device_id=parent,
        child_device_id=child,
        parent_entity_id=f"sensor.{parent}_power",
        child_entity_id=f"sensor.{child}_power",
        parent_name=parent.title(),
        child_name=child.title(),
    )


def test_retention_never_exceeds_100_calendar_days() -> None:
    book = EvidenceBook()
    start = date(2026, 1, 1)

    for offset in range(150):
        _match(book, day=start + timedelta(days=offset))

    today = start + timedelta(days=149)
    book.prune(today)

    relation = book.get("parent", "child")
    assert relation is not None
    assert len(relation.daily) == 100
    assert min(relation.daily) == (today - timedelta(days=99)).isoformat()
    assert max(relation.daily) == today.isoformat()


def test_confirmation_requires_bidirectional_evidence() -> None:
    book = EvidenceBook()
    day = date(2026, 8, 27)

    for _ in range(3):
        _match(book, day=day, direction="on")

    relation = book.get("parent", "child")
    assert relation is not None
    assert relation.status == "suspected"

    for _ in range(2):
        _match(book, day=day, direction="on")
    assert relation.status == "strong"

    for _ in range(3):
        _match(book, day=day, direction="on")
    assert relation.status == "strong"

    _match(book, day=day, direction="off")
    assert relation.status == "confirmed"


def test_contradictions_prevent_false_confirmation() -> None:
    book = EvidenceBook()
    day = date(2026, 8, 27)

    for _ in range(7):
        _match(book, day=day, direction="on")
    _match(book, day=day, direction="off")

    relation = book.get("parent", "child")
    assert relation is not None
    assert relation.status == "confirmed"

    for index in range(2):
        book.record_contradiction(
            day=day,
            observed_at=f"2026-08-27T12:0{index}:00+00:00",
            relation=relation,
        )

    assert relation.support == 0.8
    assert relation.status == "suspected"


def test_confirmed_transitive_ancestor_is_not_direct() -> None:
    book = EvidenceBook()
    day = date(2026, 8, 27)

    for parent, child in (("a", "b"), ("b", "c"), ("a", "c")):
        for _ in range(7):
            _match(book, day=day, parent=parent, child=child, direction="on")
        _match(book, day=day, parent=parent, child=child, direction="off")

    book.recompute_directness()

    assert book.get("a", "b").direct is True
    assert book.get("b", "c").direct is True
    assert book.get("a", "c").direct is False


def test_relationship_count_is_hard_capped() -> None:
    book = EvidenceBook()
    day = date(2026, 8, 27)

    for index in range(250):
        _match(book, day=day, parent=f"p{index}", child=f"c{index}")

    assert len(book.relationships) == 200
