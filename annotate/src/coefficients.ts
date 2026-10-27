/**
 * Commentary features -> an xG adjustment, in log-odds.
 *
 * Why log-odds and not a multiplier on the probability: xG is a probability, so a flat multiplier
 * either pushes it past 1 at the top end or does almost nothing at the bottom. Adding in log-odds
 * is a constant effect on the *odds* at every base rate, which is how these effects actually
 * behave, and it can never leave [0, 1].
 *
 *   z = alpha + beta * logit(x_goal) + gate * sum_i (w_i * confidence_i)
 *   p = sigmoid(z)
 *
 * THE WEIGHTS BELOW ARE PRIORS, NOT MEASUREMENTS. They are set from what is publicly known about
 * shot quality in hockey and sized so that the big, well-evidenced effects (backdoor, breakaway,
 * a scrambling goalie) dominate the speculative ones. They are versioned so that calibrate.ts can
 * replace them with a fit once there are enough annotated shots to support one, and because
 * `features` is stored in the database, re-scoring against a new table costs nothing.
 */

import { EXCLUSIVE_GROUPS, featureDef } from "./vocabulary.ts";

/** Bump whenever a weight changes, so rows can be told apart and recomputed. */
export const COEFF_VERSION = "c1";

/** Log-odds added at confidence 1.0. */
export const WEIGHTS: Readonly<Record<string, number>> = {
  // A. Goalie sight line
  screen_opponent: 0.55,
  screen_own_defender: 0.6,
  traffic_heavy: 0.4,
  clear_sight: -0.4,
  // B. On the way to the net
  deflect_off_defender: 0.45,
  deflect_off_teammate: 0.7,
  partial_block: -0.55,
  shot_blocked_lane: -0.35,
  off_skate_or_body: 0.4,
  // C. Goalie state
  goalie_out_of_position: 0.75,
  goalie_down_scrambling: 0.9,
  goalie_set_square: -0.35,
  goalie_cold: 0.1,
  backup_just_in: 0.1,
  // D. Pre-shot movement
  cross_ice_pass: 0.6,
  one_timer: 0.35,
  behind_net_feed: 0.45,
  backdoor: 1.2,
  slot_feed: 0.35,
  no_pass_perimeter: -0.25,
  // E. Shooter state
  shooter_unpressured: 0.25,
  shooter_wide_open_slot: 0.5,
  quick_release: 0.3,
  shooter_pressured: -0.4,
  shooter_falling: -0.55,
  fanned_or_weak: -0.85,
  bouncing_puck: -0.3,
  broken_stick: -1.0,
  // F. Rare
  breakaway: 0.9,
  penalty_shot: 1.4,
  delayed_penalty_extra_attacker: 0.15,
  second_chance_scramble: 0.4,
  net_off_moorings: 0,
  puck_off_official: 0,
  // G. Data corrections: a quality signal, never an xG change.
  shot_type_correction: 0,
  shooter_correction: 0,
  location_dispute: 0,
  // H.
  other: 0,
};

/** No single shot may move by more than this in log-odds, however many features pile up. */
export const MAX_SHIFT = 2.0;
/** Below this relevance the commentary is not about this shot; the adjustment is dropped. */
export const RELEVANCE_FLOOR = 0.3;
const P_MIN = 0.002;
const P_MAX = 0.97;

export const logit = (p: number): number => {
  const q = Math.min(Math.max(p, 1e-4), 1 - 1e-4);
  return Math.log(q / (1 - q));
};
export const sigmoid = (z: number): number => 1 / (1 + Math.exp(-z));

export interface ScoredFeature {
  code: string;
  confidence: number;
  source: "pre" | "post";
  quote: string;
  correctedValue?: string | null;
}

export interface Calibration {
  alpha: number;
  beta: number;
  version: string;
}
export const IDENTITY_CALIBRATION: Calibration = { alpha: 0, beta: 1, version: "identity" };

export interface Contribution {
  code: string;
  confidence: number;
  source: "pre" | "post";
  weight: number;
  /** weight * confidence, before the gate and the cap. */
  raw: number;
  /** What actually reached the total, after gate, cap and exclusivity. */
  applied: number;
  dropped?: "exclusive" | "zero-weight";
}

export interface AdjustResult {
  xgBase: number;
  xgAdjusted: number;
  delta: number;
  deltaLogOdds: number;
  shift: number;
  gate: number;
  capped: boolean;
  contributions: Contribution[];
}

/**
 * Resolve features that contradict each other, keeping the more confident one. Done before
 * summing so that, say, screen_opponent@0.9 and clear_sight@0.3 give a screen rather than a
 * near-zero net effect that reads as "nothing unusual happened".
 */
function resolveExclusives(features: ScoredFeature[]): Set<number> {
  const dropped = new Set<number>();
  for (const group of EXCLUSIVE_GROUPS) {
    const present = features
      .map((f, i) => ({ f, i }))
      .filter(({ f, i }) => group.includes(f.code) && !dropped.has(i));
    if (present.length < 2) continue;
    present.sort((a, b) => b.f.confidence - a.f.confidence);
    for (const { i } of present.slice(1)) dropped.add(i);
  }
  return dropped;
}

/**
 * Apply the commentary features to MoneyPuck's xG.
 *
 * `relevance` gates everything: most commentary windows are pre-game filler or the call for a
 * different shot, so the honest default is no change at all.
 */
export function adjust(
  xgBase: number,
  features: ScoredFeature[],
  relevance: number,
  calib: Calibration = IDENTITY_CALIBRATION,
): AdjustResult {
  const gate = relevance < RELEVANCE_FLOOR ? 0 : relevance;
  const droppedIdx = resolveExclusives(features);

  const contributions: Contribution[] = features.map((f, i) => {
    const weight = WEIGHTS[f.code] ?? 0;
    const raw = weight * Math.min(Math.max(f.confidence, 0), 1);
    const c: Contribution = {
      code: f.code, confidence: f.confidence, source: f.source, weight, raw, applied: 0,
    };
    if (droppedIdx.has(i)) c.dropped = "exclusive";
    else if (weight === 0) c.dropped = "zero-weight";
    return c;
  });

  const live = contributions.filter((c) => !c.dropped);
  const rawSum = live.reduce((s, c) => s + c.raw, 0);
  const gated = rawSum * gate;
  const shift = Math.min(Math.max(gated, -MAX_SHIFT), MAX_SHIFT);
  const capped = Math.abs(gated) > MAX_SHIFT;
  // Share the cap proportionally so each line of the explanation sums to the total shown.
  const scale = rawSum === 0 ? 0 : shift / rawSum;
  for (const c of live) c.applied = c.raw * scale;

  // A no-op must be exactly a no-op. Round-tripping through logit/sigmoid returns 0.05 as
  // 0.05000000000000001, which would show up as a 1e-17 delta on every unadjusted row.
  const isIdentity = calib.alpha === 0 && calib.beta === 1;
  const z = calib.alpha + calib.beta * logit(xgBase) + shift;
  const xgAdjusted =
    shift === 0 && isIdentity
      ? Math.min(Math.max(xgBase, P_MIN), P_MAX)
      : Math.min(Math.max(sigmoid(z), P_MIN), P_MAX);

  return {
    xgBase,
    xgAdjusted,
    delta: xgAdjusted - xgBase,
    deltaLogOdds: shift === 0 && isIdentity ? 0 : z - logit(xgBase),
    shift,
    gate,
    capped,
    contributions,
  };
}

const pct = (p: number) => `${(p * 100).toFixed(1)}%`;
const signed = (n: number) => `${n >= 0 ? "+" : ""}${n.toFixed(2)}`;

/**
 * The human-readable reason, generated from the arithmetic rather than written by the model, so
 * the sentence can never disagree with the number it is explaining.
 */
export function explain(r: AdjustResult): string {
  if (r.gate === 0) {
    return `No change: the commentary in this window is not about this shot (relevance below ${RELEVANCE_FLOOR}).`;
  }
  const live = r.contributions.filter((c) => !c.dropped && c.raw !== 0);
  if (live.length === 0) {
    return "No change: nothing in the commentary adds to what the shot data already records.";
  }
  live.sort((a, b) => Math.abs(b.applied) - Math.abs(a.applied));
  const parts = live.map((c) => {
    const d = featureDef(c.code);
    return `${c.code} (${d?.polarity ?? "?"}, confidence ${c.confidence.toFixed(2)}, ${c.source}-shot) ${signed(c.applied)}`;
  });
  const bits = [
    `${pct(r.xgBase)} -> ${pct(r.xgAdjusted)} (${signed(r.delta * 100)} pts).`,
    `Log-odds ${signed(r.shift)} from: ${parts.join("; ")}.`,
  ];
  if (r.gate < 1) bits.push(`Scaled by relevance ${r.gate.toFixed(2)}.`);
  if (r.capped) bits.push(`Capped at +/-${MAX_SHIFT} log-odds.`);
  const dropped = r.contributions.filter((c) => c.dropped === "exclusive");
  if (dropped.length) {
    bits.push(`Ignored as contradictory: ${dropped.map((c) => c.code).join(", ")}.`);
  }
  return bits.join(" ");
}
