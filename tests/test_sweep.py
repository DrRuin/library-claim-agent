"""Work started for a book before it was identified afresh never overwrites the newer result."""

import asyncio
from pathlib import Path

import pytest

from app import identify
from app.config import Locale
from app.identify import Identified, Identity
from app.merge import Group, Library
from app.sweep import Sweep

LOCALE = Locale(country="India", currency="INR", appraisal_limit=100_000)


def verdict(title: str) -> Identity:
    """An unidentified verdict that carries a marker in its evidence."""
    return Identity(
        verdict="unidentified",
        title="",
        author="",
        publisher="",
        edition="",
        isbn="",
        confidence=0,
        evidence=title,
        catalogue_url="",
        appraisal_reason="",
    )


def test_a_stale_identification_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first identification answers last, after a remark started a second one, and is ignored."""
    run = Sweep(tmp_path / "run", LOCALE)
    book = Group(id="B1", unit="U1", shelf="S1", position=1, sightings=[], best="", notes=[], excluded_by="")
    run.library = Library(books=[book])
    delays = iter([0.2, 0.01])

    async def slow(*args: object) -> Identified:
        """An identification that takes its turn's delay and says which call it was."""
        wait = next(delays)
        await asyncio.sleep(wait)
        return Identified(group=book, identity=verdict(f"took {wait}"), flags=[])

    monkeypatch.setattr(identify, "identify", slow)

    async def go() -> None:
        """Start, restart, and wait for both."""
        run.start("B1")
        await asyncio.sleep(0)
        run.start("B1")
        await run.settle()

    asyncio.run(go())
    assert run.identified["B1"].identity.evidence == "took 0.01"
