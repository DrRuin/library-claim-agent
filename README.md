# Library claim agent

A live voice-and-vision agent on a phone that inventories and values a home library in one camera sweep and writes a contents claim packet: `claim_packet.json`, a readable `report.html` and a review queue. The policyholder talks to a Gemini Live agent while the page sends full-resolution frames once a second; background agents read spines, count and track books across frames, identify and price each book, list and price the other objects, and measure the room from an A4 sheet on the floor. Every judgement is a Gemini call with a Pydantic schema. Code does arithmetic (totals, currency, geometry), I/O and the rules the brief gives to code.

## Run

Needs [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 from `.python-version`) and a Gemini API key. A phone needs HTTPS for the camera, which [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) gives without an account.

```bash
uv sync
cp .env.example .env            # set GEMINI_API_KEY
uv run pytest
uv run uvicorn app.main:app --port 8300
```

Open `http://127.0.0.1:8300` on a laptop, or on a phone through HTTPS:

```bash
cloudflared tunnel --url http://127.0.0.1:8300
```

Press **Start sweep**, allow camera and microphone, and follow the agent. Put a sheet of A4 paper flat on the floor and keep it in view while filming the room's corners, doors and bookcases; put a second sheet on a shelf for spine sizes. Say "I'm done" or press **I'm done**. The packet and report land in `runs/sweep-<time>/`, linked from the page.

Replay saved photos through the same pipeline, without the phone:

```bash
uv run python -m app.replay submission/ground_truth/photo.jpg --frames 12 --overlap 0.75
uv run python -m app.evaluate --packet runs/<id>/claim_packet.json --truth submission/ground_truth --out results/<id>/
```

## Configure

Settings are class defaults in `app/config.py`, overridden by environment variables or `.env`.

| Setting | Default | What it is |
|---|---|---|
| `GEMINI_API_KEY` | none | Google AI key for every model call |
| `READER_MODEL`, `JUDGE_MODEL`, `GEOMETRY_MODEL`, `PRICE_MODEL` | gemini-4-argon | Spines and objects; tracking, identifying and room plans; room pointing; price search |
| `FRAME_MODEL`, `CHECK_MODEL` | gemini-3.8-flash | Camera check per frame; rereading each price and catalogue page |
| `LIVE_MODEL`, `LIVE_VOICE` | gemini-3.8-live, Kore | The voice agent |
| `LOCALE` | India, INR, 100000 | Claim market and appraisal limit (JSON) |
| `SECOND_LOCALE`, `SECOND_SAMPLE` | United Kingdom, GBP, 1000; 10 | Market the sample of books is priced again in |
| `REFERENCE_MM` | [210, 297] | Reference sheet size |
| `FRAME_SECONDS` | 1.0 | Interval between full-resolution frames |
| `CONCURRENCY` | 96 | Model calls in flight at once |
| `BUDGET_USD` | 5 | USD one server or replay process may spend at list prices; past it no request is sent and the lines go to review. Set a spend cap in AI Studio as well: list prices undercount the bill |
| `PACKET_DUE_S` | 280 | Seconds after the sweep when a packet is written from the finished work; the final packet follows |
| `IDENTIFY_CROPS` | 4 | Spine crops of one book sent to identification, clearest first |
| `LOG_LEVEL` | INFO | Level of the app's log lines on the server |
| `LIVE_RETRIES`, `LIVE_RETRY_S`, `LIVE_WAIT_S` | 5, 2, 30 | Reconnects after a failed live session, seconds between them, and seconds a packet read-back waits for a session |
| `SCORING__*` | brief | Pass bars; `SCORING__OPENING_MIN_M2` sets which openings come off wall area |

## How a sweep becomes a packet

| Stage | Module | Model | What it does |
|---|---|---|---|
| Talk | `app/live.py` | gemini-3.8-live | Greets, coaches the sweep, hears remarks ("those are my neighbour's"), reads back counts; tools `set_market`, `log_remark`, `inventory`, `finish_sweep` |
| Camera check | `app/frames.py` | Flash | Each frame: readable, blur, glare, too far, with advice the agent speaks |
| Read | `app/read.py` | Argon | Every spine (box, text, pose) and every non-book object, plus any A4 sheet and the surface it lies on |
| Track | `app/merge.py` | Argon | Places each sighting as a new book or a repeat of one already logged, in shelf order; same for objects; links remarks to lines |
| Identify and price | `app/identify.py`, `app/price.py` | Argon + search, Flash checker | One search resolves a book to a work and finds new and used offers in the claim market; Flash rereads the confirming page, and the other pages the search grounded on when that one falls short; one must show the title; it reads printed dimensions; Flash reads each price on its page; ECB rates convert |
| Second market, objects | `app/price.py` | Argon + search, Flash checker | A sample of books searched again in the second market; ranges for objects |
| Measure | `app/measure.py` | none | Spine and item sizes from the A4 sheet and catalogue heights in the same frame, leaving a book's own height out |
| Room | `app/room.py`, `app/geometry.py` | Argon | Points at sheet, corners, wall edges, openings and units; code fits the sheet homography, focal length and pose, aligns frames, and computes areas |
| Packet | `app/claim.py`, `app/report.py` | none | Lines, totals, review queue, exclusions, second market, cost and latency per stage |

Identification and pricing start while the sweep runs, as soon as one sighting shows a book's whole title or the pan leaves it. When `PACKET_DUE_S` passes with work still running, the packet is written from what is finished, every unfinished line goes to the review queue, and a copy stays as `claim_packet_at_deadline.json`; `claim_packet.json` and `report.html` are replaced by the final packet when the rest is done.

## Results

Measured on 2026-10-24 against hand-made truth for public shelf photos (`app.evaluate`); the shelf1 truth ships in `submission/ground_truth/`. Prices and spine sizes have no hand-checked truth for these public photos, so those bars are not scored here. Titles are scored over spines a person can read; books the policyholder excluded by voice count toward the book count and stay out of the title score.

| Run | Books | Count | Titles at the deadline | Titles, final | Confidently wrong | Final packet | Cost |
|---|---|---|---|---|---|---|---|
| Replay shelf2, 24 frames, retailer replacement rule, deadline 280 s | 57 | 57/57 | 80.0% | 93.3% | 0 | 815 s | 4.67 USD |
| Replay shelf1, 30 frames | 63 | 63/63 | 66.7% | 95.8% | 0 of 56 | 707 s | 9.71 USD |
| Live, headless, shelf2 (59 frames, spoken remarks, 3 books excluded by voice) | 57 | 57/57 | 86.7% | 86.7% | 0 | 502 s | 6.45 USD |
| Same, second run | 57 | 57/57 | 53.3% | 73.3% | 0 | 529 s | 4.85 USD |

- The 5-minute bar is met by the packet that falls due at `PACKET_DUE_S`; the other runs used 240 s, now 280 s. Grounded search takes 25 to 73 s a call even for a one-line question, so a final packet for 60 books took 8 to 14 minutes.
- Two live runs on the same frames gave 86.7% and 73.3%: the public photo gives frames 687 px tall, where one spine is about 75 px wide, and the reader and identifier differ run to run on such small text.
- Confidently wrong has wide intervals at this size: 0 of 56 identified lines still has a one-sided 95% upper bound of 5.2%.
- With the retailer rule (shelf2 replay), replacement prices came from Indian bookshops (BooksWagon, SapnaOnline, Flipkart and others) for 12 of 15 new prices, each stating the edition priced.
- Shelf1 replay prices: new for 32 and used for 45 of 56 identified books. Live shelf2: new for 16 and used for 24 of 25; the second market priced 10 books; 6 room items found, the framed print marked for appraisal.
- The live agent read back the first packet as provisional and the final packet with what changed, and costs 0.12 to 0.13 USD a sweep, billed from the tokens the Live API reports.
- Room on 20 rendered frames of a 6.0 by 4.0 by 2.5 m room with real Gemini pointing: floor 23.99 m2 for 24.0, length 6.09 m, width 4.17 m, height 2.28 m (from one wall edge), net walls 43.8 m2 for 47.1, door 0.91 by 1.97 m for 0.9 by 2.0.
- Cost by stage, shelf1 replay: identification 3.35 USD, price checks and searches 2.69, room items 1.87, reading 0.89, tracking 0.49.
- Costs are metered at list prices and undercount the bill: these runs sent hedged twin requests that Google billed and the meter missed. Each call is now sent once, and `BUDGET_USD` stops a process at its limit.
- `docs/failure-log.md` is the failure log (three worst errors, cost and latency per stage, two more weeks, build log); `docs/architecture.md` is the architecture note.

## Submission

`submission/` holds the claim packet, its report, an 88 s walkthrough video and the ground truth the packet is scored against; `submission/README.md` lists each file and the score. No phone sweep of a real room was recorded: the author had no home library to film, so the packet comes from replaying a public shelf photo and the room fields are empty.

## Models and tools

- Gemini only, through `google-genai`: `gemini-3.8-live` talks; `gemini-4-argon` reads spines and objects, tracks books across frames, identifies and prices with Google Search grounding, and points at the room; `gemini-3.8-flash` checks each frame and rereads every confirming page and price.
- Exchange rates: the ECB reference rates through Frankfurter. Pages are fetched with `httpx` and stripped with `selectolax`. FastAPI and uvicorn serve the page; Pydantic schemas bind every model call; numpy does the room geometry; Jinja2 renders the report.
- No model was trained or fine-tuned. Built with Claude Code (Anthropic) as the coding assistant.

## Assumptions

- One room per sweep. The policyholder lays one A4 sheet flat on the floor and keeps it in view while filming corners and walls; a second sheet on a shelf improves spine sizes. Without a floor sheet the room fields stay empty and the packet says why.
- Replacement cost is a new copy of the same work from a retailer selling new books in the claim country, the same format where the edition is known, else a current edition. Used value is a second-hand listing in the condition it states.
- A price from another market is converted at the ECB rate of its day and marked converted. A line with no checked price is left out of the totals and listed in the review queue.
- Anything rare, signed, antiquarian, a first edition, or priced at or above the appraisal limit (100,000 INR by default, carried over at the day's rate when the market changes) goes to appraisal unpriced. Art goes to appraisal unless the policyholder says it is a print.
- Wall area is perimeter times ceiling height, less openings of at least `SCORING__OPENING_MIN_M2`. Shelved wall area is the wall face the bookcases cover.
- Each packet lists the assumptions it ran with under `assumptions`.

## From the reference app

The reference is `voice_ai_agents/insurance_claim_live_agent_team` in awesome-llm-apps (commit 874bc13).

| Part | What happened |
|---|---|
| FastAPI WebSocket bridge to Gemini Live, key kept on the server | Kept |
| PCM16 at 16 kHz in, 24 kHz out, barge-in on `interrupted` | Kept |
| Structured-output recheck of what the live model claims | Kept: the live agent never writes the inventory; background stages do, from saved frames |
| Live config without compression or resumption | Changed: context compression and session resumption on, so a sweep outlives the 2-minute camera limit |
| 512 px frames at quality 0.6 | Changed: full-resolution frames for reading; a 1024 px copy goes to the live agent |
| Fixed-timer frames with no quality check | Changed: Flash judges every frame and the agent speaks the advice |
| `ScriptProcessorNode` audio | Changed: `AudioWorklet` |
| In-memory session dict | Changed: every stage output is saved under `runs/<sweep>/` next to its frames |
| Graph rerun on the whole transcript each turn | Changed: work runs per book and per object, so cost tracks the library, not the call length |
| Claim narrative, policy directory, fraud rules, document checklist | Thrown away |
| Incident sketch, avatar, notebook UI | Thrown away |
| Regex safety checks and substring field filling | Thrown away: every judgement is a structured model call |

## Layout

Each folder has a README with the detail.

| Path | What it holds |
|---|---|
| `app/` | The server and the pipeline, one module per stage |
| `web/` | The phone page |
| `tests/` | Tests that guard behaviour; none call Gemini |
| `docs/` | The architecture note and the failure log |
| `submission/` | The submitted packet, report, video and ground truth |
