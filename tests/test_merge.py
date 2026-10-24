"""The trackers refuse answers that lose, repeat or invent a sighting, and apply good ones without touching the old inventory."""

import pytest

from app.merge import Contents, Described, Library, Link, ObjectUpdate, Order, Placed, Rejected, Seen, Update


def update(placed: dict[str, str], shelves: dict[str, list[str]], **rest: object) -> Update:
    """A tracker answer placing sightings on books and ordering shelves."""
    frames: dict[int, list[str]] = {}
    for s, b in placed.items():
        n, i = int(s.split(".b")[0]), int(s.split(".b")[1])
        row = frames.setdefault(n, [])
        row += [""] * (i + 1 - len(row))
        row[i] = b
    return Update(
        frames=[Seen(frame=n, books=v) for n, v in frames.items()],
        rejected=rest.get("rejected", []),
        shelves=[Order(unit="U1", shelf=k, books=v) for k, v in shelves.items()],
        units=[],
        best=rest.get("best", []),
        links=rest.get("links", []),
        unrelated=rest.get("unrelated", []),
    )


def first() -> Library:
    """An inventory with three books on one shelf."""
    lib, _ = Library().applied(
        update({"0.b0": "new1", "0.b1": "new2", "0.b2": "new3"}, {"S1": ["new1", "new2", "new3"]}),
        {"0.b0", "0.b1", "0.b2"},
        set(),
    )
    return lib


def test_new_books_get_ids_and_positions() -> None:
    """New books are numbered and placed left to right."""
    lib = first()
    assert [(b.id, b.shelf, b.position, b.sightings) for b in lib.books] == [
        ("B1", "S1", 1, ["0.b0"]),
        ("B2", "S1", 2, ["0.b1"]),
        ("B3", "S1", 3, ["0.b2"]),
    ]


def test_a_second_frame_adds_sightings_and_inserts_a_book() -> None:
    """A sighting of a logged book joins it; a new book between two logged ones shifts the positions."""
    lib, created = first().applied(
        update({"1.b0": "B2", "1.b1": "new9", "1.b2": "B3"}, {"S1": ["B1", "B2", "new9", "B3"]}),
        {"1.b0", "1.b1", "1.b2"},
        set(),
    )
    assert [b.id for b in created] == ["B4"]
    assert {b.id: b.position for b in lib.books} == {"B1": 1, "B2": 2, "B4": 3, "B3": 4}
    assert lib.books[1].sightings == ["0.b1", "1.b0"]


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (update({"1.b0": "B1"}, {}), "left unplaced"),
        (update({"1.b0": "B1", "1.b1": "B1", "9.b9": "B1"}, {}), "not new"),
        (update({"1.b0": "new1", "1.b1": "B2"}, {"S1": ["B1", "B2", "B3"]}), "missing from the shelf orders"),
        (update({"1.b0": "B1", "1.b1": "B2"}, {"S1": ["B1", "B2"]}), "leave out logged books"),
        (update({"1.b0": "B1", "1.b1": "B2"}, {"S1": ["B1", "B2", "B3"], "S2": ["B1"]}), "two shelves"),
        (update({"1.b0": "B1", "1.b1": "B2"}, {}, best=[Placed(sighting="1.b1", book="B1")]), "not one of book"),
        (
            update({"1.b0": "B1", "1.b1": "B2"}, {}, links=[Link(remark="R9", books=["B1"], exclude=True)]),
            "unknown remarks",
        ),
    ],
)
def test_bad_answers_are_refused_and_change_nothing(answer: Update, message: str) -> None:
    """Every refused answer leaves the inventory exactly as it was."""
    lib = first()
    before = lib.model_dump()
    with pytest.raises(ValueError, match=message):
        lib.applied(answer, {"1.b0", "1.b1"}, set())
    assert lib.model_dump() == before


def test_a_remark_can_exclude_a_shelf() -> None:
    """A remark linked with exclude marks every book it names."""
    lib, _ = first().applied(update({}, {}, links=[Link(remark="R1", books=["B1", "B2"], exclude=True)]), set(), {"R1"})
    assert [b.excluded_by for b in lib.books] == ["R1", "R1", ""]
    assert lib.remarks == ["R1"]


def test_rejected_sightings_count_as_placed() -> None:
    """A sighting the tracker rejects with a reason is accounted for."""
    lib, created = first().applied(
        update({"1.b0": "B1"}, {}, rejected=[Rejected(sighting="1.b1", reason="a box file")]), {"1.b0", "1.b1"}, set()
    )
    assert not created and lib.rejected[0].reason == "a box file"


def test_objects_need_a_description() -> None:
    """A new object must come with what it is, and gets an id."""
    bare = ObjectUpdate(
        placed=[Placed(sighting="0.t0", book="new1")], rejected=[], new_objects=[], revised=[], best=[], links=[]
    )
    with pytest.raises(ValueError, match="without an entry"):
        Contents().applied(bare, {"0.t0"}, set())
    good = bare.model_copy(
        update={
            "new_objects": [Described(id="new1", category="lighting", description="brass floor lamp", brand_model="")]
        }
    )
    lib, created = Contents().applied(good, {"0.t0"}, set())
    assert [(o.id, o.category, o.best) for o in lib.objects] == [("I1", "lighting", "0.t0")]
    assert created[0].id == "I1"


def test_a_remark_about_books_not_seen_yet_stays_open() -> None:
    """A remark the tracker neither links nor calls unrelated is offered again; an unrelated one is closed."""
    lib, _ = first().applied(update({}, {}), set(), {"R1"})
    assert lib.remarks == []
    lib, _ = lib.applied(update({}, {}, unrelated=["R1"]), set(), {"R1"})
    assert lib.remarks == ["R1"]
