import { test } from "node:test";
import assert from "node:assert/strict";
import { brier, logLoss, reliability, fitZeroSumAlpha } from "./stats.ts";
import { logit, sigmoid } from "./coefficients.ts";

test("brier rewards a confident correct prediction", () => {
  assert.ok(brier([{ p: 0.9, goal: true }]) < brier([{ p: 0.5, goal: true }]));
  assert.equal(brier([{ p: 1, goal: true }]), 0);
});

test("log loss punishes confident wrong predictions hardest", () => {
  assert.ok(logLoss([{ p: 0.01, goal: true }]) > logLoss([{ p: 0.4, goal: true }]));
});

test("reliability bins partition the rows exactly once", () => {
  const rows = Array.from({ length: 50 }, (_, i) => ({ p: i / 50, goal: i % 7 === 0 }));
  const bins = reliability(rows);
  assert.equal(bins.reduce((s, b) => s + b.n, 0), rows.length);
});

test("zero-sum alpha makes the adjusted total match the baseline total", () => {
  const base = [0.02, 0.05, 0.1, 0.3, 0.6];
  const shifts = [0.5, 0.5, 0.5, 0.5, 0.5]; // all pushed up
  const zs = base.map(logit);
  const target = base.reduce((a, b) => a + b, 0);
  const alpha = fitZeroSumAlpha(zs, shifts, target);
  const got = zs.reduce((s, z, i) => s + sigmoid(alpha + z + shifts[i]!), 0);
  assert.ok(Math.abs(got - target) < 1e-6, `${got} vs ${target}`);
  assert.ok(alpha < 0, "upward shifts must be offset by a negative intercept");
});

test("zero-sum alpha is zero when nothing moved", () => {
  const base = [0.02, 0.05, 0.1];
  const zs = base.map(logit);
  const alpha = fitZeroSumAlpha(zs, [0, 0, 0], base.reduce((a, b) => a + b, 0));
  assert.ok(Math.abs(alpha) < 1e-6);
});
