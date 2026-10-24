# app

The server and the pipeline. Every judgement is a Gemini call bound to a Pydantic schema; code does arithmetic, I/O and the brief's rules.

## Modules

| Module | What it does | Model |
|---|---|---|
| `main.py` | FastAPI app: serves the phone page from `web/`, the `/ws/sweep` WebSocket, `/api/client` and the `runs/` folder | none |
| `config.py` | Every setting with its default, read from the environment or `.env` | none |
| `live.py` | Bridge between the phone and Gemini Live: audio both ways, frames, tool calls, reconnects, camera advice held until the agent's turn ends, the packet read back | gemini-3.8-live |
| `sweep.py` | One sweep: saves frames, runs the background stages as frames arrive, keeps the event log | none |
| `frames.py` | Judges each frame as it arrives: readable, blurred, glare, too far, a title cut at the top or bottom | gemini-3.8-flash |
| `read.py` | Reads each saved frame: every spine (box, text, upright or flat) and every non-book object, plus any A4 sheet | gemini-4-argon |
| `merge.py` | Tracks books and objects across frames in shelf order and links spoken remarks to lines | gemini-4-argon |
| `identify.py` | Resolves a book to a work with Google Search, then has Flash confirm it on the pages the search used | gemini-4-argon, gemini-3.8-flash |
| `price.py` | New and used offers in the claim market and a second market, each price reread on its page; ECB rates convert | gemini-4-argon, gemini-3.8-flash |
| `measure.py` | Spine and item sizes from the A4 sheet in the same frame | none |
| `room.py`, `geometry.py` | Room size from the floor sheet: the model points at corners, edges and openings, code fits the homography and pose and computes areas | gemini-4-argon |
| `claim.py` | Builds the packet: lines, totals, review queue, exclusions, second market, cost per stage; writes the deadline packet, then the final one | none |
| `packet.py` | The packet contract (Pydantic models for every field) | none |
| `report.py`, `report.html` | Renders the packet as a page a claims handler can read | none |
| `llm.py` | The one Gemini client: each call sent once, metered, and refused once the process reaches `BUDGET_USD` | none |
| `replay.py` | Feeds a saved photo through the pipeline as a pan of frames, without the phone | none |
| `evaluate.py`, `judge.py` | Scores a packet against ground truth on the brief's bars; the judge pairs packet titles with truth titles | gemini-4-argon |

## Commands

```bash
uv run uvicorn app.main:app --port 8300
uv run python -m app.replay submission/ground_truth/photo.jpg --frames 12 --overlap 0.75
uv run python -m app.evaluate --packet runs/<id>/claim_packet.json --truth submission/ground_truth --out results/<id>/
```

Each sweep or replay writes to `runs/<id>/`: frames, each stage's output, `claim_packet.json` and `report.html`.
