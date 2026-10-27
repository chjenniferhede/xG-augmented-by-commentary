import { test } from "node:test";
import assert from "node:assert/strict";
import { clusterShots } from "./cluster.ts";
import type { ShotRow } from "./db.ts";

const shot = (game: string, event: number, video_sec: number): ShotRow =>
  ({ game_id: game, event_id: String(event), video_sec } as ShotRow);

test("shots inside the gap group together", () => {
  // The real case from game 2016030245: events 4, 6 and 9 within ten seconds.
  const c = clusterShots([shot("g", 4, 38), shot("g", 6, 40), shot("g", 9, 48)]);
  assert.equal(c.length, 1);
  assert.deepEqual(c[0]!.map((s) => s.event_id), ["4", "6", "9"]);
});

test("a gap larger than the window splits the cluster", () => {
  const c = clusterShots([shot("g", 1, 10), shot("g", 2, 100)]);
  assert.equal(c.length, 2);
});

test("clusters never span games even when the times are close", () => {
  const c = clusterShots([shot("a", 1, 10), shot("b", 2, 11)]);
  assert.equal(c.length, 2);
});

test("the gap is measured from the previous shot, so a long chain still splits at the cap", () => {
  const chain = Array.from({ length: 12 }, (_, i) => shot("g", i, i * 5));
  const c = clusterShots(chain);
  assert.ok(c.every((x) => x.length <= 5), "no cluster may exceed the max size");
  assert.equal(c.reduce((s, x) => s + x.length, 0), 12, "every shot appears exactly once");
});

test("shots are ordered by video time within a cluster", () => {
  const c = clusterShots([shot("g", 9, 48), shot("g", 4, 38), shot("g", 6, 40)]);
  assert.deepEqual(c[0]!.map((s) => s.event_id), ["4", "6", "9"]);
});

test("an empty input yields no clusters", () => {
  assert.deepEqual(clusterShots([]), []);
});
