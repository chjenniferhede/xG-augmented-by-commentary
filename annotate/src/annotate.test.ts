import { test } from "node:test";
import assert from "node:assert/strict";
import { verifyQuotes, isQuotaExhausted, quoteSource } from "./annotate.ts";
import type { ShotRow } from "./db.ts";
import type { ShotAnnotation } from "./schema.ts";

const shot = (pre: string, post = ""): ShotRow =>
  ({ commentary_pre: pre, commentary_post: post } as ShotRow);

const ann = (features: { code: string; quote: string; source?: "pre" | "post" }[]): ShotAnnotation =>
  ({
    event_id: 1, relevance: 0.9, attribution_confidence: 0.9, commentary_quality: "clear",
    outcome_leaked: false, leak_note: null, summary: "",
    features: features.map((f) => ({
      code: f.code, confidence: 0.8, source: f.source ?? "pre", quote: f.quote, corrected_value: null,
    })),
  }) as ShotAnnotation;

test("a quote that appears in the commentary is kept", () => {
  const s = shot("Everly trying to turn away from Kesler and gets a shot off");
  const { kept, rejected } = verifyQuotes(ann([{ code: "shooter_pressured", quote: "trying to turn away from Kesler" }]), s);
  assert.equal(kept.length, 1);
  assert.equal(rejected.length, 0);
});

test("an invented quote is rejected -- the main anti-hallucination guard", () => {
  const s = shot("A routine point shot, saved easily.");
  const { kept, rejected } = verifyQuotes(ann([{ code: "screen_opponent", quote: "two bodies in front, he never saw it" }]), s);
  assert.equal(kept.length, 0);
  assert.deepEqual(rejected.map((r) => r.code), ["screen_opponent"]);
});

test("punctuation and casing differences do not reject a real quote", () => {
  const s = shot("He NEVER saw it -- screened, by two!");
  const { kept } = verifyQuotes(ann([{ code: "screen_opponent", quote: "he never saw it, screened by two" }]), s);
  assert.equal(kept.length, 1);
});

test("a post-window quote is accepted for a post-sourced feature", () => {
  const s = shot("Carries it in.", "the bounce goes off of the Anaheim defender");
  const { kept } = verifyQuotes(
    ann([{ code: "off_skate_or_body", quote: "goes off of the Anaheim defender", source: "post" }]), s);
  assert.equal(kept.length, 1);
  assert.equal(kept[0]!.source, "post");
});

test("a pre-window quote the model tagged post is counted as pre", () => {
  // Measured case (2017030415/290): the pass was said before the shot, but tagged post, so it
  // moved only the with-replay number and the pre-shot xG never saw it.
  const s = shot("Here's Ovkin to the slot. Got it across.", "with Flurry down and taking away any angle");
  const { kept } = verifyQuotes(
    ann([{ code: "cross_ice_pass", quote: "Here's Ovkin to the slot. Got it across.", source: "post" }]), s);
  assert.equal(kept[0]!.source, "pre");
});

test("a post-window quote the model tagged pre is counted as post", () => {
  const s = shot("Carries it in.", "the bounce goes off of the Anaheim defender");
  const { kept } = verifyQuotes(
    ann([{ code: "off_skate_or_body", quote: "goes off of the Anaheim defender", source: "pre" }]), s);
  assert.equal(kept[0]!.source, "post", "replay talk must not reach the pre-shot number");
});

test("a quote that straddles the two windows is counted as post", () => {
  const s = shot("he walks in alone and", "fires it top shelf");
  const { kept } = verifyQuotes(
    ann([{ code: "shooter_unpressured", quote: "walks in alone and fires it", source: "pre" }]), s);
  assert.equal(kept[0]!.source, "post");
});

test("quoteSource prefers pre when the words appear in both windows", () => {
  assert.equal(quoteSource("nobody home", "backdoor nobody home", "replay nobody home"), "pre");
  assert.equal(quoteSource("nobody home", "a point shot", "replay nobody home"), "post");
  assert.equal(quoteSource("nobody home", "a point shot", "a save"), null);
});

test("a trivially short quote is rejected rather than matching by accident", () => {
  const s = shot("He shoots and it is wide of the net.");
  const { kept, rejected } = verifyQuotes(ann([{ code: "screen_opponent", quote: "he" }]), s);
  assert.equal(kept.length, 0);
  assert.equal(rejected.length, 1);
});

test("quota exhaustion is told apart from an ordinary failure", () => {
  assert.ok(isQuotaExhausted(new Error("You exceeded your current quota ... free_tier_requests, limit: 20")));
  assert.ok(isQuotaExhausted(new Error("429 RESOURCE_EXHAUSTED")));
  assert.equal(isQuotaExhausted(new Error("API key not valid")), false);
  assert.equal(isQuotaExhausted(new Error("503 model is overloaded")), false);
});

test("a leak note is only believed when its words are in the pre-shot window", () => {
  const s = shot("Pouliot winds up from the point.", "And he scores! Top corner.");
  const claimed = { ...ann([]), outcome_leaked: true, leak_note: "And he scores" };
  // The giveaway is in the POST window, which is by design, not a leak.
  const r = verifyQuotes(claimed as ShotAnnotation, s);
  assert.equal(r.leaked, false);
  assert.equal(r.leakNote, null);
});

test("a genuine pre-window leak is kept", () => {
  const s = shot("He shoots and Talbot's got that, easy save.", "Play continues.");
  const claimed = { ...ann([]), outcome_leaked: true, leak_note: "Talbot's got that" };
  const r = verifyQuotes(claimed as ShotAnnotation, s);
  assert.equal(r.leaked, true);
  assert.equal(r.leakNote, "Talbot's got that");
});

test("a paraphrased leak note is not believed", () => {
  const s = shot("He shoots from the slot.", "Saved.");
  const claimed = { ...ann([]), outcome_leaked: true, leak_note: "the commentator said it was saved" };
  assert.equal(verifyQuotes(claimed as ShotAnnotation, s).leaked, false);
});

test("importing this module must not start a run", () => {
  // This file imports from annotate.ts. Before the entry-point guard that import executed main(),
  // so running the tests annotated real shots against the database and the model. If this test
  // finishes at all the guard is holding; the assertion documents why it matters.
  assert.equal(typeof verifyQuotes, "function");
  assert.ok(!process.env.XG_ANNOTATE_RAN, "main() ran on import");
});

test("a correction that decodes a mangled name back to the recorded player is discarded", () => {
  const s = { ...shot("a drive by Benino that would not go"), shooter: "Nick Bonino" } as ShotRow;
  const a = ann([{ code: "shooter_correction", quote: "a drive by Benino" }]);
  a.features[0]!.corrected_value = "Nick Bonino";   // the roster let it resolve the name correctly
  const { kept, rejected } = verifyQuotes(a, s);
  assert.equal(kept.length, 0, "agreeing with the data is not a correction");
  assert.match(rejected[0]!.quote, /agrees with the data/);
});

test("a correction naming a genuinely different player is kept", () => {
  const s = { ...shot("rolled off the stick of Antoine Verment"), shooter: "Ryan Getzlaf" } as ShotRow;
  const a = ann([{ code: "shooter_correction", quote: "off the stick of Antoine Verment" }]);
  a.features[0]!.corrected_value = "Antoine Vermette";
  assert.equal(verifyQuotes(a, s).kept.length, 1);
});

test("a correction with no corrected value is discarded", () => {
  const s = { ...shot("Now Nurse on a backhand."), shot_type: "WRIST" } as ShotRow;
  const a = ann([{ code: "shot_type_correction", quote: "Now Nurse on a backhand" }]);
  const { kept, rejected } = verifyQuotes(a, s);
  assert.equal(kept.length, 0);
  assert.match(rejected[0]!.quote, /no corrected_value/);
});

test("a shot type correction that really disagrees survives", () => {
  const s = { ...shot("Now Nurse on a backhand."), shot_type: "WRIST" } as ShotRow;
  const a = ann([{ code: "shot_type_correction", quote: "Now Nurse on a backhand" }]);
  a.features[0]!.corrected_value = "BACKHAND";
  assert.equal(verifyQuotes(a, s).kept.length, 1);
});

test("a save by the goalie is not a blocked shooting lane", () => {
  const s = { ...shot("Cole a shot blocked away by Anderson as Malcin"),
              goalie: "Craig Anderson" } as ShotRow;
  const { kept, rejected } = verifyQuotes(
    ann([{ code: "shot_blocked_lane", quote: "Cole a shot blocked away by Anderson" }]), s);
  assert.equal(kept.length, 0);
  assert.match(rejected[0]!.quote, /names the goalie/);
});

test("the goalie's name survives mangling in the check", () => {
  const s = { ...shot("that one is blocked away by Talbert"), goalie: "Cam Talbot" } as ShotRow;
  assert.equal(verifyQuotes(
    ann([{ code: "partial_block", quote: "blocked away by Talbert" }]), s).kept.length, 0);
});

test("a genuine block by a skater is kept", () => {
  const s = { ...shot("His shot doesn't get through, Methot in the way"),
              goalie: "Craig Anderson" } as ShotRow;
  assert.equal(verifyQuotes(
    ann([{ code: "shot_blocked_lane", quote: "His shot doesn't get through" }]), s).kept.length, 1);
});

test("a screen may name the goalie -- only interception codes are checked", () => {
  const s = { ...shot("Anderson never saw it, screened by two"), goalie: "Craig Anderson" } as ShotRow;
  assert.equal(verifyQuotes(
    ann([{ code: "screen_opponent", quote: "Anderson never saw it, screened by two" }]), s).kept.length, 1);
});
