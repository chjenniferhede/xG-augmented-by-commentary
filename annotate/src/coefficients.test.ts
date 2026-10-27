import { test } from "node:test";
import assert from "node:assert/strict";
import { adjust, explain, logit, sigmoid, MAX_SHIFT, WEIGHTS, IDENTITY_CALIBRATION } from "./coefficients.ts";
import { FEATURES, EXCLUSIVE_GROUPS } from "./vocabulary.ts";

const f = (code: string, confidence = 1, source: "pre" | "post" = "pre") =>
  ({ code, confidence, source, quote: "q" }) as const;

test("every vocabulary code has a weight, and every weight a code", () => {
  for (const def of FEATURES) assert.ok(def.code in WEIGHTS, `no weight for ${def.code}`);
  const codes = new Set(FEATURES.map((d) => d.code));
  for (const code of Object.keys(WEIGHTS)) assert.ok(codes.has(code), `weight for unknown ${code}`);
});

test("weight signs match declared polarity", () => {
  for (const def of FEATURES) {
    const w = WEIGHTS[def.code]!;
    if (def.polarity === "positive") assert.ok(w > 0, `${def.code} should be > 0, is ${w}`);
    if (def.polarity === "negative") assert.ok(w < 0, `${def.code} should be < 0, is ${w}`);
    if (def.polarity === "neutral") assert.equal(w, 0, `${def.code} should be 0`);
  }
});

test("exclusive groups reference real codes", () => {
  const codes = new Set(FEATURES.map((d) => d.code));
  for (const g of EXCLUSIVE_GROUPS) for (const c of g) assert.ok(codes.has(c), `unknown ${c}`);
});

test("a screen on a 0.05 xG shot lands near 0.082", () => {
  const r = adjust(0.05, [f("screen_opponent")], 1);
  assert.equal(r.shift, 0.55);
  assert.ok(Math.abs(r.xgAdjusted - 0.0836) < 0.001, `got ${r.xgAdjusted}`);
  assert.ok(r.delta > 0);
});

test("a partial block on the same shot lands near 0.030", () => {
  const r = adjust(0.05, [f("partial_block")], 1);
  assert.ok(Math.abs(r.xgAdjusted - 0.0297) < 0.001, `got ${r.xgAdjusted}`);
});

test("relevance below the floor means no change at all", () => {
  const r = adjust(0.05, [f("backdoor"), f("breakaway")], 0.1);
  assert.equal(r.gate, 0);
  assert.equal(r.shift, 0);
  assert.equal(r.xgAdjusted, 0.05);
  assert.match(explain(r), /not about this shot/);
});

test("relevance scales the shift linearly above the floor", () => {
  const full = adjust(0.05, [f("screen_opponent")], 1);
  const half = adjust(0.05, [f("screen_opponent")], 0.5);
  assert.ok(Math.abs(half.shift - full.shift * 0.5) < 1e-12);
});

test("contradictory features resolve to the more confident one", () => {
  const r = adjust(0.05, [f("screen_opponent", 0.9), f("clear_sight", 0.3)], 1);
  const kept = r.contributions.filter((c) => !c.dropped).map((c) => c.code);
  assert.deepEqual(kept, ["screen_opponent"]);
  assert.ok(r.shift > 0, "should read as a screen, not cancel to ~zero");
  assert.match(explain(r), /Ignored as contradictory: clear_sight/);
});

test("the total shift is capped, and contributions still sum to it", () => {
  const many = ["backdoor", "breakaway", "penalty_shot", "goalie_down_scrambling", "cross_ice_pass"].map((c) => f(c));
  const r = adjust(0.05, many, 1);
  assert.ok(r.capped);
  assert.equal(r.shift, MAX_SHIFT);
  const summed = r.contributions.filter((c) => !c.dropped).reduce((s, c) => s + c.applied, 0);
  assert.ok(Math.abs(summed - r.shift) < 1e-9, `${summed} != ${r.shift}`);
});

test("probability stays in range for extreme inputs", () => {
  const hi = adjust(0.95, Array(5).fill(0).map(() => f("penalty_shot")), 1);
  const lo = adjust(0.001, Array(5).fill(0).map(() => f("broken_stick")), 1);
  assert.ok(hi.xgAdjusted <= 0.97 && hi.xgAdjusted > 0);
  assert.ok(lo.xgAdjusted >= 0.002 && lo.xgAdjusted < 1);
});

test("zero-weight codes are recorded but move nothing", () => {
  const r = adjust(0.05, [f("shot_type_correction"), f("other")], 1);
  assert.equal(r.shift, 0);
  assert.equal(r.xgAdjusted, 0.05);
  assert.equal(r.contributions.filter((c) => c.dropped === "zero-weight").length, 2);
});

test("no features means no change", () => {
  const r = adjust(0.123, [], 1);
  assert.equal(r.delta, 0);
  assert.match(explain(r), /nothing in the commentary adds/);
});

test("calibration alpha/beta shift the baseline", () => {
  const id = adjust(0.05, [], 1, IDENTITY_CALIBRATION);
  const shifted = adjust(0.05, [], 1, { alpha: 0.5, beta: 1, version: "t" });
  assert.ok(shifted.xgAdjusted > id.xgAdjusted);
});

test("logit and sigmoid round-trip", () => {
  for (const p of [0.01, 0.1, 0.5, 0.9]) assert.ok(Math.abs(sigmoid(logit(p)) - p) < 1e-9);
});

test("explanation always agrees with the number it explains", () => {
  const r = adjust(0.05, [f("screen_opponent", 0.8), f("one_timer", 0.6)], 0.9);
  const e = explain(r);
  assert.ok(e.includes((r.xgAdjusted * 100).toFixed(1)), e);
  assert.ok(e.includes(r.shift.toFixed(2)), e);
});
