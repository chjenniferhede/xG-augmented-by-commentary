/**
 * Score the extraction against shots you labelled by hand.
 *
 *   npm run eval-gold -- --export 30     write a template to fill in
 *   npm run eval-gold                    score the current annotations against it
 *   npm run eval-gold -- --provider groq re-annotate the gold shots elsewhere first
 *
 * Why this exists: the extraction runs on whichever free model is available, and "it looks
 * about right" is not a way to choose between them. With a labelled set, swapping provider or
 * editing the prompt becomes a measurement -- per-feature precision and recall, plus how close
 * the relevance judgement is.
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { join } from "node:path";
import { assertSchema, close, loadEnv, query, REPO_ROOT } from "./db.ts";
import type { ScoredFeature } from "./coefficients.ts";

const GOLD = join(REPO_ROOT, "annotate", "gold", "gold.json");

interface GoldShot {
  game_id: string; event_id: string; shooter: string | null;
  youtube_url: string; commentary_pre: string; commentary_post: string;
  /** Fill in by hand: the codes that genuinely apply. [] is a valid and common answer. */
  expected_features: string[];
  /** Fill in by hand: is the commentary about this shot at all? */
  expected_relevant: boolean | null;
  notes?: string;
}

async function exportTemplate(n: number): Promise<void> {
  const rows = await query<GoldShot>(
    `SELECT game_id, event_id, shooter, link AS youtube_url,
            commentary_pre, commentary_post
       FROM shot_commentary_split
      WHERE commentary_pre IS NOT NULL
      ORDER BY md5(game_id::text || event_id::text)
      LIMIT $1`,
    [n],
  );
  const out = rows.map((r) => ({ ...r, expected_features: [], expected_relevant: null,
                                 notes: "watch the link, then fill expected_features and expected_relevant" }));
  mkdirSync(join(REPO_ROOT, "annotate", "gold"), { recursive: true });
  writeFileSync(GOLD, JSON.stringify(out, null, 2));
  console.log(
    `Wrote ${out.length} shots to annotate/gold/gold.json.\n` +
      "Open each youtube_url, decide which feature codes truly apply, and fill in\n" +
      "expected_features (often []) and expected_relevant. Then run: make eval-gold",
  );
}

interface Counts { tp: number; fp: number; fn: number }
const f1 = (c: Counts) => {
  const p = c.tp + c.fp ? c.tp / (c.tp + c.fp) : 0;
  const r = c.tp + c.fn ? c.tp / (c.tp + c.fn) : 0;
  return { p, r, f1: p + r ? (2 * p * r) / (p + r) : 0 };
};
const f2 = (n: number) => (Number.isFinite(n) ? n.toFixed(2) : " n/a");

async function score(): Promise<void> {
  if (!existsSync(GOLD)) {
    console.log("No annotate/gold/gold.json yet. Create one with:\n  npm run eval-gold -- --export 30");
    return;
  }
  const gold: GoldShot[] = JSON.parse(readFileSync(GOLD, "utf8"));
  const labelled = gold.filter((g) => g.expected_relevant !== null);
  if (!labelled.length) {
    console.log(`annotate/gold/gold.json has ${gold.length} shots but none are labelled yet.`);
    return;
  }

  const rows = await query<{ game_id: string; event_id: string; features: ScoredFeature[] | null; relevance: number }>(
    `SELECT game_id, event_id, features, relevance FROM commentary_xg`,
  );
  const got = new Map(rows.map((r) => [`${r.game_id}/${r.event_id}`, r]));

  const per = new Map<string, Counts>();
  let relAgree = 0, scored = 0, missing = 0;
  for (const g of labelled) {
    const r = got.get(`${g.game_id}/${g.event_id}`);
    if (!r) { missing++; continue; }
    scored++;
    const predicted = new Set((r.features ?? []).map((f) => f.code));
    const expected = new Set(g.expected_features);
    for (const code of new Set([...predicted, ...expected])) {
      const c = per.get(code) ?? { tp: 0, fp: 0, fn: 0 };
      if (predicted.has(code) && expected.has(code)) c.tp++;
      else if (predicted.has(code)) c.fp++;
      else c.fn++;
      per.set(code, c);
    }
    if ((r.relevance >= 0.3) === g.expected_relevant) relAgree++;
  }

  if (!scored) {
    console.log(`None of the ${labelled.length} labelled shots are annotated yet. Run make annotate first.`);
    return;
  }

  const total = [...per.values()].reduce(
    (a, c) => ({ tp: a.tp + c.tp, fp: a.fp + c.fp, fn: a.fn + c.fn }), { tp: 0, fp: 0, fn: 0 },
  );
  const o = f1(total);
  console.log(`\nScored ${scored} of ${labelled.length} labelled shots` + (missing ? ` (${missing} not annotated yet)` : "") + ".");
  console.log(`Relevance agreement: ${relAgree}/${scored} (${((100 * relAgree) / scored).toFixed(0)}%)`);
  console.log(`\nFeature extraction overall: precision ${f2(o.p)}  recall ${f2(o.r)}  F1 ${f2(o.f1)}`);
  console.log(`  ${total.tp} correct, ${total.fp} invented, ${total.fn} missed\n`);
  console.log(`  ${"code".padEnd(32)} ${"tp".padStart(4)} ${"fp".padStart(4)} ${"fn".padStart(4)}  prec  rec`);
  for (const [code, c] of [...per].sort((a, b) => b[1].tp + b[1].fn - (a[1].tp + a[1].fn))) {
    const m = f1(c);
    console.log(`  ${code.padEnd(32)} ${String(c.tp).padStart(4)} ${String(c.fp).padStart(4)} ${String(c.fn).padStart(4)}  ${f2(m.p)}  ${f2(m.r)}`);
  }
  if (total.fp > total.tp) {
    console.log("\nMore invented features than correct ones: tighten the prompt or raise the confidence bar.");
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
  await assertSchema();
  const i = process.argv.indexOf("--export");
  if (i !== -1) await exportTemplate(Number(process.argv[i + 1] ?? 30));
  else await score();
  await close();
}

if (runningAsScript(import.meta.url)) {
  main().catch((e) => {
    console.error(e instanceof Error ? e.message : e);
    void close();
    process.exitCode = 1;
  });
}
