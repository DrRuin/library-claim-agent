# Failure log

Runs on 2026-10-24: shelf1 replay (63 books, 30 frames) and two headless live sweeps of shelf2 (57 books, 59 frames). Replace these figures with the recorded sweep's once it exists.

## Three worst errors

1. Final packet 450 to 815 s after the sweep, over the 300 s bar. Root cause: a grounded Google Search call takes 25 to 73 s even for a one-line question, and each book needs one to three (identity with offers, a missing side, the second market). The tracker's last pass and price checks queue behind them. Change made: a packet falls due at `PACKET_DUE_S` (now 280 s) from the finished work, every unfinished line in review, and the final packet follows. At the deadline it held 53 to 87% of legible titles right (80% at 280 s on the shelf2 replay). Would change: try a plain web search API with Flash reading the result pages, and measure it against grounded search.
2. Right titles thrown out by one weak page. Root cause: confirmation read only the page the model named. A search listing, a 25-volume set or a page naming the editor cleared four correct titles in one live run. Fix: when the named page falls short, Flash reads every page the search grounded on, and an editor, compiler or a volume of a set counts. Measured on the shelf1 replay: titles 85.4% to 95.8%, still 0 of 56 confidently wrong.
3. Replacement prices from resellers. Root cause: the price rule accepted any listing marked new, so a Penguin paperback (Why Not Me?) got 6,108 INR from an AbeBooks marketplace seller against 158 INR used. In one live run 4 of 16 new prices were more than four times the used price. Fix: replacement must be a retailer's own new price in the claim market, a current edition when the format is out of print, and the packet states the condition priced. On the shelf2 replay 12 of 15 new prices then came from Indian bookshops, each naming the edition. Accuracy needs the 15 hand-checked prices.

Two live runs on the same frames gave 86.7% and 73.3% titles: the public photo gives frames 687 px tall, a spine about 75 px wide, and the reader and identifier differ run to run on text that small.

## Cost and latency

Cost per sweep: 9.71 USD for the 63-book replay, 6.45 and 4.85 USD for the 57-book live runs (live agent 0.12 to 0.13 USD).

These are metered figures and they undercount. The 17 test runs of 23 and 24 Oct metered 89 USD, while AI Studio showed INR 27,180 (about 305 USD) for the month: hedged twin requests were cancelled on our side but still billed, and calls that timed out were never metered. Hedging is gone, and `BUDGET_USD` stops a process at its limit.

| Stage (shelf1 replay) | Calls | Wall time | Done after sweep | USD |
|---|---|---|---|---|
| Frame check | 30 | during the sweep | 0 s | 0.07 |
| Read | 60 | 109 s | 85 s | 0.89 |
| Identify | 121 | 446 s | 567 s | 3.35 |
| Price (checks and searches) | 177 | 559 s | 707 s | 2.69 |
| Second market | 26 | 193 s | 341 s | 0.35 |
| Room items | 103 | 381 s | 497 s | 1.87 |
| Track | 9 | 721 s | 707 s | 0.49 |

Every packet's `stages` and the report's cost table give the same figures per sweep.

## With two more weeks

- A plain web search API plus Flash page reading, measured against grounded search, to bring the final packet under 300 s.
- Hand-checked prices for 15 books, then tune the replacement rule against them.
- A shelf of 200 books and a second room to measure count and room bars away from the test space.
- Barcode-free edition matching from spine imprint and size against catalogue dimensions.
- Condition grading per book feeding the used value.

## Build log

What broke during the build, how it showed, and the fix. Newest last.

| Date | Failure | How it showed | Fix |
|---|---|---|---|
| 2026-10-23 | Price offers refused as "not one the search opened" | shelf1 replay: 35 of 63 books with no checked price. The model cited retailer URLs, the grounding list held only `vertexaisearch` redirects | The membership test was dropped. A second model now rereads the cited page itself |
| 2026-10-24 | The page reader tool made grounded search slow | Argon search with `url_context`: 40 to 180 s a call and up to 535k tool-prompt tokens on Flash. Search alone: 12 to 33 s | Search runs without the page reader, and the checker reads one page per offer |
| 2026-10-24 | Gemini's page reader could not open most shops | Oxford Bookstore, Padhega India, Bookchor: "could not be accessed" | Pages are fetched over HTTP with browser Accept headers and stripped to text, JSON-LD and price meta tags. Flash reads the price from that text, and the page reader stays as the fallback |
| 2026-10-24 | Single slow calls held the whole packet | Calls hit the 180 s limit while their twins answered in 30 s | Hedged requests: a call slower than the 95th percentile of its kind gets a twin, the first good reply wins (`llm.first`) |
| 2026-10-24 | Stage outputs saved as Python reprs | `identified.json` held `"group=Group(id='B1', ...)"` strings | `Sweep.save` dumps through `pydantic_core.to_jsonable_python` |
| 2026-10-24 | Every live frame raised and was lost | `Bridge.frame` subtracted a datetime from the loop clock, and the error died inside a background task | Frame time comes from the sweep's start. Failed background tasks are logged and listed in the review queue |
| 2026-10-24 | Room frames disagreed on the floor frame | Real pointing on a rendered room gave 12.2 m2 for a 24 m2 room: the model listed the sheet's corners from different starts and directions | Code orders the corners anticlockwise and puts x along a short side, leaving a half-turn per frame, which the phone compass settles, or else the smoothest camera path plus shared features |
| 2026-10-24 | Quantised corner points | Points on the 0-1000 grid put a floor point 3 m from the sheet 11% off | The sheet's corners are pointed again in a close-up crop |
| 2026-10-24 | The live session's cost was missing | `live_usd_per_min` was never used | The bridge bills the live stage by the minute before the packet is built |
| 2026-10-24 | Item sizes were never filled | `dimensions_cm` stayed empty | Items in the shelf plane take the frame's scale, and the reader judges the plane |
| 2026-10-24 | Packet 684 s after the sweep, over the 300 s bar | shelf1 replay: identify, then price, then the second market ran as three searches in a row per book. Grounded search took 25 to 73 s even for a one-line question, and 18 books timed out at 180 s | One search identifies a book and finds its claim-market offers. The second market starts at identification, and calls with a tool get a twin at their median latency |
| 2026-10-24 | Queued calls timed out before they were sent | The 180 s limit and the hedge clock started before a call waited for a concurrency slot | Both start once the call holds its slot |
| 2026-10-24 | Packet quote disagreed with its amount | A line read 75.94 GBP with the quote "£ 76.33": the amount came from the checker, the quote from the search | The checker returns the price text it read and the packet quotes that |
| 2026-10-24 | Identification waited for the tracker | 30-frame paced replay: no book identified before 325 s. A book counts as left only when a batch adds no sighting, and at 1 frame a second each book stays in view for about 10 frames | A book is identified as soon as one sighting shows its whole title on a spine inside the frame, or when the pan leaves it |
| 2026-10-24 | The checker dropped usable prices and the search invented addresses | 14 new prices refused: sold-out pages with a printed price, another format of the same work. Item offers cited retailer homepages | The search cites the result address it read. The checker keeps a printed price of a sold-out copy or another format and flags it for review |
| 2026-10-24 | Price flags never reached the packet | `Priced.flags` (grounded fallback, conversion) were dropped when lines were built | Each price carries `notes`, and review-worthy flags go to the review queue |
| 2026-10-24 | One failed read froze the tracker | Three reads timed out at 180 s. The tracker waits at the first frame with no reading, so frames after 23 were never placed and the deadline packet had no books | A failed read is stored as an empty reading and listed in the review queue |
| 2026-10-24 | Tracker answers grew with every sighting | At 1 frame a second each book is in about 10 frames. Four tracker calls wrote 18.8k tokens and one took 163 s | The tracker answers with one book id per sighting per frame instead of a sighting and book pair |
| 2026-10-24 | Identification sent every crop of a book | 114 identify calls read 1.6M input tokens, up to 10 near-identical crops per book | The best sighting and the clearest others, up to `identify_crops` (4) |
| 2026-10-24 | A remark before the books were in view was lost | Live test: "the three books on the far left of the top shelf belong to my neighbour" came while the camera was on the miniatures. The tracker's next batch had no books, the remark was marked done and nothing was excluded | A remark stays open until a tracker links it or calls it unrelated |
| 2026-10-24 | The final packet died with the phone connection | The phone left after the first packet. Reading it back to the closed live session raised `ConnectionClosedOK` and stopped the packet loop | The session is cleared when the agent ends and a failed read-back is logged, so the final packet is still written |
| 2026-10-24 | Live frame checks named the wrong frame | Two checks reported frame 10: the bridge read the frame count after the check returned | The frame number is taken before the check |
| 2026-10-24 | A transient 400 lost a book | `400 INVALID_ARGUMENT` on one identify search. The same call succeeded when rerun | A failed call gets its twin at once |
| 2026-10-24 | Review: one failure could stop a stage | A raising listener, a failed frame check, a failed second price search or a late identify result could end a task or overwrite newer work | Listeners and each step are guarded. A stale identify or price result is dropped by turn number |
| 2026-10-24 | The live session was billed by audio minutes | 0.03 USD for a 13-minute session. The frames the live model sees were not counted | The bridge bills each turn from the tokens the Live API reports, priced per modality |
| 2026-10-24 | The agent read a first packet as final | Live test: the agent read the 240 s packet as the claim | The read-back is headed `[first packet]` or `[final packet]` and the prompt says how to read each |
| 2026-10-24 | A dropped live connection ended the agent silently | Found while tracing a missing read-back (that one was the test driver, which stopped reading 30 s early). A server close without `go_away` raises `APIError` from `receive`, which ended the agent task unlogged and left no session, so a later read-back went nowhere | The bridge reconnects with the resumption handle (`LIVE_RETRIES`), and a read-back waits up to `LIVE_WAIT_S` for an open session and retries a failed send |
| 2026-10-24 | Camera advice cut the agent off | Live test: the agent restarted its opening three times in 16 s. Each interruption came within 0.1 s of a `[camera]` message, and most were `cut_off` for books at the edge of a pan | Advice waits until the agent's turn ends, only the latest is sent, a repeat of the last problems is skipped, and `cut_off` covers only titles cut at the top or bottom |
| 2026-10-24 | A dropped connection would also end the sweep | Review: phone audio and frames were sent to the live session unguarded, so a send during a drop ended the phone loop, and a frame was lost before it was saved | Media and context sends are dropped while the session reconnects |
| 2026-10-24 | Right titles thrown out by one weak page | Live test: four correct titles (Tall Timber Tales, Tom Sawyer Abroad, a Collier Twain volume, The Little Folks Animal Story Book) were cleared because the one page the model named was a search listing, a 25-volume set or named the editor only | When the named page does not confirm, Flash reads every page the search grounded on. An editor, compiler or a volume of a set counts. Shelf1 replay titles 85.4% to 95.8%, 0 wrong |
| 2026-10-24 | The review queue was hard to act on | Report check: 70 reasons in one flat table, unnamed books shown only as an id | Each review carries an `action`. The report groups lines by what a person does (identify, price, appraise, measure, system), each with its spine picture, shelf position and only that action's reasons |
| 2026-10-24 | Face-out books report the cover width as spine thickness | Shelf2 replay: The Book of Dogs and Raggedy Ann shown cover-out measured 17.2 and 17.1 cm thick | Open: the reader should mark a face-out book and leave its thickness unmeasured |
| 2026-10-24 | The Kensington replay hit the account quota | 55 of 65 books unidentified with `429 RESOURCE_EXHAUSTED` ("check your plan and billing details") after 1.10 USD. The result is not scored | Testing stopped, to rerun once the quota is raised |
| 2026-10-24 | The bill ran far past the metered cost | 89 USD metered over 17 runs. AI Studio showed INR 27,180 for the month and no spend cap was set | Each call is sent once (twin requests removed). A process stops sending at `BUDGET_USD` (5). AI Studio spend cap set to 1 USD |
