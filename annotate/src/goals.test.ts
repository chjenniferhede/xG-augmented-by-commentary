import { test } from "node:test";
import assert from "node:assert/strict";
import { refineGoals, usableFeatures, DEFAULT_OPTIONS, type GoalInput } from "./goals.ts";
import { logit, sigmoid, type ScoredFeature } from "./coefficients.ts";

const feat = (code: string, source: "pre" | "post", confidence = 1): ScoredFeature =>
  ({ code, source, confidence, quote: "" });

const goal = (id: string, x_goal: number, features: ScoredFeature[] = []): GoalInput =>
  ({ game_id: "g", event_id: id, x_goal, relevance: 1, features });

const sample: GoalInput[] = [
  goal("1", 0.05, [feat("backdoor", "post")]),
  goal("2", 0.10, [feat("screen_opponent", "pre")]),
  goal("3", 0.20, [feat("fanned_or_weak", "post")]),
  goal("4", 0.30),
  goal("5", 0.08, [feat("goalie_out_of_position", "post")]),
];

test("the refined total equals MoneyPuck's total over the same goals", () => {
  const res = refineGoals(sample);
  assert.ok(Math.abs(res.totalRefined - res.totalBase) < 1e-6, `${res.totalRefined} vs ${res.totalBase}`);
});

test("a goal with no features moves only by the shared rebalancing shift", () => {
  const res = refineGoals(sample);
  const plain = res.goals.find((r) => r.goal.event_id === "4")!;
  assert.equal(plain.featureShift, 0);
  assert.ok(Math.abs(plain.xgRefined - sigmoid(plain.rebalance + logit(0.3))) < 1e-12);
  assert.ok(res.goals.every((r) => r.rebalance === res.alpha), "a pooled budget shares one shift");
});

test("features that raise a goal push it up relative to an unadjusted goal", () => {
  const res = refineGoals(sample);
  const up = res.goals.find((r) => r.goal.event_id === "1")!;
  const down = res.goals.find((r) => r.goal.event_id === "3")!;
  assert.ok(up.xgRefined > up.xgBase);
  assert.ok(down.xgRefined < down.xgBase);
});

test("replay goalie-state features are ignored by default, but count when said before the shot", () => {
  const fs = [feat("goalie_out_of_position", "post"), feat("goalie_down_scrambling", "pre")];
  assert.deepEqual(usableFeatures(fs, DEFAULT_OPTIONS).map((f) => f.code), ["goalie_down_scrambling"]);
  assert.equal(usableFeatures(fs, { useReplay: true, dropReplayFinish: false }).length, 2);
  const res = refineGoals(sample);
  assert.equal(res.goals.find((r) => r.goal.event_id === "5")!.featureShift, 0);
});

test("turning the replay off leaves only pre-shot features", () => {
  const fs = [feat("backdoor", "post"), feat("screen_opponent", "pre")];
  assert.deepEqual(usableFeatures(fs, { useReplay: false, dropReplayFinish: true }).map((f) => f.code), ["screen_opponent"]);
});

test("scaling a group to zero removes its features' effect and keeps the total", () => {
  const res = refineGoals(sample, { ...DEFAULT_OPTIONS, groupScale: { D: 0 } });
  assert.equal(res.goals.find((r) => r.goal.event_id === "1")!.featureShift, 0); // backdoor is group D
  assert.ok(res.goals.find((r) => r.goal.event_id === "2")!.featureShift > 0);   // screen is group A
  assert.ok(Math.abs(res.totalRefined - res.totalBase) < 1e-6);
});

test("a per-game budget keeps each game's total and leaves a featureless game untouched", () => {
  const twoGames: GoalInput[] = [
    { ...goal("1", 0.05, [feat("backdoor", "post")]), game_id: "A" },
    { ...goal("2", 0.20), game_id: "A" },
    { ...goal("3", 0.10), game_id: "B" },
    { ...goal("4", 0.30), game_id: "B" },
  ];
  const res = refineGoals(twoGames, { ...DEFAULT_OPTIONS, budget: "game" });
  assert.equal(res.alpha, null);
  for (const game of ["A", "B"]) {
    const rs = res.goals.filter((r) => r.goal.game_id === game);
    const before = rs.reduce((s, r) => s + r.xgBase, 0);
    const after = rs.reduce((s, r) => s + r.xgRefined, 0);
    assert.ok(Math.abs(after - before) < 1e-6, `game ${game}: ${after} vs ${before}`);
  }
  // Game B has no features, so its goals don't pay for game A's back-door play.
  for (const r of res.goals.filter((r) => r.goal.game_id === "B")) {
    assert.ok(Math.abs(r.xgRefined - r.xgBase) < 1e-9);
  }
  // With one pooled budget they would.
  const pooled = refineGoals(twoGames);
  assert.ok(pooled.goals.find((r) => r.goal.event_id === "4")!.xgRefined < 0.3);
});

test("with no features anywhere, nothing moves", () => {
  const res = refineGoals([goal("a", 0.1), goal("b", 0.4)]);
  for (const r of res.goals) assert.ok(Math.abs(r.xgRefined - r.xgBase) < 1e-9);
});
