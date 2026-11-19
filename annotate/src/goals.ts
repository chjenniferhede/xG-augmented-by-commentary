/**
 * Goals-only refinement: re-rank the goals by what the commentary says about each chance.
 *
 *   npm run goals              print the summary and write data/out/goals.csv
 *
 * Why goals only. Fitting on every shot needs commentary from before the shot, because the replay
 * talk gives the result away. Commentators rarely describe a goal until the replay, so that left
 * 4 usable goals. If every shot in the set is a goal, the replay can't give anything away, and
 * the replay is where the passes, screens and deflections get described.
 *
 * Why a fixed budget. With only goals there are no misses to learn from, so any adjustment
 * judged against "did it go in" would push every goal up. The total is therefore pinned to
 * MoneyPuck's total over the same goals: the commentary can move expected goals between goals,
 * never add them. The result is an ordering of goals by chance quality, not a better-calibrated
 * xG -- whether the ordering is *right* needs an outside reference (hand ratings or tracked data).
 *
 * The weights are the hand-set priors in coefficients.ts, unchanged. Nothing here is fitted.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { pathToFileURL } from "node:url";
import { assertSchema, close, loadEnv, query, REPO_ROOT } from "./db.ts";
import { adjust, IDENTITY_CALIBRATION, logit, sigmoid, WEIGHTS, type Contribution, type ScoredFeature } from "./coefficients.ts";
import { fitZeroSumAlpha } from "./stats.ts";
import { featureDef } from "./vocabulary.ts";
import { featureLabel } from "./views/labels.ts";

/**
 * Goalie-state features that, said on a goal's replay, usually describe the moment the puck went
 * in rather than the chance: "he was down and out" is how the goal looked, not why it was likely.
 * Said before the shot they describe the chance and still count.
 */
export const FINISH_ON_REPLAY: ReadonlySet<string> = new Set([
  "goalie_out_of_position",
  "goalie_down_scrambling",
]);

export interface GoalOptions {
  /** Use features from the after-the-shot (replay) window. */
  useReplay: boolean;
  /** Ignore FINISH_ON_REPLAY features when they come from the replay. */
  dropReplayFinish: boolean;
  /**
   * What-if: multiply every weight in a vocabulary group (A-F) by this factor. Missing groups
   * keep their weights. Used by the dashboard's sliders; the analysis itself never sets it.
   */
  groupScale?: Readonly<Record<string, number>>;
  /**
   * Where the total is held fixed. "pooled": one budget across all goals, so xG can move
   * between games. "game": each game keeps its own total, so goals are only re-ordered against
   * goals from the same broadcast -- a crew that describes more cannot pull xG from a quieter one.
   */
  budget?: "pooled" | "game";
}
export const DEFAULT_OPTIONS: GoalOptions = { useReplay: true, dropReplayFinish: true, budget: "pooled" };

/** The vocabulary groups a what-if can scale, in the order the dashboard shows them. */
export const GROUPS: readonly { key: string; label: string }[] = [
  { key: "A", label: "Goalie sight line (screens, traffic)" },
  { key: "B", label: "Deflections and blocks" },
  { key: "C", label: "Goalie state" },
  { key: "D", label: "Passing before the shot" },
  { key: "E", label: "Shooter state" },
  { key: "F", label: "Rare (breakaways, scrambles)" },
];

function scaledWeights(scale: Readonly<Record<string, number>>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [code, w] of Object.entries(WEIGHTS)) {
    out[code] = w * (scale[featureDef(code)?.group ?? ""] ?? 1);
  }
  return out;
}

export interface GoalInput {
  game_id: string;
  event_id: string;
  x_goal: number;
  relevance: number;
  features: ScoredFeature[];
}

export interface RefinedGoal<T extends GoalInput = GoalInput> {
  goal: T;
  xgBase: number;
  xgRefined: number;
  /** Log-odds shift from this goal's own features, before rebalancing. 0 = no usable features. */
  featureShift: number;
  /** Log-odds shift shared by every goal in this goal's budget, keeping that budget's total. */
  rebalance: number;
  contributions: Contribution[];
}

export interface GoalsResult<T extends GoalInput = GoalInput> {
  /**
   * The shared rebalancing shift (log-odds) with a pooled budget. null with a per-game budget,
   * where each game has its own: see RefinedGoal.rebalance.
   */
  alpha: number | null;
  totalBase: number;
  totalRefined: number;
  goals: RefinedGoal<T>[];
}

/** The features this goal's adjustment may use under the given options. */
export function usableFeatures(features: ScoredFeature[], opts: GoalOptions): ScoredFeature[] {
  return features.filter((f) => {
    if (f.source !== "post") return true;
    if (!opts.useReplay) return false;
    return !(opts.dropReplayFinish && FINISH_ON_REPLAY.has(f.code));
  });
}

export function refineGoals<T extends GoalInput>(goals: T[], opts: GoalOptions = DEFAULT_OPTIONS): GoalsResult<T> {
  const weights = opts.groupScale ? scaledWeights(opts.groupScale) : WEIGHTS;
  const adjusted = goals.map((g) =>
    adjust(g.x_goal, usableFeatures(g.features, opts), g.relevance, IDENTITY_CALIBRATION, weights));
  const baseLogits = goals.map((g) => logit(g.x_goal));
  const shifts = adjusted.map((a) => a.shift);
  const totalBase = goals.reduce((s, g) => s + g.x_goal, 0);

  // One budget for all goals, or one per game. Each budget gets its own rebalancing shift.
  const perGame = opts.budget === "game";
  const budgets = new Map<string, number[]>();
  goals.forEach((g, i) => {
    const key = perGame ? g.game_id : "";
    budgets.set(key, [...(budgets.get(key) ?? []), i]);
  });
  const rebalance = new Array<number>(goals.length).fill(0);
  for (const idx of budgets.values()) {
    const a = fitZeroSumAlpha(idx.map((i) => baseLogits[i]!), idx.map((i) => shifts[i]!),
      idx.reduce((s, i) => s + goals[i]!.x_goal, 0));
    for (const i of idx) rebalance[i] = a;
  }

  const refined = goals.map((g, i) => ({
    goal: g,
    xgBase: g.x_goal,
    xgRefined: sigmoid(rebalance[i]! + baseLogits[i]! + shifts[i]!),
    featureShift: shifts[i]!,
    rebalance: rebalance[i]!,
    contributions: adjusted[i]!.contributions,
  }));
  return {
    alpha: perGame ? null : (rebalance[0] ?? 0),
    totalBase,
    totalRefined: refined.reduce((s, r) => s + r.xgRefined, 0),
    goals: refined,
  };
}

/** Codes that actually moved this goal (non-zero, not dropped as contradictory or zero-weight). */
export const liveCodes = (r: RefinedGoal): Contribution[] =>
  r.contributions.filter((c) => !c.dropped && c.applied !== 0);

// ---------------------------------------------------------------------------------------------
// CLI

interface GoalRow extends GoalInput {
  shooter: string | null; team: string | null; period: number | null; clock: string | null;
}

/** All goals with a MoneyPuck xG and an annotation. Shared with the dashboard. */
export async function fetchGoals(): Promise<GoalRow[]> {
  const rows = await query<GoalRow>(
    `SELECT c.game_id, c.event_id, c.x_goal, COALESCE(c.relevance, 0) AS relevance,
            COALESCE(c.features, '[]'::jsonb) AS features,
            s.shooter, s.team, s.period, s.clock
       FROM commentary_xg c
       JOIN shot_commentary_split s USING (game_id, event_id)
      WHERE s.goal AND c.x_goal IS NOT NULL
      ORDER BY c.game_id, c.event_id`,
  );
  return rows.map((r) => ({ ...r, x_goal: Number(r.x_goal), relevance: Number(r.relevance) }));
}

const pct = (p: number) => `${(100 * p).toFixed(1)}%`.padStart(6);
const outDir = () => {
  const data = process.env.XG_DATA_DIR ?? "data";
  return join(isAbsolute(data) ? data : join(REPO_ROOT, data), "out");
};

function line(r: RefinedGoal<GoalRow>): string {
  const g = r.goal;
  const codes = liveCodes(r).map((c) => `${c.code}(${c.source})`).join(", ") || "-";
  return `  ${(g.shooter ?? "?").padEnd(22)} ${pct(r.xgBase)} -> ${pct(r.xgRefined)}   ${codes}`;
}

const csvCell = (v: unknown) => {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

async function main(): Promise<void> {
  loadEnv();
  await assertSchema();
  const goals = await fetchGoals();
  if (goals.length === 0) {
    console.log("No annotated goals with a MoneyPuck xG yet.");
    await close();
    return;
  }

  const res = refineGoals(goals);
  const withFeatures = (o: GoalOptions) => refineGoals(goals, o).goals.filter((r) => r.featureShift !== 0).length;
  const preOnly = withFeatures({ useReplay: false, dropReplayFinish: true });
  const replayAll = withFeatures({ useReplay: true, dropReplayFinish: false });
  const moved = res.goals.filter((r) => r.featureShift !== 0);

  console.log(`\n${goals.length} goals. MoneyPuck total ${res.totalBase.toFixed(2)}, refined total ${res.totalRefined.toFixed(2)}.`);
  console.log(`Rebalancing shift (log-odds, applied to every goal): ${(res.alpha ?? 0).toFixed(3)}.`);
  const perGame = refineGoals(goals, { ...DEFAULT_OPTIONS, budget: "game" });
  const gap = res.goals.map((r, i) => Math.abs(r.xgRefined - perGame.goals[i]!.xgRefined));
  console.log(`With a per-game budget instead, goals differ by ${(100 * gap.reduce((s, d) => s + d, 0) / gap.length).toFixed(1)} ` +
    `points on average (largest ${(100 * Math.max(...gap)).toFixed(1)}).`);
  console.log(`Goals moved by their own features: ${preOnly} using pre-shot commentary only, ` +
    `${moved.length} with the replay (${replayAll} if replay goalie-state features were kept).`);

  const byChange = [...res.goals].sort((a, b) => (b.xgRefined - b.xgBase) - (a.xgRefined - a.xgBase));
  console.log("\nBiggest rises:");
  byChange.slice(0, 8).forEach((r) => console.log(line(r)));
  console.log("\nBiggest falls:");
  byChange.slice(-5).reverse().forEach((r) => console.log(line(r)));

  const low = res.goals.filter((r) => r.xgBase < 0.05);
  const lowUp = low.filter((r) => r.xgRefined > r.xgBase);
  console.log(`\nGoals MoneyPuck rated under 5%: ${low.length}; the commentary raises ${lowUp.length} of them.`);

  const counts = new Map<string, number>();
  for (const r of moved) for (const c of liveCodes(r)) counts.set(c.code, (counts.get(c.code) ?? 0) + 1);
  console.log("\nFeatures that moved the most goals:");
  [...counts].sort((a, b) => b[1] - a[1]).slice(0, 10)
    .forEach(([code, n]) => console.log(`  ${code.padEnd(30)} ${n}`));

  const dir = outDir();
  mkdirSync(dir, { recursive: true });
  const header = ["game_id", "event_id", "shooter", "team", "period", "clock",
    "xg_moneypuck", "xg_refined", "change", "feature_shift", "features"];
  const body = byChange.map((r) => [
    r.goal.game_id, r.goal.event_id, r.goal.shooter, r.goal.team, r.goal.period, r.goal.clock,
    r.xgBase.toFixed(4), r.xgRefined.toFixed(4), (r.xgRefined - r.xgBase).toFixed(4),
    r.featureShift.toFixed(4), liveCodes(r).map((c) => `${c.code}:${c.source}`).join(" "),
  ].map(csvCell).join(","));
  const file = join(dir, "goals.csv");
  writeFileSync(file, [header.join(","), ...body].join("\n") + "\n");

  // The counts the slide figures need (scripts/goals_figures.py), so no number is retyped.
  const [shots] = await query<{ n: number }>(
    `SELECT COUNT(*)::int AS n FROM commentary_xg c JOIN shot_commentary_split s USING (game_id, event_id)
      WHERE c.x_goal IS NOT NULL AND s.goal IS NOT NULL`,
  );
  const summary = {
    shots: shots?.n ?? 0,
    games: new Set(goals.map((g) => g.game_id)).size,
    goals: goals.length,
    total_xg: Number(res.totalBase.toFixed(4)),
    moved_pre_only: preOnly,
    moved_with_replay: moved.length,
    long_shot_cutoff: 0.05,
    long_shots: low.length,
    long_shots_raised: lowUp.length,
    long_shots_now_above_cutoff: low.filter((r) => r.xgRefined >= 0.05).length,
    feature_counts: [...counts].sort((a, b) => b[1] - a[1])
      .map(([code, n]) => ({ code, label: featureLabel(code), goals: n })),
  };
  const summaryFile = join(dir, "goals_summary.json");
  writeFileSync(summaryFile, JSON.stringify(summary, null, 2) + "\n");
  console.log(`\nWrote ${file}\nWrote ${summaryFile}`);
  await close();
}

/** Run main() only when executed directly, so tests can import the functions above. */
const runningAsScript = (url: string): boolean => {
  const entry = process.argv[1];
  return !!entry && url === pathToFileURL(entry).href;
};

if (runningAsScript(import.meta.url)) {
  main().catch((e) => {
    console.error(e instanceof Error ? e.message : e);
    void close();
    process.exitCode = 1;
  });
}
