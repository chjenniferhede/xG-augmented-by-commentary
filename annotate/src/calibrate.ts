/**
 * Normalise the adjusted xG and score it against what actually happened.
 *
 *   npm run calibrate            fit, score, store a calibration row
 *   npm run calibrate -- --dry   print the numbers, write nothing
 *
 * Two different things, often confused:
 *  - Normalisation keeps the adjusted numbers on the same scale as MoneyPuck's, by fitting an
 *    intercept so the two sum to the same total. The commentary then moves expected goals
 *    between shots instead of manufacturing them.
 *  - Scoring asks whether the result is any better: Brier and log loss against real goals, on
 *    xg_adjusted_pre only. If it is not better, that is the finding, and it gets printed.
 */

import { assertSchema, close, loadEnv, query } from "./db.ts";
import { pathToFileURL } from "node:url";
import { adjust, logit, sigmoid, COEFF_VERSION, type ScoredFeature } from "./coefficients.ts";
import { brier, logLoss, reliability, fitZeroSumAlpha, type Scored } from "./stats.ts";

interface Row {
  game_id: string; event_id: string; x_goal: number | null; goal: boolean | null;
  features: ScoredFeature[] | null; relevance: number | null;
}

const pad = (s: string, n: number) => s.padEnd(n);
const f4 = (n: number) => (Number.isFinite(n) ? n.toFixed(4) : "   n/a");

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
  await assertSchema();
  const dry = process.argv.includes("--dry");

  const rows = await query<Row>(
    `SELECT c.game_id, c.event_id, c.x_goal, s.goal, c.features, c.relevance
       FROM commentary_xg c
       JOIN shot_commentary_split s USING (game_id, event_id)
      WHERE c.x_goal IS NOT NULL AND s.goal IS NOT NULL`,
  );
  if (rows.length < 20) {
    console.log(`Only ${rows.length} annotated shots with a known outcome. Annotate more first.`);
    await close();
    return;
  }

  // Pre-shot features only: the post-shot window is downstream of the result.
  const preShifts = rows.map((r) =>
    adjust(r.x_goal ?? 0, (r.features ?? []).filter((f) => f.source === "pre"), r.relevance ?? 0).shift,
  );
  const baseLogits = rows.map((r) => logit(r.x_goal ?? 0));
  const targetSum = rows.reduce((s, r) => s + (r.x_goal ?? 0), 0);

  const alpha = fitZeroSumAlpha(baseLogits, preShifts, targetSum);
  const beta = 1;

  const base: Scored[] = rows.map((r) => ({ p: r.x_goal ?? 0, goal: !!r.goal }));
  const adj: Scored[] = rows.map((r, i) => ({
    p: sigmoid(alpha + beta * (baseLogits[i] ?? 0) + (preShifts[i] ?? 0)),
    goal: !!r.goal,
  }));

  const moved = preShifts.filter((s) => Math.abs(s) > 1e-9).length;
  const nGoals = rows.filter((r) => r.goal).length;
  const bBase = brier(base), bAdj = brier(adj);
  const lBase = logLoss(base), lAdj = logLoss(adj);

  console.log(`\n${rows.length} annotated shots, ${nGoals} goals (${((100 * nGoals) / rows.length).toFixed(1)}%).`);
  console.log(`${moved} shots (${((100 * moved) / rows.length).toFixed(0)}%) had a pre-shot adjustment; the rest are unchanged.`);
  console.log(`Zero-sum intercept alpha = ${alpha.toFixed(4)} (beta fixed at ${beta}).`);
  console.log(`  sum xG  baseline ${targetSum.toFixed(2)}  adjusted ${adj.reduce((s, r) => s + r.p, 0).toFixed(2)}  actual goals ${nGoals}`);
  console.log(`\n  ${pad("metric", 12)} ${pad("MoneyPuck", 11)} ${pad("adjusted", 11)} verdict`);
  for (const [name, b, a] of [["Brier", bBase, bAdj], ["log loss", lBase, lAdj]] as const) {
    const better = a < b;
    console.log(
      `  ${pad(name, 12)} ${pad(f4(b), 11)} ${pad(f4(a), 11)} ` +
        `${better ? "better" : "WORSE"} by ${Math.abs(a - b).toFixed(5)}`,
    );
  }
  if (bAdj >= bBase) {
    console.log(
      "\nThe commentary adjustment does not beat MoneyPuck on this sample. That is a real result,\n" +
        "not a bug: with few adjusted shots the honest reading is 'no measurable improvement yet'.",
    );
  }

  const bins = reliability(adj);
  console.log(`\n  reliability (adjusted)`);
  for (const b of bins.filter((x) => x.n > 0)) {
    console.log(
      `   ${b.lo.toFixed(1)}-${b.hi.toFixed(1)}  n=${pad(String(b.n), 5)} predicted ${f4(b.meanPred)}  observed ${f4(b.observed)}`,
    );
  }

  if (dry) {
    console.log("\n--dry: nothing written.");
    await close();
    return;
  }

  const version = `cal-${new Date().toISOString().slice(0, 10)}-${COEFF_VERSION}`;
  await query(
    `INSERT INTO calibrations (calib_version, alpha, beta, n_shots, n_goals, brier_base, brier_adj,
                               logloss_base, logloss_adj, reliability, coeff_version, notes)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
     ON CONFLICT (calib_version) DO UPDATE SET
       alpha=EXCLUDED.alpha, beta=EXCLUDED.beta, n_shots=EXCLUDED.n_shots, n_goals=EXCLUDED.n_goals,
       brier_base=EXCLUDED.brier_base, brier_adj=EXCLUDED.brier_adj,
       logloss_base=EXCLUDED.logloss_base, logloss_adj=EXCLUDED.logloss_adj,
       reliability=EXCLUDED.reliability, created_at=now()`,
    [version, alpha, beta, rows.length, nGoals, bBase, bAdj, lBase, lAdj,
     JSON.stringify(bins), COEFF_VERSION,
     `${moved} of ${rows.length} shots adjusted; zero-sum against the MoneyPuck total`],
  );
  // Apply the fitted intercept to the stored rows. Without this the calibration exists only in
  // the calibrations table while the dashboard keeps summing uncalibrated numbers -- and the
  // claim that the adjusted total matches the baseline would be false of the actual data.
  const all = await query<Row>(
    `SELECT game_id, event_id, x_goal, NULL::boolean AS goal, features, relevance FROM commentary_xg`,
  );
  const calib = { alpha, beta, version };
  for (const r of all) {
    const feats = (r.features ?? []) as ScoredFeature[];
    const pre = adjust(r.x_goal ?? 0, feats.filter((f) => f.source === "pre"), r.relevance ?? 0, calib);
    const full = adjust(r.x_goal ?? 0, feats, r.relevance ?? 0, calib);
    await query(
      `UPDATE commentary_xg
          SET xg_adjusted_pre = $3, xg_adjusted_full = $4,
              delta_pre = $5, delta_full = $6, calib_version = $7
        WHERE game_id = $1 AND event_id = $2`,
      [r.game_id, r.event_id, pre.xgAdjusted, full.xgAdjusted, pre.delta, full.delta, version],
    );
  }
  console.log(`\nStored calibration ${version} and applied it to ${all.length} rows.`);
  await close();
}

if (runningAsScript(import.meta.url)) {
  main().catch((e) => {
    console.error(e instanceof Error ? e.message : e);
    void close();
    process.exitCode = 1;
  });
}
