"""The frame model judges each captured frame at once, so the live agent can ask for a better one while the policyholder is still there."""

import io
from typing import Literal

from PIL import Image
from pydantic import BaseModel, Field

from app.config import settings
from app.llm import ask, image

PROMPT = """<task>You check one frame from a phone sweep of a home library for an insurance inventory, the moment it is taken.</task>
<judge>
- Say whether the book spines in this frame are sharp and large enough for a person to read their titles.
- List every capture problem you see. Report only what the frame shows.
- cut_off: the top or bottom edge of the frame cuts through spines, so titles run out of it. Books cut at the left or right edge are normal in a pan, since the next frame covers them; do not report those.
- no_spines: the frame shows no book spines (a wall, the floor, furniture or an open book). That is fine while the policyholder films the rest of the room.
- Give one short instruction the agent can speak to fix the worst problem, or leave it empty when the frame is good.
- Describe in a few words what the frame shows, so the agent knows where the policyholder is pointing.
</judge>"""


class View(BaseModel):
    """What the frame model sees in one frame."""

    readable: bool = Field(description="Whether a person could read the spine titles in this frame.")
    problems: list[Literal["blur", "glare", "too_far", "too_dark", "occluded", "cut_off", "no_spines"]]
    advice: str = Field(description="One short spoken instruction, or empty.")
    shows: str = Field(description="What the frame shows, in a few words.")


class Check(BaseModel):
    """The frame model's view of one frame."""

    width: int
    height: int
    view: View


async def check(data: bytes) -> Check:
    """Ask the frame model about one frame."""
    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
    view = await ask(View, [image(data), PROMPT], settings().frame_model)
    return Check(width=width, height=height, view=view)
