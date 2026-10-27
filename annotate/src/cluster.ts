/**
 * Group shots whose commentary windows overlap into a single request.
 *
 * Consecutive shots are often seconds apart -- in game 2016030245, events 4, 6 and 9 fall within
 * ten seconds and share almost identical commentary. Annotating them separately asks the model
 * the same question three times (three times the tokens, which on a free tier is the binding
 * cost) and invites it to pin one backdoor chance on all three. Sent together, it has to decide
 * which shot each detail belongs to.
 */

import type { ShotRow } from "./db.ts";

/** Shots closer than this in video time share enough commentary to be judged together. */
export const CLUSTER_GAP_SEC = 20;
/** Beyond this the prompt gets unwieldy and attribution degrades. */
export const MAX_CLUSTER = 5;

export function clusterShots(
  shots: ShotRow[],
  gapSec = CLUSTER_GAP_SEC,
  maxSize = MAX_CLUSTER,
): ShotRow[][] {
  const byGame = new Map<string, ShotRow[]>();
  for (const s of shots) {
    const g = byGame.get(s.game_id) ?? [];
    g.push(s);
    byGame.set(s.game_id, g);
  }

  const clusters: ShotRow[][] = [];
  for (const group of byGame.values()) {
    group.sort((a, b) => a.video_sec - b.video_sec);
    let current: ShotRow[] = [];
    for (const shot of group) {
      const prev = current[current.length - 1];
      const contiguous = prev !== undefined && shot.video_sec - prev.video_sec <= gapSec;
      if (!contiguous || current.length >= maxSize) {
        if (current.length) clusters.push(current);
        current = [];
      }
      current.push(shot);
    }
    if (current.length) clusters.push(current);
  }
  return clusters;
}
