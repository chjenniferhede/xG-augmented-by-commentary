/**
 * Read the commentary around each shot, extract the features an xG model cannot see, and store
 * the adjusted number.
 *
 *   npm run annotate -- --limit 50            a pilot
 *   npm run annotate -- --game 2016030245     one game
 *   npm run annotate -- --redo --game ...     re-annotate rows already done
 *   npm run annotate -- --dry-run --limit 2   print, write nothing
 *   npm run annotate -- --recompute           re-score stored features, no model calls
 *
 * Every cluster is written as it completes, so an interrupted run -- or one that runs out of
 * free-tier budget -- resumes without paying for the work already done.
 */

import { generateText, Output, type LanguageModel } from "ai";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { assertSchema, close, fetchRosters, fetchShots, loadEnv, query,
         type Roster, type ShotRow } from "./db.ts";
import { resolveProvider, type ProviderConfig } from "./provider.ts";
import { clusterShots } from "./cluster.ts";
import { RateLimiter } from "./limiter.ts";
import { systemPrompt, userPrompt } from "./prompt.ts";
import { clusterResponseSchema, PROMPT_VERSION, type ShotAnnotation } from "./schema.ts";
import { adjust, explain, COEFF_VERSION, IDENTITY_CALIBRATION, type ScoredFeature } from "./coefficients.ts";

interface Args {
  limit?: number; game?: string; redo: boolean; dryRun: boolean;
  recompute: boolean; provider?: string; model?: string;
}

function parseArgs(argv: string[]): Args {
  const a: Args = { redo: false, dryRun: false, recompute: false };
  for (let i = 0; i < argv.length; i++) {
    const v = argv[i + 1];
    switch (argv[i]) {
      case "--limit": a.limit = Number(v); i++; break;
      case "--game": a.game = v; i++; break;
      case "--provider": a.provider = v; i++; break;
      case "--model": a.model = v; i++; break;
      case "--redo": a.redo = true; break;
      case "--dry-run": a.dryRun = true; break;
      case "--recompute": a.recompute = true; break;
    }
  }
  return a;
}

/** Loose match so punctuation and transcription spacing do not reject a genuine quote. */
const normalise = (s: string) => s.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();

/**
 * Drop any feature whose quote is not actually in the commentary we supplied. This is the main
 * defence against a small model inventing a screen that nobody mentioned; it is checked here
 * rather than in the schema because only this layer knows what text the model was given.
 */
export function verifyQuotes(
  ann: ShotAnnotation,
  shot: ShotRow,
): {
  kept: ScoredFeature[];
  rejected: { code: string; quote: string }[];
  leaked: boolean;
  leakNote: string | null;
} {
  const haystacks = {
    pre: normalise(shot.commentary_pre ?? ""),
    post: normalise(shot.commentary_post ?? ""),
  };
  const both = `${haystacks.pre} ${haystacks.post}`;
  const kept: ScoredFeature[] = [];
  const rejected: { code: string; quote: string }[] = [];
  // What each correction code claims is wrong, so its proposed value can be compared with it.
  const recorded: Record<string, string | null> = {
    shooter_correction: shot.shooter,
    shot_type_correction: shot.shot_type,
    location_dispute: null, // free-form; only the missing-value check applies
  };
  const sameName = (a: string, b: string) =>
    normalise(a).replace(/[^a-z ]/g, "") === normalise(b).replace(/[^a-z ]/g, "");

  // Codes that all mean "something stopped or turned the puck before it reached the goalie".
  // The goalie doing it is the outcome of the shot, not a property of the chance.
  const INTERCEPTION = ["shot_blocked_lane", "partial_block",
                        "deflect_off_defender", "deflect_off_teammate"];
  // Surnames survive transcription better than first names, and a short stem absorbs the usual
  // damage (Talbot -> "Talbert", so the stem "talb" still matches).
  const goalieStem = (() => {
    const last = (shot.goalie ?? "").trim().split(/\s+/).pop() ?? "";
    return last.length >= 4 ? normalise(last).slice(0, 4) : "";
  })();
  const namesTheGoalie = (quote: string) =>
    !!goalieStem && normalise(quote).split(" ").some((w) => w.startsWith(goalieStem));

  for (const f of ann.features) {
    // Measured case: "Cole a shot blocked away by Anderson" became shot_blocked_lane, when
    // Anderson is the goalie and that sentence is simply a save. Telling the model who the
    // goalie is fixed its summary but not its choice of code, so the check lives here.
    if (INTERCEPTION.includes(f.code) && namesTheGoalie(f.quote)) {
      rejected.push({ code: f.code, quote: `names the goalie (${shot.goalie}); that is a save` });
      continue;
    }
    if (f.code in recorded) {
      // Two ways a "correction" turns out to be nothing. One: it never says what the value
      // should be, so it is a suspicion rather than a finding. Two -- much the commoner, once
      // the roster is in the prompt -- it decodes a mangled transcription back to the player
      // already recorded ("Benino" -> Nick Bonino) and files that agreement as a correction.
      if (!f.corrected_value) {
        rejected.push({ code: f.code, quote: "no corrected_value given" });
        continue;
      }
      const was = recorded[f.code];
      if (was && sameName(f.corrected_value, was)) {
        rejected.push({ code: f.code, quote: `agrees with the data (${was})` });
        continue;
      }
    }
    const needle = normalise(f.quote);
    // Accept a quote found in either window; trust the model's own source tag for which it was,
    // but require that the words exist somewhere in what we actually sent.
    if (needle.length >= 8 && both.includes(needle)) {
      kept.push({
        code: f.code, confidence: f.confidence, source: f.source,
        quote: f.quote, correctedValue: f.corrected_value,
      });
    } else {
      rejected.push({ code: f.code, quote: f.quote });
    }
  }
  // Hold outcome_leaked to the same standard as a feature. Measured over the first 237 shots,
  // 11 of 16 claimed leaks quoted the POST window -- which is not a leak, it is the design --
  // and one was paraphrased. Only a note whose words really appear BEFORE the shot counts.
  const note = normalise(ann.leak_note ?? "");
  const leaked = ann.outcome_leaked && note.length >= 8 && haystacks.pre.includes(note);
  return { kept, rejected, leaked, leakNote: leaked ? ann.leak_note : null };
}

export function inputHash(shot: ShotRow, cfg: ProviderConfig, modelId: string): string {
  return createHash("sha256")
    .update([
      PROMPT_VERSION, cfg.name, modelId,
      shot.commentary_pre ?? "", shot.commentary_post ?? "",
      String(shot.x_goal), shot.shot_type ?? "", shot.shooter ?? "",
    ].join("\u0000"))
    .digest("hex")
    .slice(0, 32);
}

async function upsert(row: Record<string, unknown>): Promise<void> {
  const cols = Object.keys(row);
  const placeholders = cols.map((_, i) => `$${i + 1}`);
  const updates = cols
    .filter((c) => c !== "game_id" && c !== "event_id")
    .map((c) => `${c} = EXCLUDED.${c}`);
  await query(
    `INSERT INTO commentary_xg (${cols.join(", ")}) VALUES (${placeholders.join(", ")})
     ON CONFLICT (game_id, event_id) DO UPDATE SET ${updates.join(", ")}`,
    cols.map((c) => row[c]),
  );
}

function buildRow(
  shot: ShotRow,
  ann: ShotAnnotation,
  features: ScoredFeature[],
  leak: { leaked: boolean; leakNote: string | null },
  cfg: ProviderConfig,
  modelId: string,
  tokens: { in?: number; out?: number },
): Record<string, unknown> {
  const base = shot.x_goal ?? 0;
  const pre = adjust(base, features.filter((f) => f.source === "pre"), ann.relevance);
  const full = adjust(base, features, ann.relevance);
  return {
    game_id: shot.game_id,
    event_id: shot.event_id,
    video_id: shot.video_id,
    x_goal: base,
    xg_adjusted_pre: pre.xgAdjusted,
    xg_adjusted_full: full.xgAdjusted,
    delta_pre: pre.delta,
    delta_full: full.delta,
    features: JSON.stringify(features),
    summary: ann.summary || null,
    explanation: explain(full),
    relevance: ann.relevance,
    attribution_confidence: ann.attribution_confidence,
    commentary_quality: ann.commentary_quality,
    outcome_leaked: leak.leaked,
    leak_note: leak.leakNote,
    youtube_url: shot.link,
    commentary_pre: shot.commentary_pre,
    commentary_post: shot.commentary_post,
    provider: cfg.name,
    model: modelId,
    prompt_version: PROMPT_VERSION,
    coeff_version: COEFF_VERSION,
    calib_version: null,
    input_hash: inputHash(shot, cfg, modelId),
    tokens_in: tokens.in ?? null,
    tokens_out: tokens.out ?? null,
  };
}

/** Re-score stored features against the current coefficient table. No model calls. */
async function recompute(): Promise<void> {
  // Carry the stored calibration forward. Re-scoring with the identity would silently strip the
  // fitted intercept off every row and leave the totals no longer matching the baseline.
  const [cal] = await query<{ calib_version: string; alpha: number; beta: number }>(
    `SELECT calib_version, alpha, beta FROM calibrations ORDER BY created_at DESC LIMIT 1`,
  );
  const calib = cal
    ? { alpha: cal.alpha, beta: cal.beta, version: cal.calib_version }
    : IDENTITY_CALIBRATION;

  const rows = await query<{
    game_id: string; event_id: string; x_goal: number; features: ScoredFeature[]; relevance: number;
    outcome_leaked: boolean; leak_note: string | null; commentary_pre: string | null;
  }>(`SELECT game_id, event_id, x_goal, features, relevance,
             outcome_leaked, leak_note, commentary_pre FROM commentary_xg`);
  console.log(
    `Re-scoring ${rows.length} rows against coefficients ${COEFF_VERSION}` +
      (cal ? ` with calibration ${cal.calib_version} (alpha ${cal.alpha.toFixed(4)})` : " (uncalibrated)") +
      "...",
  );
  let changed = 0;
  for (const r of rows) {
    const feats = (r.features ?? []) as ScoredFeature[];
    const pre = adjust(r.x_goal ?? 0, feats.filter((f) => f.source === "pre"), r.relevance ?? 0, calib);
    const full = adjust(r.x_goal ?? 0, feats, r.relevance ?? 0, calib);
    // Re-check the leak claim against the stored text, the same way a fresh annotation would.
    const note = (r.leak_note ?? "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
    const pre_txt = (r.commentary_pre ?? "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
    const leaked = r.outcome_leaked && note.length >= 8 && pre_txt.includes(note);

    const res = await query<{ updated: number }>(
      `UPDATE commentary_xg
          SET xg_adjusted_pre = $3, xg_adjusted_full = $4, delta_pre = $5, delta_full = $6,
              explanation = $7, coeff_version = $8, outcome_leaked = $9, leak_note = $10
        WHERE game_id = $1 AND event_id = $2
          AND (coeff_version IS DISTINCT FROM $8 OR xg_adjusted_full IS DISTINCT FROM $4
               OR outcome_leaked IS DISTINCT FROM $9)
        RETURNING 1 AS updated`,
      [r.game_id, r.event_id, pre.xgAdjusted, full.xgAdjusted, pre.delta, full.delta,
       explain(full), COEFF_VERSION, leaked, leaked ? r.leak_note : null],
    );
    if (res.length) changed++;
  }
  console.log(`Updated ${changed} of ${rows.length}.`);
}

/** A provider refusing for the rest of the day, as opposed to a momentary rate limit. */
export function isQuotaExhausted(err: unknown): boolean {
  const text = err instanceof Error ? `${err.message}` : String(err);
  return /RESOURCE_EXHAUSTED|exceeded your current quota|free_tier_requests/i.test(text);
}
const isTransient = (err: unknown) =>
  /UNAVAILABLE|high demand|overloaded|50[0-9]|ECONNRESET|fetch failed/i.test(
    err instanceof Error ? err.message : String(err),
  );

/**
 * Try each model in the rotation until one answers.
 *
 * Google counts the free-tier request quota per model, so a model that is out for the day is
 * dead weight for the rest of the run: it is parked in `exhausted` and skipped rather than
 * retried. Only when every model is out do we give up -- and then the caller stops the run so
 * the next day picks up from the database.
 */
async function callWithRotation(
  cfg: ProviderConfig,
  limiter: RateLimiter,
  exhausted: Set<string>,
  params: () => Omit<Parameters<typeof generateText>[0], "model">,
): Promise<{ result: Awaited<ReturnType<typeof generateText>>; modelId: string }> {
  const available = cfg.modelIds.filter((m) => !exhausted.has(m));
  if (available.length === 0) throw new AllModelsExhausted();

  let lastErr: unknown;
  for (const modelId of available) {
    try {
      const result = await limiter.run(
        () =>
          generateText({ model: cfg.model(modelId) as LanguageModel, ...params() } as Parameters<typeof generateText>[0]),
        5,
        (err) => !isQuotaExhausted(err),
      );
      return { result, modelId };
    } catch (err) {
      lastErr = err;
      if (isQuotaExhausted(err)) {
        exhausted.add(modelId);
        console.warn(`  ${modelId}: daily free-tier quota used up; rotating to the next model`);
        continue;
      }
      if (isTransient(err)) {
        console.warn(`  ${modelId}: temporarily unavailable; trying the next model`);
        continue;
      }
      throw err;
    }
  }
  if (cfg.modelIds.every((m) => exhausted.has(m))) throw new AllModelsExhausted();
  throw lastErr;
}

class AllModelsExhausted extends Error {
  constructor() {
    super("every model in the rotation is out of free-tier quota for today");
    this.name = "AllModelsExhausted";
  }
}

/**
 * True only when this file is what node was asked to run.
 *
 * Without the check, importing anything from a CLI module runs its main() as a side effect -- a
 * unit test that imports one helper would start a real annotation run against the database and
 * the model. Tests import from here, so the guard is load-bearing, not tidiness.
 */
const runningAsScript = (url: string): boolean => {
  const entry = process.argv[1];
  return !!entry && url === pathToFileURL(entry).href;
};

async function main(): Promise<void> {
  loadEnv();
  const args = parseArgs(process.argv.slice(2));
  await assertSchema();

  if (args.recompute) {
    await recompute();
    await close();
    return;
  }

  const cfg = resolveProvider({ provider: args.provider, model: args.model });
  const limiter = new RateLimiter(cfg.rpm, cfg.tokensPerDay);
  const shots = await fetchShots({ game: args.game, limit: args.limit, redo: args.redo });
  const clusters = clusterShots(shots);
  const rosters = await fetchRosters([...new Set(shots.map((s) => s.game_id))]);
  console.log(
    `${cfg.name} [${cfg.modelIds.join(" -> ")}]: ${shots.length} shots in ${clusters.length} clusters ` +
      `(${cfg.rpm} rpm${cfg.tokensPerDay ? `, ${cfg.tokensPerDay} tokens/day` : ""})` +
      (args.dryRun ? "  [dry run]" : ""),
  );
  if (!shots.length) {
    console.log("Nothing to do. Use --redo to re-annotate rows that already exist.");
    await close();
    return;
  }

  const system = systemPrompt();
  const exhausted = new Set<string>();
  const stats = { done: 0, features: 0, rejected: 0, leaked: 0, relevant: 0, tokens: 0, failed: 0 };

  for (const [i, cluster] of clusters.entries()) {
    if (limiter.budgetExhausted()) {
      console.log(`\nDaily token budget reached (${limiter.used}). Stopping; re-run to continue.`);
      break;
    }
    const label = `[${i + 1}/${clusters.length}] game ${cluster[0]!.game_id} ` +
      `events ${cluster.map((s) => s.event_id).join(",")}`;

    if (args.dryRun) {
      console.log(`\n${label}\n${userPrompt(cluster, rosters.get(cluster[0]!.game_id))}\n`);
      continue;
    }

    try {
      // generateText + Output.object, not the deprecated generateObject (ai v7).
      // maxRetries: 0 -- the SDK's own retry loop hides the status code from the limiter and
      // burns quota re-sending a request the provider has already refused for the day.
      const { result, modelId } = await callWithRotation(cfg, limiter, exhausted, () => ({
        output: Output.object({ schema: clusterResponseSchema }),
        system,
        prompt: userPrompt(cluster),
        temperature: 0,
        maxOutputTokens: 4000,
        maxRetries: 0,
        providerOptions: cfg.providerOptions,
      }));
      const used = (result.usage.inputTokens ?? 0) + (result.usage.outputTokens ?? 0);
      limiter.record(used);
      stats.tokens += used;

      const perShot = new Map(result.output.shots.map((s: ShotAnnotation) => [String(s.event_id), s]));
      for (const shot of cluster) {
        const ann = perShot.get(String(shot.event_id)) as ShotAnnotation | undefined;
        if (!ann) {
          console.warn(`  ${label}: model returned nothing for event ${shot.event_id}`);
          stats.failed++;
          continue;
        }
        const { kept, rejected, leaked, leakNote } = verifyQuotes(ann, shot);
        stats.features += kept.length;
        stats.rejected += rejected.length;
        if (leaked) stats.leaked++;
        if (ann.relevance >= 0.3) stats.relevant++;
        for (const r of rejected) {
          console.warn(`  dropped ${r.code}: quote not in commentary -- "${r.quote.slice(0, 60)}"`);
        }
        // One call covers the whole cluster, so split its usage across the shots it produced.
        // Storing the full figure on each row made SUM(tokens_in) overstate real usage by ~47%.
        await upsert(buildRow(shot, ann, kept, { leaked, leakNote }, cfg, modelId, {
          in: Math.round((result.usage.inputTokens ?? 0) / cluster.length),
          out: Math.round((result.usage.outputTokens ?? 0) / cluster.length),
        }));
        stats.done++;
      }
      const tail = cluster
        .map((s) => {
          const a = perShot.get(String(s.event_id)) as ShotAnnotation | undefined;
          return a ? `${s.event_id}:rel=${a.relevance.toFixed(2)},f=${a.features.length}` : `${s.event_id}:-`;
        })
        .join(" ");
      console.log(`${label}  ${tail}  (${used} tok via ${modelId}, ${stats.tokens} total)`);
    } catch (err) {
      if (err instanceof AllModelsExhausted) {
        console.log(
          `\nEvery model is out of free-tier quota for today ` +
            `(${cfg.modelIds.join(", ")}). Stopping; re-run tomorrow to continue where this left off.`,
        );
        break;
      }
      stats.failed += cluster.length;
      console.error(`${label}  FAILED: ${err instanceof Error ? err.message : String(err)}`);
      if (limiter.looksThrottled) {
        console.error("Repeated rate limiting -- lower LLM_RPM and re-run; progress is saved.");
        break;
      }
    }
  }

  if (!args.dryRun) {
    console.log(
      `\nAnnotated ${stats.done} shots. ` +
        `${stats.relevant} had relevant commentary (${pctOf(stats.relevant, stats.done)}), ` +
        `${stats.features} features kept, ${stats.rejected} rejected for unverifiable quotes, ` +
        `${stats.leaked} leaked the outcome. ${stats.failed} failed. ${stats.tokens} tokens.`,
    );
  }
  await close();
}

const pctOf = (n: number, d: number) => (d ? `${((100 * n) / d).toFixed(0)}%` : "n/a");

if (runningAsScript(import.meta.url)) {
  main().catch((err) => {
    console.error(err instanceof Error ? err.message : err);
    void close();
    process.exitCode = 1;
  });
}
