# Video processing recovery

## Scope and baseline

Work is on `ken/video-processing-recovery`; nothing has been committed, pushed, or
merged. The repository reads `DATABASE_URL` from its ignored `.env` and connects
to PostgreSQL schemas `ops` and `warehouse` as `xg_pipeline`. Credentials
are not recorded here. Read-only investigation queries used `BEGIN READ ONLY`,
a five-second statement timeout, and a one-second lock timeout. The deployed
schema versions matched the checked-in migrations before recovery.

On 2026-11-18, the starting queue had **19 aligned videos**, each with a current
passing warehouse alignment; 5 videos were `alignment_unverified`, 10 were
`game_unresolved` (9 NHL games and one World Cup game), 3 were `game_loaded`,
and 1 was `transcribed`. This checkout initially had no `data/` cache.

## Phase 1: verified games and controlled jobs

All nine ambiguous NHL replays were verified against their fetched title or
opening captions and NHL game reports. The current titles for `daLAIWujj6w`,
`ExKPs2APq4A`, and `iSY4vliozTs` contained the exact game date; the existing
resolver selected the latter two automatically. Other matches were pinned
through the existing `pipeline.py add --game-id` path only after corroboration:

| Video | Verified game | Evidence from replay and NHL | Current result |
| --- | --- | --- | --- |
| `daLAIWujj6w` | `2025020790` | Current title says Jan 21, 2026; [Utah comeback recap](https://www.nhl.com/news/philadelphia-flyers-utah-mammoth-game-recap-january-21-2026) | **Aligned** |
| `HL94VFb6iyA` | `2019020951` | David Ayres replay; [NHL recap](https://www.nhl.com/news/carolina-hurricanes-toronto-maple-leafs-game-recap-315386778) | Captions loaded; source event conflict |
| `brW31GmX1Do` | `2008030222` | Crosby/Ovechkin hat tricks; [playoff Game 2](https://www.nhl.com/news/this-date-in-nhl-history-may-4-280540066) | No MoneyPuck game CSV |
| `ExKPs2APq4A` | `2014020502` | Current title says Dec 22, 2014; [Gaudreau first hat trick](https://www.nhl.com/flames/news/fast-facts-flames-kings-283440270) | No MoneyPuck game CSV |
| `iSY4vliozTs` | `2019021036` | Current title says Mar 5, 2020; [Zibanejad five goals](https://www.nhl.com/news/washington-capitals-new-york-rangers-game-recap-315780448) | **Aligned** |
| `cgUMKv0HLUc` | `2023021060` | Opening says third and final matchup; [Tkachuk OT hat trick](https://www.nhl.com/news/ottawa-senators-new-york-islanders-game-recap-march-16) | **Aligned** |
| `YXKVkd39ZSw` | `2025020142` | Opening says Wild lost to Utah the previous night; [Oct 25 Utah result](https://www.nhl.com/utah/news/utah-wins-sixth-straight-with-6-2-win-over-minnesota-game-story-10-25-25) and [Oct 26 Sharks OT win](https://www.nhl.com/wild/news/topic/game-recaps/san-jose-sharks-minnesota-wild-game-recap-october-26-2025-x1503) | **Aligned** |
| `60-5C9m7lZo` | `2019020803` | Opening calls it the fourth meeting; [Feb 1 Battle of Alberta recap](https://www.nhl.com/news/edmonton-oilers-top-calgary-flames-in-heated-battle-of-alberta-314569574) | **Aligned** |
| `wNv3lukwAq0` | `2025020208` | Opening says Saros lost to Vancouver in OT the prior night; [Nov 3 recap](https://www.nhl.com/predators/news/preds-come-back-to-earn-point-fall-to-canucks-in-overtime-2025-11-03) | **Aligned** |

`daLAIWujj6w` loaded 729 captions, 354 events, and 98 shots. Early capped
alignment runs saved 234 OCR readings, enough to pin 55/60 play segments.
One continuation, run 40, logged 71 failed frame seeks and was stopped.
Its `ffmpeg` stderr was not retained, so the transport/decoding cause is
unconfirmed; its abandoned run record was marked failed under the pipeline
lock. The saved readings nonetheless passed every original quality gate
offline: 0.917 coverage, zero reversals, and score agreement 1.0.
After the targeted dense-scan change below, run 55 passed using six YouTube
requests. The database confirms `aligned` and `passed = true`.

`eZw4UCsNAS0`, previously ready but never attempted, received three capped
frame batches of 150, 100, and 50 requests. Its cached clocks then covered
63/68 segments. Run 62 validated the saved readings with zero new requests:
**0.926 coverage, zero reversals, score agreement 1.0**, and a passing
warehouse alignment.

All recovery jobs used the scheduled trigger for spacing and hourly caps,
per-run request budgets, and `XG_KEEP_FRAMES=1`. Batches ended at their
budget and retained `index.json` and JPEG caches. No existing passing
alignment was reprocessed. The last direct database check found **30 current passing
alignments**, including the original 19.

## Phase 2: source and alignment failures

The original `efgAdKZD6g4` duplicate key is `(2020020290,297)`.
Its cached MoneyPuck CSV contains `GOFF` and `GEND` rows with that ID;
they differ in `event` and `leadWinProbability`. The Ayres game's CSV
similarly reuses event ID 335 for distinct `PEND` and `GEND` rows.
`build_db.load_events` now removes **only identical parsed duplicate
rows** and raises `GameDataConflict` for distinct rows before any warehouse
transaction. The pipeline keeps the transcript and sets
`next_attempt_at = 'never'` for a conflict, requiring deliberate source
adjudication rather than repeated automatic retries. Both observed games
remain blocked; no source row was silently discarded.

Three videos that failed to locate the scoreboard in October now locate it
with the current code and newly saved full frames:
`E8nCckGKf4Y`, `RrfcpMf2Zls`, and `7gCtZ0mktyQ`. Visual inspection
confirmed visible period/clock graphics. Each initial probe stopped at ten requests
and saved its partial OCR cache. All three have since aligned as described
below.
Their old runs used a substantially earlier, dirty code version, so the
exact historical cause cannot be isolated.

The historical quality failures `TOc0LCl9wX0` and `LXyz_HBWc-w` had
coverage 0.812 (56/69) and 0.695 (41/59), respectively. Their score checks
passed. Their old frame indexes were absent from this checkout. New bounded
jobs rebuilt both caches: `LXyz_HBWc-w` recovered; `TOc0LCl9wX0` remains
below the coverage gate as detailed below.

`align_video.align` now checks quality after bisection once clock coverage
reaches the **existing** 0.90 gate, before a costly dense scan. If another
quality gate fails, it falls back to the original dense scan. This recovered
`daLAIWujj6w` from saved readings. The earlier uncommitted frame retry
change also discards legacy empty failed readings and does not cache new
failed fetches. The quality thresholds remain coverage >= 0.90, reversal
rate <= 0.01, and score match >= 0.90 when enough scores are read.

Five historical failed alignments had already passed before this work:
`0AMIEzrA28M`, `7wt9Y_89whY`, `Fz88yBrqurM`, `nCsYRIoMbxU`,
and `OEzJFPhsRkc`. Several passing retries fetched zero new frames.
Their dirty code versions prevent attribution to a single change.

### Targeted recovery of `7gCtZ0mktyQ`

Runs 65 and 66 grew its saved clock coverage to 62/70 play segments. Run 67
used 30 more requests without pinning another segment. Eight remained unpinned.
Cached event and OCR inspection identified segment 11 (game seconds 849–858)
and segment 25 (1876–1908) as plausible targets. The remaining short segments
had subsecond or inconsistent video brackets, or occurred at the end of
regulation when the broadcast omitted the clock.

Targeted run 69 fetched five frames inside segment 25. The frame at video
second 2558 showed game second 1871; frames from 2568 to 2590 showed play or
crowd shots without a scoreboard. Run 70 fetched five frames near the end of
segment 11, but each clock was already frozen at game second 858. Run 71
sampled earlier: video second 1122 still read 849, and second 1126 read 851.
That last reading pinned the 63rd segment, so sampling stopped immediately.
The cached quality check then passed **63/70 coverage (0.900), zero clock
reversals, and 1.000 score agreement** on 98 checked readings. Run 72 saved
the alignment through the normal pipeline in offline mode, with zero new
YouTube requests. Direct database verification found `ops.videos.state =
aligned`, current `warehouse.alignments.passed = true`, and **22** current
passing videos.

The local `.env` configures `XG_FRAME_PER_HOUR=1800` and a one-second frame
interval. The user subsequently authorized this configured limit for further
recovery, with one active video at a time. All monitored frame/page requests
through run 91 succeeded; no 403, 429, or YouTube block occurred. A future
YouTube block or throttle requires pausing and reverting to 600/hour.

### Further controlled recoveries

All passing results below were saved by the normal scheduled pipeline in
offline mode after the cached quality check passed. Their save runs fetched
zero additional YouTube frames. Each was verified as the current passing
alignment in the warehouse, and none overwrote an earlier success.

| Video | Save run | Pinned segments | Coverage | Reversals | Score agreement |
| --- | ---: | ---: | ---: | ---: | ---: |
| `wNv3lukwAq0` | 76 | 45/50 | 0.900 | 0 | No readable scores |
| `RrfcpMf2Zls` | 80 | 52/57 | 0.912 | 0 | 1.000 on 77 readings |
| `E8nCckGKf4Y` | 84 | 61/67 | 0.910 | 0 | 1.000 on 78 readings |
| `60-5C9m7lZo` | 87 | 52/57 | 0.912 | 0 | 1.000 on 59 readings |
| `YXKVkd39ZSw` | 91 | 54/59 | 0.915 | 0 | 1.000 on 78 readings |
| `cgUMKv0HLUc` | 94 | 55/61 | 0.902 | 0 | 1.000 on 55 readings |
| `iSY4vliozTs` | 100 | 56/62 | 0.903 | 0 | No readable scores |
| `LXyz_HBWc-w` | 105 | 54/59 | 0.915 | 0 | 1.000 on 83 readings |

The first `60-5C9m7lZo` pass revealed five frames whose broadcast graphic
still read `2ND 19:16` through `2ND 19:03` after the third period had begun.
The next frame read `3RD 19:02`, with the power-play countdown continuous.
`align_video.PERIOD_CORRECTIONS` corrects only those exact video times and
clock values; the original OCR is retained. This removes a false reversal and
bad segment brackets. The `YXKVkd39ZSw` frames exposed two clock-selection
errors: a player-shift timer below the scoreboard and a shot-speed decimal
beside it were closer to the period label than the real clock. Candidate
ranking now favors vertical alignment and minute:second clocks. Regression
tests cover both cases. Every newly recovered video's cached alignment still
passes after that parser change.

Run 92 of `cgUMKv0HLUc` ended during a transient PostgreSQL pooler DNS
resolution failure. On resumption, its process and pipeline lock were gone,
so the abandoned operations record was closed. Its 94 cached frames remained.
The first-period graphic visibly says `1st`, but OCR recorded `lst`; accepting
that exact label recovered ten first-period segments from cache. Run 93 then
pinned 13 more segments with 20 targeted frames and stopped at passing quality.

`iSY4vliozTs` passed after one bounded cache-building run and three small
targeted runs. Its final needed clock was game second 2362.7 at video second
3232.5. This broadcaster did not provide readable scores, so the existing
gate used coverage and clock reversals without changing its score threshold.

`LXyz_HBWc-w` initially reproduced its historical 41/59 coverage. A frame
showed that a statistics graphic's `RANK 3RD` and split `82.6%` had been
mistaken for a game clock, creating a false reversal and bad video brackets.
The parser now rejects a decimal candidate with an adjacent percent sign.
With corrected cached brackets, 23 targeted frames raised coverage to 54/59.

`TOc0LCl9wX0` remains `alignment_unverified`. Its old 551-frame alignment
reached only 56/69 segments. A new 125-request bounded run rebuilt 120 OCR
frames; its cache-only check pinned 44/69. A seven-frame probe of the opening
and late-overtime windows added only one segment, leaving 45/69 (0.652).
Opening lineup graphics hide the clock, several plays are cut to subsecond
video brackets, and the final moments lack reliable clock OCR. Further broad
scans are not justified by the saved evidence. The new runs recorded one
failed frame seek at video second 735; all other frame and page requests
succeeded, with no 403, 429, or YouTube block. The failed seek did not trip
the limiter, so the authorized 1,800/hour setting remains in effect.

## Phase 3 verification and remaining work

The database was checked after each passing alignment. The original 19
remain current, and all eleven newly recovered videos pass the unchanged
gates. The full local suite currently passes **53 tests**, including new
regressions for identical versus conflicting MoneyPuck rows, manual-review
state for conflicts, skipping dense scans after sufficient coverage, and
falling back when another gate fails. `git diff --check` passes.

Among the prioritized recovery candidates, `TOc0LCl9wX0` remains below the
unchanged coverage gate; two games lack MoneyPuck CSVs, and two have
conflicting terminal event IDs (including `efgAdKZD6g4`). Source conflicts
need an explicit upstream-data decision, not automatic deduplication. The
full 58-video operations queue currently has 30 aligned, 1
`alignment_unverified`, 2 `game_loaded`, 2 `transcribed`, 16 `no_game_data`,
3 `no_captions`, 3 `discovered`, and 1 `game_unresolved`; the other queue
states were outside these targeted attempts. All eleven recovered videos'
local caches were rechecked against the current parser and still pass.
The database has 30 current passing alignments versus 19 at the baseline.
