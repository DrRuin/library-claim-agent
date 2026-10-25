# Architecture

How a sweep becomes a claim packet, as built.

## Flow

```
phone page (web/sweep.js)
  mic PCM16 16 kHz ─┐        ┌─ agent voice 24 kHz
  1024 px frame ────┤        │
  full frame + heading ─┐    │
                        ▼    │
FastAPI WebSocket (app/main.py, app/live.py Bridge)
  ├─ Gemini Live session: talk, tools set_market / log_remark / inventory / finish_sweep
  └─ Sweep (app/sweep.py): one asyncio task per frame, per book, per object
       frame ─► frame check (Flash) ─► advice spoken by the agent
       frame ─► read (Argon): spines, objects, A4 sheet
             ─► track (Argon): new book or repeat, shelf order, objects too
             ─► book the pan has left ─► identify + offers (Argon + Google Search)
                                       ─► confirm page, check each price (code fetch, Flash reads)
                                       ─► second market for a sample of 10
             ─► object ─► price range (Argon + Google Search) ─► checks
       finish ─► room (Argon points, code fits, Argon plans) ─► packet, report, review queue
```

## Who decides what

- Models decide: is a frame usable and what to tell the user, where each spine, object and sheet corner is, whether two sightings are one book, which work a spine shows, whether a page confirms that work, what price a page prints, which room observations are the same corner or wall, and whether an object is art or sits in the shelf plane.
- Code decides: totals and exclusions, currency conversion at ECB rates, millimetres per pixel, floor homographies, poses, frame alignment, areas, the review queue's rules from the brief, cost and latency per stage.
- No price, identity or size enters the packet on the search model's word alone: a second model reads it on the page code fetched, or the line goes to review with the reason.

## Contracts

- `app/packet.py` is the brief's packet. `Totals` are recomputed from the lines and a packet whose totals disagree fails to load.
- Every model call has a Pydantic schema (`llm.ask`, `llm.search`, `llm.reread`).
- Every stage writes its output under `runs/<sweep>/` beside the frames: `frames/`, `checks/`, `readings/`, `events.jsonl`, `library.json`, `contents.json`, `identified.json`, `prices.json`, `item_prices.json`, `room.json`, `claim_packet.json` (and `claim_packet_at_deadline.json` when the deadline came first), `report.html`.
- Each packet line names its frames (`frame_ref`, `frame_refs`) and each price its source, URL, date and quote.

## Latency and cost

- Calls run under one concurrency limit and each is sent once. Hedged twin requests were removed on 2026-10-24: Google bills a cancelled copy, and the bill reached about 300 USD against 89 USD metered.
- `BUDGET_USD` stops the process sending requests once its metered spend reaches it, live session included.
- Each book costs one grounded search (identity and claim-market offers together) plus two or three Flash page reads.
- At `packet_due_s` after the sweep a packet is written from the finished work, with unfinished lines listed for review. The final packet follows.
- Cost per stage is list price times metered tokens plus grounded search queries. The live session is billed from the tokens the Live API reports per turn, priced per modality.

## Metric scale

- Spines: a known-size object in the same frame gives millimetres per pixel at the shelf: an A4 sheet on a shelf, else the catalogue heights printed on the confirming pages of other identified books in that frame. A book's own catalogue height never measures itself. Each size is the median over its sightings. A book with no scaled sighting keeps empty sizes and the reason.
- Items: the same frame scale, only for objects the reader places at the depth of the spines. Depth is not measured.
- Room: the A4 sheet on the floor (below).

## Prices

- One grounded Google Search per book finds the work and its new and used offers in the claim market. A missing side gets a second search, and 10 books are searched again in the second market.
- Code fetches every cited page and Flash reads the printed price, currency, stock and format on it. An offer whose page does not show it is dropped. A price that cannot be checked is kept only with a review reason.
- Prices in another currency are converted in code at the ECB rate of the day and marked converted. No price comes from a model's memory: every amount carries its source, URL, quote and retrieval time.

## Room

- The A4 sheet on the floor gives each frame a homography (DLT on its four corners, refined in a close-up). Focal agreement picks its labelling, and with a focal length the frame gets a camera pose.
- The sheet's half-turn ambiguity per frame is settled by the phone compass (Rayleigh test on doubled angles), else by the smoothest camera path (two-state Viterbi) refined by how tightly shared features meet.
- Argon groups corners, wall edges, openings and units across frames. Code gives floor area (shoelace), length and width (smallest rectangle on an edge), height (median of edge tops), wall areas gross and net of openings, and shelved wall area.
