"""Replay saved images through the live sweep code, as if a phone had sent them one frame a second."""

import argparse
import asyncio
import io
import logging
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from app.claim import close
from app.config import settings
from app.sweep import Sweep


def pan(path: Path, count: int, overlap: float) -> list[bytes]:
    """Frames cut along the photo's long side, each overlapping the previous by `overlap` of its length."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        across = im.width >= im.height
        side = (im.width if across else im.height) / (1 + (count - 1) * (1 - overlap))
        frames = []
        for k in range(count):
            start = round(k * side * (1 - overlap))
            end = round(start + side)
            box = (start, 0, end, im.height) if across else (0, start, im.width, end)
            out = io.BytesIO()
            im.crop(box).save(out, "JPEG", quality=92)
            frames.append(out.getvalue())
        return frames


async def replay(frames: list[bytes], run: Path, device: str) -> None:
    """Send frames to a sweep one frame interval apart, as a phone does, end the sweep at the last one, then write the packet."""
    sweep = Sweep(run, settings().locale, device)

    async def send(k: int, data: bytes) -> None:
        """Send one frame at its moment in the sweep."""
        await asyncio.sleep(k * settings().frame_seconds)
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
        if k == len(frames) - 1:
            sweep.ended = datetime.now(UTC)
        verdict = await sweep.frame(data, k * settings().frame_seconds, width, height)
        print(f"frame {k}: readable={verdict.view.readable} problems={verdict.view.problems}")

    await asyncio.gather(*(send(k, data) for k, data in enumerate(frames)))
    async for packet in close(sweep):
        seconds = (packet.packet_at - packet.sweep_ended_at).total_seconds()
        kind = "final packet" if packet.final else "packet at the deadline"
        print(packet.totals.model_dump_json(indent=1))
        print(
            f"{kind}: review {len(packet.review_queue)}, cost {packet.cost_usd:.3f} USD, {seconds:.0f} s after the sweep ended"
        )
    print(f"report {run / 'report.html'}")


def main() -> None:
    """Replay a folder of frames, or a simulated pan across one photo."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="a folder of JPEG frames, or one wide photo to pan across")
    parser.add_argument("--frames", type=int, default=6, help="frames to cut when panning one photo")
    parser.add_argument("--overlap", type=float, default=0.5, help="share of each panned frame the next one repeats")
    args = parser.parse_args()
    if args.source.is_dir():
        frames = [p.read_bytes() for p in sorted(args.source.glob("*.jpg"))]
    else:
        frames = pan(args.source, args.frames, args.overlap)
    run = settings().runs_dir / f"replay-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(replay(frames, run, f"replay of {args.source.name}"))


if __name__ == "__main__":
    main()
