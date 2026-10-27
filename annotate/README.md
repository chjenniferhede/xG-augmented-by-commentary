# annotate — commentary-adjusted xG

Reads the broadcast commentary around each shot, extracts the things an expected-goals model
cannot see, and turns them into an adjusted xG with a reason attached.

MoneyPuck's xG — like every public model — is built from the NHL's public play-by-play. That
feed has location, angle, shot type, rebound and rush flags, strength state and empty net. It has
**no screens, no pre-shot passing, no goalie position, no shooter pressure**. Those are the
largest known gaps in public xG, and they are exactly what commentators say out loud: *"he never
saw it"*, *"backdoor, nobody home"*, *"that's deflected off the defenceman"*. Tracking-data models
(Sportlogiq, NHL EDGE) have those features; this is a cheap proxy built from speech.

## Running it

```sh
make annotate-setup     # npm install
make annotate-pilot     # 50 shots, free tier
make calibrate          # normalise and score against real goals
make run-xg-dash        # http://127.0.0.1:5058
```

`make annotate` works through everything not yet done. It is resumable: each cluster is written
as it completes, so an interrupted run — or one that hits the day's free-tier quota — picks up
where it left off.

## How a number is produced

1. **Split the window.** `shot_commentary_split` gives commentary before the shot and after it
   separately (migrations 0005/0006).
2. **Cluster.** Shots within 20 s share a window, so they are judged together in one request —
   cheaper, and it forces the model to decide which shot each detail belongs to.
3. **Extract.** The model returns features from a closed vocabulary, each with a confidence, a
   `pre`/`post` tag, and **a verbatim quote**.
4. **Verify the quotes.** Any feature whose quote is not actually in the text we sent is dropped,
   and the same check is applied to the leak flag. This is the main defence against a small model
   inventing hockey.
5. **Score, in TypeScript.** `coefficients.ts` converts features to a log-odds shift. The model
   never produces the number, and the `explanation` text is generated from the arithmetic, so it
   can never disagree with the figure it explains.

```
z = alpha + logit(x_goal) + relevance_gate * sum(weight_i * confidence_i)
```

Relevance gates everything: below 0.3 the adjustment is zero. Most commentary is filler or is
about a neighbouring shot, so **no change is the expected answer** — in practice about 70% of
shots come back untouched.

## Two xG numbers, on purpose

The window runs past the shot, so it contains the call. A model that reads *"SCORES!"* is not
predicting, it is reporting.

| Column | Built from | Use |
|---|---|---|
| `xg_adjusted_pre` | commentary up to the shot | The honest one. The only one scored against goals. |
| `xg_adjusted_full` | plus post-shot replay talk | Richer description. Not evaluable. |

The replay talk is where most screen/deflection detail lives, so it is used — but the result
itself is off limits, and `outcome_leaked` flags the cases where the *pre-shot* window gave the
game away — about 1.7% of shots, where a caption line straddles the moment of release
(*"Goes wide"*, *"Save Anderson."*).

That flag is quote-verified like every feature: the model's `leak_note` must appear verbatim in
the pre-shot text. It is worth the check — on the first 237 shots the model claimed 16 leaks, but
11 of them quoted the *post* window, which is not a leak but the design.

## Free-tier reality

Google counts the free request quota **per model**, and the numbers are not published. Measured
on this project: `gemini-3.8-flash` allows **20 requests a day**. So `LLM_MODEL` is a rotation,
not one model — when one runs out the runner moves to the next and only stops when all are spent.

```
LLM_PROVIDER=google
LLM_MODEL=gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.6-flash,gemini-3.5-flash
```

`gemini-2.5-*` is deliberately absent: Google returns 404 *"no longer available to new users"*.
`--provider groq` and `--provider openrouter` work through the same code path.

Roughly 2 000 tokens per cluster; the full corpus is ~600 clusters. At free-tier rates that is
days, not minutes — which is why resumability matters more than speed.

**Free tiers may train on what you send them.** The captions are YouTube data the project's
README asks to keep private. A paid Flash tier is a few dollars for the whole corpus and avoids
the question.

## Tuning the coefficients is free

`features` is stored, so `make recompute` re-derives every adjusted xG, explanation and leak flag
against a new coefficient table **without calling a model**. Change a weight in `coefficients.ts`, bump
`COEFF_VERSION`, run `make recompute`.

The weights are **priors, not measurements** — set from what is publicly known about shot
quality, not fitted. `make calibrate` reports what a fit would say and prints, honestly, when the
adjustment fails to beat MoneyPuck.

## Choosing a model on evidence

```sh
npm run eval-gold -- --export 30   # template: watch each link, fill in what truly applies
make eval-gold                     # per-feature precision/recall + relevance agreement
```

Without this, comparing providers is guesswork. `annotate/gold/*.json` is gitignored because it
embeds caption text.

## Files

| File | What it does |
|---|---|
| `vocabulary.ts` | the feature codes, what each means, which contradict each other |
| `coefficients.ts` | weights and the log-odds maths; pure and unit-tested |
| `schema.ts` | the shape the model must return |
| `prompt.ts` | the instructions, kept terse because tokens are the budget |
| `cluster.ts` | groups shots that share a commentary window |
| `provider.ts` | model rotation, per-provider limits |
| `limiter.ts` | pacing, 429 backoff, daily token ledger |
| `annotate.ts` | the run loop: fetch, call, verify quotes, score, upsert |
| `calibrate.ts` | zero-sum normalisation, Brier/log loss, reliability |
| `eval-gold.ts` | scores extraction against hand labels |
| `server.ts`, `views/` | the dashboard |

Schema lives in `scripts/db/migrations/warehouse/` and is applied by the Python layer; this
program never issues DDL.
