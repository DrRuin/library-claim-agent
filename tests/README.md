# tests

Run with `uv run pytest`. No test calls Gemini; model calls are replaced by fakes.

| File | What it guards |
|---|---|
| `test_packet.py` | The packet contract: totals add up from priced lines only, edited totals are refused, missing frames are reported |
| `test_claim.py` | Building the packet: lines and totals, a reason for every review line, every frame link opens, the deadline packet then the final one |
| `test_merge.py` | Tracking: new books get ids and shelf positions, a second frame adds sightings, bad answers change nothing, remarks exclude books or stay open until the books are seen |
| `test_measure.py` | Sizes from the A4 sheet and catalogue heights, flat books, cut-off sightings, items only in the shelf plane |
| `test_geometry.py`, `test_room.py` | Room geometry: homography, focal length, pose, frame alignment, floor and wall areas |
| `test_identify.py` | A book is confirmed by a page the search used, or stays unidentified |
| `test_live.py` | The live bridge: billing, reconnects, the packet reaching a reopened session, camera advice timing |
| `test_sweep.py` | A stale identification result is dropped |
| `test_evaluate.py` | Scoring: same work, different work, unpaired and unidentified lines, a skipped shelf, one-to-one judge answers, an L-shaped room |
| `test_llm.py` | Each model call is sent once; no request is sent past the budget |
