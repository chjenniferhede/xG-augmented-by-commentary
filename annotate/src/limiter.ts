/**
 * Pacing and budget for a run that may span days.
 *
 * Free tiers publish little and enforce a lot, so this does not trust a configured number: it
 * spaces requests at the configured RPM, backs off when the provider says 429 (honouring
 * retry-after when present), and stops the run cleanly when the day's token budget is spent so
 * the next run can pick up where it left off.
 */

export class RateLimiter {
  private lastRequest = 0;
  private readonly minGapMs: number;
  private tokensUsed = 0;
  private consecutive429 = 0;
  private readonly tokensPerDay: number;

  // Plain fields, not constructor parameter properties: Node's --experimental-strip-types
  // cannot compile those, and the unit tests run under it.
  constructor(rpm: number, tokensPerDay: number) {
    this.minGapMs = rpm > 0 ? Math.ceil(60_000 / rpm) : 0;
    this.tokensPerDay = tokensPerDay;
  }

  get used(): number {
    return this.tokensUsed;
  }

  budgetExhausted(): boolean {
    return this.tokensPerDay > 0 && this.tokensUsed >= this.tokensPerDay;
  }

  record(tokens: number): void {
    this.tokensUsed += tokens;
  }

  async pace(): Promise<void> {
    const wait = this.lastRequest + this.minGapMs - Date.now();
    if (wait > 0) await sleep(wait);
    this.lastRequest = Date.now();
  }

  /**
   * Run `fn`, retrying rate-limit and transient server errors with exponential backoff.
   * Anything else (a bad key, a rejected schema) is thrown straight through: retrying it would
   * just burn the day's budget on the same failure.
   */
  async run<T>(fn: () => Promise<T>, maxRetries = 5, retryable?: (err: unknown) => boolean): Promise<T> {
    for (let attempt = 0; ; attempt++) {
      await this.pace();
      try {
        const out = await fn();
        this.consecutive429 = 0;
        return out;
      } catch (err) {
        const status = statusOf(err);
        // A day-long quota refusal is not a transient rate limit: backing off and retrying just
        // wastes minutes before the caller can rotate to another model.
        const worthRetrying =
          (retryable?.(err) ?? true) && (status === 429 || (status !== undefined && status >= 500));
        if (!worthRetrying || attempt >= maxRetries) throw err;
        if (status === 429) this.consecutive429++;
        const hinted = retryAfterMs(err);
        const backoff = hinted ?? Math.min(60_000, 2_000 * 2 ** attempt) + Math.random() * 1_000;
        console.warn(
          `  rate limited (HTTP ${status}); waiting ${Math.round(backoff / 1000)}s ` +
            `[attempt ${attempt + 1}/${maxRetries}]`,
        );
        await sleep(backoff);
      }
    }
  }

  /** Many 429s in a row means the configured RPM is wrong for this account. */
  get looksThrottled(): boolean {
    return this.consecutive429 >= 3;
  }
}

export const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

function statusOf(err: unknown): number | undefined {
  if (typeof err !== "object" || err === null) return undefined;
  const e = err as Record<string, unknown>;
  for (const key of ["statusCode", "status"]) {
    const v = e[key];
    if (typeof v === "number") return v;
  }
  const nested = e["cause"];
  return nested && nested !== err ? statusOf(nested) : undefined;
}

function retryAfterMs(err: unknown): number | undefined {
  if (typeof err !== "object" || err === null) return undefined;
  const headers = (err as { responseHeaders?: Record<string, string> }).responseHeaders;
  const raw = headers?.["retry-after"];
  if (!raw) return undefined;
  const seconds = Number(raw);
  return Number.isFinite(seconds) ? seconds * 1000 : undefined;
}
