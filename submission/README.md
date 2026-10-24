# submission

The submitted claim packet, its report, the video and the ground truth it is scored against.

| Path | What it is |
|---|---|
| `claim_packet.json` | The packet: books, other contents, room, totals, review queue, second market, cost and time per stage, assumptions |
| `report.html` | The same packet as a readable page; open it in a browser |
| `crops/` | The picture of each line shown in the report |
| `frames/` | The 30 frames the pipeline saw, linked from each line |
| `walkthrough.mp4` | 88 s: the sweep page, a frame, then the report section by section |
| `ground_truth/books.csv` | Hand count and reading of every spine in the photo (63 books, 56 legible) |
| `ground_truth/photo.jpg` | The shelf photo: "IKEA Billy bookshelf (80x106 cm birch veneer)" by Magnus Bäck, Wikimedia Commons, public domain |

## How it was made

No phone sweep of a real room was recorded: the author had no home library to film. The packet comes from replaying the public shelf photo through the full pipeline as a pan of 30 frames (`app/replay.py`). There was no floor sheet, so the room fields are empty and the packet says why.

## Score

| Bar (from the brief) | Result |
|---|---|
| Book count within 5% | 63 of 63 |
| Titles identified, at least 70% of legible spines | 95.8% |
| Confidently wrong, at most 3% | 0 of 56 (one-sided 95% upper bound 5.2%) |
| Packet within 5 minutes | The deadline packet, yes; the final packet took 707 s |
| Prices within 25%, spines within 15%, room within 10% and 15% | Not scored: no hand-checked prices, tape measurements or room for a public photo |
