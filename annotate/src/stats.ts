/**
 * Scoring the adjusted xG against what actually happened.
 *
 * Only xg_adjusted_pre is scored. xg_adjusted_full has seen the replay talk, which is downstream
 * of the result, so comparing it to the result would flatter it for no good reason.
 */

export interface Scored { p: number; goal: boolean }

export const brier = (rows: Scored[]): number =>
  rows.length ? rows.reduce((s, r) => s + (r.p - (r.goal ? 1 : 0)) ** 2, 0) / rows.length : NaN;

export const logLoss = (rows: Scored[]): number => {
  if (!rows.length) return NaN;
  const eps = 1e-6;
  return (
    -rows.reduce((s, r) => {
      const p = Math.min(Math.max(r.p, eps), 1 - eps);
      return s + (r.goal ? Math.log(p) : Math.log(1 - p));
    }, 0) / rows.length
  );
};

export interface Bin { bin: number; lo: number; hi: number; n: number; meanPred: number; observed: number }

/** Equal-width reliability bins. Sparse at the top end -- most shots are low-danger. */
export function reliability(rows: Scored[], bins = 10): Bin[] {
  const out: Bin[] = [];
  for (let i = 0; i < bins; i++) {
    const lo = i / bins;
    const hi = (i + 1) / bins;
    const inBin = rows.filter((r) => r.p >= lo && (i === bins - 1 ? r.p <= hi : r.p < hi));
    out.push({
      bin: i, lo, hi, n: inBin.length,
      meanPred: inBin.length ? inBin.reduce((s, r) => s + r.p, 0) / inBin.length : NaN,
      observed: inBin.length ? inBin.filter((r) => r.goal).length / inBin.length : NaN,
    });
  }
  return out;
}

/**
 * Fit the intercept so the adjusted probabilities sum to the same total as the baseline.
 *
 * This keeps MoneyPuck's calibration intact: the commentary redistributes expected goals between
 * shots rather than inventing them. Solved by bisection on alpha -- the sum is monotone in it,
 * and with a few hundred rows this is instant and has no dependencies.
 */
export function fitZeroSumAlpha(
  baseLogits: number[], shifts: number[], targetSum: number,
): number {
  const sumAt = (alpha: number) =>
    baseLogits.reduce((s, z, i) => s + 1 / (1 + Math.exp(-(alpha + z + (shifts[i] ?? 0)))), 0);
  let lo = -5, hi = 5;
  if (sumAt(lo) > targetSum || sumAt(hi) < targetSum) return 0; // target unreachable; stay identity
  for (let i = 0; i < 80; i++) {
    const mid = (lo + hi) / 2;
    if (sumAt(mid) < targetSum) lo = mid;
    else hi = mid;
  }
  return (lo + hi) / 2;
}
