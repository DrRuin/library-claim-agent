"""Render the readable report from the claim packet alone."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from app.config import settings
from app.packet import Packet, Replacement, Review, Used

ACTIONS = [
    (
        "identify",
        "Identify by eye",
        "The system could not name these with evidence. Look at the picture and name the book, or leave it unidentified.",
    ),
    (
        "price",
        "Check the price",
        "No price was confirmed on a seller's page, or the page showed another edition or a sold-out copy. Open the link or find a seller.",
    ),
    (
        "appraise",
        "Send for appraisal",
        "Rare, signed, old, art or above the appraisal limit. These are not priced automatically.",
    ),
    (
        "measure",
        "Measure by hand",
        "No object of known size was in the same frame, so the size could not be worked out.",
    ),
    ("system", "System problems", "A step failed during the run. Check these lines by hand."),
]


def render(packet: Packet) -> str:
    """The packet as one HTML page."""
    env = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=select_autoescape())
    currency = packet.sweep.currency

    def money(amount: float | None, unit: str = currency) -> str:
        """An amount with its currency, or a dash when unknown."""
        return "-" if amount is None else f"{amount:,.2f} {unit}"

    def offer(figure: Replacement | Used, unit: str = currency) -> Markup | str:
        """A priced figure with a link to its page."""
        if figure.amount is None:
            return "-"
        mark = " (converted)" if figure.converted else ""
        return Markup('<a href="{}">{}</a>{}').format(figure.url, money(figure.amount, unit), mark)

    def area(m2: float | None, ft2: float | None) -> str:
        """An area in square metres and feet."""
        if m2 is None:
            return "not measured"
        return f"{m2:.1f} m² ({ft2:.0f} ft²)" if ft2 else f"{m2:.1f} m²"

    def metres(value: float | None) -> str:
        """A length in metres."""
        return "not measured" if value is None else f"{value:.2f} m"

    ready = (
        f"{(packet.packet_at - packet.sweep_ended_at).total_seconds():.0f} s"
        if packet.packet_at and packet.sweep_ended_at
        else "an unrecorded time"
    )
    lines = {line.id: line for line in [*packet.books, *packet.items]}
    reviews: dict[str, list[Review]] = {}
    for r in packet.review_queue:
        reviews.setdefault(r.ref_id, []).append(r)
    groups = []
    for action, heading, why in ACTIONS:
        rows = [
            (ref, lines.get(ref), reasons)
            for ref, rs in reviews.items()
            if (reasons := [r.reason for r in rs if r.action == action])
        ]
        if rows:
            groups.append((heading, why, rows))
    template = env.get_template("report.html")
    return template.render(
        p=packet,
        reviews=reviews,
        groups=groups,
        ready=ready,
        money=money,
        offer=offer,
        area=area,
        metres=metres,
        titles={b.id: b.title for b in packet.books},
        second_country=settings().second_locale.country,
    )
