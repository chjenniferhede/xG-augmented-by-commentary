import { test } from "node:test";
import assert from "node:assert/strict";
import { chips } from "./chips.ts";
import type { ScoredFeature } from "../coefficients.ts";

const f = (code: string, source: "pre" | "post"): ScoredFeature =>
  ({ code, confidence: 0.8, source, quote: "some words" });

test("a shot with both kinds shows two labelled groups, before first", () => {
  const html = chips([f("screen_opponent", "post"), f("shooter_pressured", "pre")]);
  assert.ok(html.includes(">before<"), "missing the pre-shot group");
  assert.ok(html.includes(">replay<"), "missing the replay group");
  assert.ok(html.indexOf(">before<") < html.indexOf(">replay<"), "pre-shot must come first");
  // each code sits under its own heading
  const beforeBlock = html.slice(html.indexOf(">before<"), html.indexOf(">replay<"));
  assert.ok(beforeBlock.includes("shooter_pressured"));
  assert.ok(!beforeBlock.includes("screen_opponent"));
});

test("a group with nothing in it is omitted rather than left empty", () => {
  const html = chips([f("backdoor", "post")]);
  assert.ok(html.includes(">replay<"));
  assert.ok(!html.includes(">before<"));
});

test("filtering to one source drops the headings and the other source", () => {
  const html = chips([f("backdoor", "post"), f("shooter_pressured", "pre")], "pre");
  assert.ok(html.includes("shooter_pressured"));
  assert.ok(!html.includes("backdoor"));
  assert.ok(!html.includes("grp-k"), "a filtered list needs no heading");
});

test("no features renders nothing at all", () => {
  assert.equal(chips([]), "");
  assert.equal(chips(null), "");
  assert.equal(chips([f("backdoor", "post")], "pre"), "");
});

test("the weight is shown so a tag's direction is readable", () => {
  assert.ok(chips([f("backdoor", "pre")], "pre").includes("+1.2"));
  assert.ok(chips([f("fanned_or_weak", "pre")], "pre").includes("-0.85"));
  // A zero-weight code shows no weight at all, so it cannot read as "worth 0 points" --
  // the separator is the tell, since the confidence itself contains digits.
  const zero = chips([f("shot_type_correction", "pre")], "pre");
  assert.ok(zero.includes("shot_type_correction"));
  assert.ok(!zero.includes("\u00b7"), `zero-weight chip should carry no weight: ${zero}`);
});

test("quotes are escaped into the tooltip, not injected", () => {
  const evil: ScoredFeature = { code: "backdoor", confidence: 1, source: "pre", quote: '"><script>x' };
  const html = chips([evil], "pre");
  assert.ok(!html.includes("<script>"), "must not emit raw markup from model output");
  assert.ok(html.includes("&lt;script&gt;"));
});
