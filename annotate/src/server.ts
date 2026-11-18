/**
 * The xG dashboard, on its own port so the Flask ops dashboard at :5057 stays what it is.
 *
 *   npm run dash        -> http://127.0.0.1:5058
 *
 * Server-rendered: every page is one SQL query and some HTML, which keeps it honest about what
 * is actually in the database and means it works with JavaScript off.
 */

import { serve } from "@hono/node-server";
import { pathToFileURL } from "node:url";
import { Hono } from "hono";
import { assertSchema, loadEnv, query } from "./db.ts";
import { esc, page } from "./views/layout.ts";
import { deltaBar, featureTable, reliabilityChart, type RelBin } from "./views/charts.ts";
import { chips } from "./views/chips.ts";
import { COEFF_VERSION, logit, sigmoid, WEIGHTS } from "./coefficients.ts";
import { featureDef, FEATURES } from "./vocabulary.ts";
import type { ScoredFeature } from "./coefficients.ts";
import { fetchGoals, GROUPS, liveCodes, refineGoals, usableFeatures, type RefinedGoal } from "./goals.ts";
import { featureLabel } from "./views/labels.ts";

const app = new Hono();

/**
 * The moment, embedded.
 *
 * A watch-page link carries the time in `t=`, but YouTube only acts on it when the page loads
 * fresh: if that video is already open the player keeps its own position and the link appears to
 * do nothing. The embed player takes `start=` and always honours it, so the detail page plays
 * the moment itself rather than relying on a link that may be ignored.
 *
 * youtube-nocookie.com is the same player without the tracking cookies, which suits a project
 * that keeps its YouTube data to itself.
 */
const clip = (videoId: string, atSec: number, label: string): string => {
  const start = Math.max(Math.trunc(atSec) - 5, 0); // 5 s early, for the build-up
  return `<div class="clip"><iframe
      src="https://www.youtube-nocookie.com/embed/${esc(videoId)}?start=${start}&rel=0"
      title="${esc(label)}" loading="lazy" allowfullscreen
      allow="accelerometer; clipboard-write; encrypted-media; picture-in-picture"></iframe></div>`;
};

const pct = (n: number | null | undefined, d = 1) =>
  n === null || n === undefined ? "–" : `${(n * 100).toFixed(d)}%`;
const signedPts = (n: number | null | undefined) =>
  n === null || n === undefined ? "–" : `${n >= 0 ? "+" : "−"}${(Math.abs(n) * 100).toFixed(1)}`;

interface ListRow {
  game_id: string; event_id: string; shooter: string | null; result: string; period: number;
  clock: string; team: string | null; x_goal: number; xg_adjusted_pre: number;
  xg_adjusted_full: number; delta_pre: number; delta_full: number; relevance: number;
  summary: string | null; features: ScoredFeature[] | null; youtube_url: string;
  outcome_leaked: boolean; video_id: string; video_time: string;
  seq_pos?: number; seq_len?: number; seq_span?: number;
}


/**
 * Label runs of shots that happen within a few seconds of each other.
 *
 * Hockey produces genuine scrambles -- a shot, a rebound, another rebound, all inside two
 * seconds -- and their video links are then nearly identical. Without a marker those rows read
 * as duplicated data. This is gaps-and-islands over video_sec: a gap longer than SEQ_GAP starts
 * a new sequence, and each row learns its position in its own.
 */
const SEQ_GAP = 6;
const SEQ_CTE = `
  WITH marked AS (
    SELECT game_id, event_id, video_sec,
           CASE WHEN video_sec - LAG(video_sec) OVER w > ${SEQ_GAP}
                  OR LAG(video_sec) OVER w IS NULL THEN 1 ELSE 0 END AS starts
      FROM shot_commentary_split
    WINDOW w AS (PARTITION BY game_id ORDER BY video_sec)
  ), grouped AS (
    SELECT game_id, event_id, video_sec,
           SUM(starts) OVER (PARTITION BY game_id ORDER BY video_sec
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS grp
      FROM marked
  ), seq AS (
    SELECT game_id, event_id,
           ROW_NUMBER() OVER (PARTITION BY game_id, grp ORDER BY video_sec) AS seq_pos,
           COUNT(*)    OVER (PARTITION BY game_id, grp) AS seq_len,
           (MAX(video_sec) OVER (PARTITION BY game_id, grp)
            - MIN(video_sec) OVER (PARTITION BY game_id, grp)) AS seq_span
      FROM grouped
  )`;

/** "2nd of 3 shots in 2s" -- so a repeated timestamp reads as a scramble, not a glitch. */
const seqBadge = (pos: number, len: number, span: number): string => {
  if (!len || len < 2) return "";
  const nth = ["", "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th"][pos] ?? `${pos}th`;
  return `<span class="seq" title="A scramble: ${len} shots within ${Math.round(span)} seconds, so these links all point to nearly the same moment.">${nth} of ${len} in ${Math.round(span)}s</span>`;
};

app.get("/", async (c) => {
  const sort = c.req.query("sort") ?? "delta";
  const game = c.req.query("game") ?? "";
  const dir = c.req.query("dir") ?? "";
  const order =
    { pre: "abs(c.delta_pre) DESC", delta: "abs(c.delta_full) DESC",
      up: "c.delta_full DESC", down: "c.delta_full ASC",
      xg: "c.x_goal DESC", time: "c.game_id, s.video_sec" }[sort] ?? "abs(c.delta_full) DESC";
  // The bar tracks whatever is being sorted on, so the picture matches the ordering.
  const barOnPre = sort === "pre";

  const where: string[] = [];
  const params: unknown[] = [];
  if (game) { params.push(game); where.push(`c.game_id = $${params.length}`); }
  if (dir === "changed") {
    params.push(WEIGHTED_CODES);
    where.push(`EXISTS (SELECT 1 FROM jsonb_array_elements(c.features) f
                         WHERE f->>'code' = ANY($${params.length}))`);
  }

  const rows = await query<ListRow>(
    `${SEQ_CTE}
     SELECT c.game_id, c.event_id, s.shooter, s.result, s.period, s.clock, s.team,
            c.x_goal, c.xg_adjusted_pre, c.xg_adjusted_full, c.delta_pre, c.delta_full,
            c.relevance, c.summary, c.features, c.youtube_url, c.outcome_leaked,
            c.video_id, s.video_time,
            q.seq_pos, q.seq_len, q.seq_span
       FROM commentary_xg c
       JOIN shot_commentary_split s USING (game_id, event_id)
       JOIN seq q USING (game_id, event_id)
      ${where.length ? `WHERE ${where.join(" AND ")}` : ""}
      ORDER BY ${order} LIMIT 400`,
    params,
  );
  const games = await query<{ game_id: string; n: number }>(
    `SELECT game_id, COUNT(*)::int AS n FROM commentary_xg GROUP BY game_id ORDER BY game_id`,
  );
  const maxDelta = Math.max(...rows.map((r) => Math.abs(barOnPre ? r.delta_pre : r.delta_full)), 0.01);

  const link = (s: string, label: string) =>
    `<a href="?sort=${s}${game ? `&game=${game}` : ""}${dir ? `&dir=${dir}` : ""}">${label}</a>`;

  const body = `
  <form class="filters" method="get">
    <select name="game"><option value="">All games</option>
      ${games.map((g) => `<option value="${g.game_id}"${g.game_id === game ? " selected" : ""}>${g.game_id} (${g.n})</option>`).join("")}
    </select>
    <select name="sort">
      ${[["delta", "Biggest change (with replay)"], ["pre", "Biggest change (pre-shot only)"],
         ["up", "Most raised"], ["down", "Most lowered"],
         ["xg", "Highest xG"], ["time", "Chronological"]]
        .map(([v, l]) => `<option value="${v}"${v === sort ? " selected" : ""}>${l}</option>`).join("")}
    </select>
    <select name="dir">
      <option value="">All shots</option>
      <option value="changed"${dir === "changed" ? " selected" : ""}>Only adjusted</option>
    </select>
    <button type="submit">Apply</button>
    <span class="muted">${rows.length} shown</span>
  </form>
  <div class="card">
  ${rows.length === 0 ? `<p class="empty">No annotations yet. Run <code>make annotate</code>.</p>` : `
  <table>
    <thead><tr>
      <th>Shot</th>
      <th class="num">${link("xg", "MoneyPuck xG")}</th>
      <th class="num" title="Uses only what was said before the shot. The one that can be scored against real goals.">
        ${link("pre", "Pre-shot ▾")}</th>
      <th class="num" title="Also uses the replay talk, where most screen and deflection detail lives. Richer, but it has seen the outcome.">
        ${link("delta", "With replay ▾")}</th>
      <th>${barOnPre ? "Δ pre-shot" : "Δ with replay"}</th>
      <th class="hide-sm">Why</th><th></th>
    </tr></thead>
    <tbody>
    ${rows.map((r) => `<tr>
      <td><a href="/shot/${r.game_id}/${r.event_id}"><strong>${esc(r.shooter ?? "unknown")}</strong></a>
          <div class="muted">${esc(r.team ?? "")} · P${r.period} ${esc(r.clock)} · ${esc(r.result)}
            ${seqBadge(r.seq_pos ?? 0, r.seq_len ?? 0, r.seq_span ?? 0)}</div></td>
      <td class="num">${pct(r.x_goal)}</td>
      <td class="num">${pct(r.xg_adjusted_pre)}
          <div class="muted">${signedPts(r.delta_pre)}</div></td>
      <td class="num">${pct(r.xg_adjusted_full)}
          <div class="muted">${signedPts(r.delta_full)}</div></td>
      <td>${deltaBar(barOnPre ? r.delta_pre : r.delta_full, maxDelta)}</td>
      <td class="hide-sm">${esc(r.summary ?? "")}<div>${chips(r.features)}</div></td>
      <td><a href="/shot/${r.game_id}/${r.event_id}" title="play this moment in the dashboard">▶</a>
          <div class="muted at">${esc(r.video_time ?? "")}</div></td>
    </tr>`).join("")}
    </tbody>
  </table>`}
  </div>`;
  return c.html(page("Shots – commentary xG", "/", body));
});

app.get("/shot/:game/:event", async (c) => {
  const rows = await query<ListRow & {
    explanation: string | null; commentary_pre: string | null; commentary_post: string | null;
    model: string | null; provider: string | null; coeff_version: string | null; video_sec: number;
    video_id: string;
    attribution_confidence: number | null; commentary_quality: string | null;
    leak_note: string | null; video_time: string;
  }>(
    `SELECT c.*, s.shooter, s.result, s.period, s.clock, s.team, s.video_time, s.video_sec
       FROM commentary_xg c JOIN shot_commentary_split s USING (game_id, event_id)
      WHERE c.game_id = $1 AND c.event_id = $2`,
    [c.req.param("game"), c.req.param("event")],
  );
  const r = rows[0];
  if (!r) return c.html(page("Not found", "/", `<p class="empty">No annotation for that shot.</p>`), 404);

  // The other shots in the same scramble, so a near-identical timestamp is explained in place.
  const neighbours = await query<{
    game_id: string; event_id: string; shooter: string | null; result: string; video_sec: number;
  }>(
    `SELECT game_id, event_id, shooter, result, video_sec FROM shot_commentary_split
      WHERE game_id = $1 AND abs(video_sec - $2) <= ${SEQ_GAP} ORDER BY video_sec`,
    [r.game_id, r.video_sec],
  );

  // Highlight the exact words each feature was drawn from.
  const highlight = (text: string | null, source: "pre" | "post") => {
    let out = esc(text ?? "(none)");
    for (const f of (r.features ?? []).filter((x) => x.source === source)) {
      const q = esc(f.quote);
      if (q.length > 6 && out.includes(q)) out = out.split(q).join(`<mark>${q}</mark>`);
    }
    return out;
  };

  const body = `
  <div class="card">
    <h2>${esc(r.shooter ?? "unknown")} · ${esc(r.team ?? "")} · P${r.period} ${esc(r.clock)} · ${esc(r.result)}</h2>
    <div class="tiles">
      <div class="tile"><div class="k">MoneyPuck xG</div><div class="v">${pct(r.x_goal)}</div></div>
      <div class="tile"><div class="k">Adjusted (pre-shot)</div><div class="v">${pct(r.xg_adjusted_pre)}</div>
        <div class="sub">${signedPts(r.delta_pre)} pts · the honest one</div></div>
      <div class="tile"><div class="k">Adjusted (incl. replay)</div><div class="v">${pct(r.xg_adjusted_full)}</div>
        <div class="sub">${signedPts(r.delta_full)} pts</div></div>
      <div class="tile"><div class="k">Relevance</div><div class="v">${r.relevance?.toFixed(2) ?? "–"}</div>
        <div class="sub">${esc(r.commentary_quality ?? "")}</div></div>
    </div>
    ${clip(r.video_id, r.video_sec, `${r.shooter ?? "shot"} at ${r.video_time}`)}
    <p class="muted">Starts 5 s before the shot, at ${esc(r.video_time)} of the replay.
       <a href="${esc(r.youtube_url)}" target="_blank" rel="noreferrer">Open on YouTube</a>
       — note that if this video is already open in another tab, YouTube keeps its own position
       and ignores the timestamp; the player above always starts in the right place.</p>
    ${r.outcome_leaked ? `<p class="muted">⚠ The pre-shot window gave the result away: ${esc(r.leak_note ?? "")}</p>` : ""}
    ${neighbours.length > 1 ? `<p class="muted">Part of a scramble — ${neighbours.length} shots within
       ${Math.round((neighbours[neighbours.length - 1]!.video_sec) - (neighbours[0]!.video_sec))}s,
       so their links all land in the same place:
       ${neighbours.map((x) => x.event_id === r.event_id
          ? `<strong>${esc(x.shooter ?? "?")} (${esc(x.result)})</strong>`
          : `<a href="/shot/${x.game_id}/${x.event_id}">${esc(x.shooter ?? "?")} (${esc(x.result)})</a>`
        ).join(" → ")}</p>` : ""}
  </div>

  <div class="card"><h2>Why</h2>
    <p>${esc(r.summary ?? "")}</p>
    <p class="muted"><code>${esc(r.explanation ?? "")}</code></p>
    <div>${chips(r.features)}</div>
  </div>

  <div class="card"><h2>Commentary before the shot — drives the pre-shot number</h2>
    <div class="quote">${highlight(r.commentary_pre, "pre")}</div></div>

  <div class="card"><h2>Commentary after — replay talk; the result itself is ignored</h2>
    <div class="quote">${highlight(r.commentary_post, "post")}</div></div>

  <p class="muted">${esc(r.provider ?? "")}/${esc(r.model ?? "")} · coefficients ${esc(r.coeff_version ?? "")}</p>`;
  return c.html(page(`${r.shooter ?? "Shot"} – commentary xG`, "/", body));
});

/**
 * Codes that can actually move a number. Needed because calibration applies a global intercept
 * to every row, so "delta is non-zero" would count all of them -- what we want to report is
 * "the commentary moved this shot".
 */
const WEIGHTED_CODES = Object.entries(WEIGHTS).filter(([, w]) => w !== 0).map(([c]) => c);
const MOVED = `EXISTS (SELECT 1 FROM jsonb_array_elements(c.features) f
                        WHERE f->>'code' = ANY($1))`;

/**
 * Pre-shot only: the shots the commentary moved using nothing but what was said before the puck
 * left the stick. This is the subset that can honestly be scored against real goals, so it is
 * worth being able to look at on its own rather than mixed in with the replay-driven rows.
 */
app.get("/pre", async (c) => {
  const sort = c.req.query("sort") ?? "delta";
  const order = { delta: "abs(c.delta_pre) DESC", up: "c.delta_pre DESC",
                  down: "c.delta_pre ASC", xg: "c.x_goal DESC",
                  time: "c.game_id, s.video_sec" }[sort] ?? "abs(c.delta_pre) DESC";

  const rows = await query<ListRow>(
    `${SEQ_CTE}
     SELECT c.game_id, c.event_id, s.shooter, s.result, s.period, s.clock, s.team,
            c.x_goal, c.xg_adjusted_pre, c.xg_adjusted_full, c.delta_pre, c.delta_full,
            c.relevance, c.summary, c.features, c.youtube_url, c.outcome_leaked,
            c.video_id, s.video_time,
            q.seq_pos, q.seq_len, q.seq_span
       FROM commentary_xg c
       JOIN shot_commentary_split s USING (game_id, event_id)
       JOIN seq q USING (game_id, event_id)
      WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(c.features) f
                     WHERE f->>'source' = 'pre' AND f->>'code' = ANY($1))
      ORDER BY ${order} LIMIT 400`,
    [WEIGHTED_CODES],
  );
  const [tot] = await query<{ n: number; feats: number }>(
    `SELECT COUNT(*)::int AS n,
            (SELECT COUNT(*)::int FROM commentary_xg c2, jsonb_array_elements(c2.features) f
              WHERE f->>'source' = 'pre') AS feats
       FROM commentary_xg`,
  );
  const maxDelta = Math.max(...rows.map((r) => Math.abs(r.delta_pre)), 0.01);
  const link = (v: string, label: string) => `<a href="/pre?sort=${v}">${label}</a>`;

  const body = `
  <div class="card">
    <h2>Shots the pre-shot commentary moved</h2>
    <p>Built from what was said <strong>before the puck left the stick</strong>, so it has not seen
       the result. This is the only number scored against real goals.</p>
    <p class="muted">${rows.length} of ${tot?.n ?? 0} annotated shots, from ${tot?.feats ?? 0} pre-shot
       features. Most commentary detail arrives during the replay instead — those shots are on the
       <a href="/">Shots</a> page, and they are richer but cannot be validated.</p>
  </div>
  <form class="filters" method="get">
    <select name="sort">
      ${[["delta", "Biggest change"], ["up", "Most raised"], ["down", "Most lowered"],
         ["xg", "Highest xG"], ["time", "Chronological"]]
        .map(([v, lb]) => `<option value="${v}"${v === sort ? " selected" : ""}>${lb}</option>`).join("")}
    </select>
    <button type="submit">Apply</button>
  </form>
  <div class="card">
  ${rows.length === 0 ? `<p class="empty">No shot has a pre-shot feature yet.</p>` : `
  <table>
    <thead><tr><th>Shot</th><th class="num">${link("xg", "MoneyPuck xG")}</th>
      <th class="num">${link("delta", "Pre-shot xG ▾")}</th><th>Δ</th>
      <th class="hide-sm">Evidence from before the shot</th><th></th></tr></thead>
    <tbody>
    ${rows.map((r) => `<tr>
      <td><a href="/shot/${r.game_id}/${r.event_id}"><strong>${esc(r.shooter ?? "unknown")}</strong></a>
          <div class="muted">${esc(r.team ?? "")} · P${r.period} ${esc(r.clock)} · ${esc(r.result)}
            ${seqBadge(r.seq_pos ?? 0, r.seq_len ?? 0, r.seq_span ?? 0)}</div></td>
      <td class="num">${pct(r.x_goal)}</td>
      <td class="num">${pct(r.xg_adjusted_pre)}<div class="muted">${signedPts(r.delta_pre)}</div></td>
      <td>${deltaBar(r.delta_pre, maxDelta)}</td>
      <td class="hide-sm">${esc(r.summary ?? "")}<div>${chips(r.features, "pre")}</div></td>
      <td><a href="/shot/${r.game_id}/${r.event_id}" title="play this moment in the dashboard">▶</a>
          <div class="muted at">${esc(r.video_time ?? "")}</div></td>
    </tr>`).join("")}
    </tbody></table>`}
  </div>`;
  return c.html(page("Pre-shot – commentary xG", "/pre", body));
});

/**
 * Goals only: every goal re-ranked by what the commentary says about the chance, with the total
 * held at MoneyPuck's. The arithmetic lives in goals.ts and is the same code `make goals` runs;
 * this page only reads options from the URL, so a scenario is a shareable link and the page
 * works without JavaScript (the one inline handler just saves clicking Apply).
 */
const SCALES = [
  { v: 0, label: "Off" }, { v: 0.5, label: "Half" }, { v: 1, label: "Normal" }, { v: 2, label: "Double" },
] as const;

/** Examples shown under each group so "Deflections and blocks" means something to an outsider. */
const GROUP_EXAMPLES: Record<string, string> = {
  A: "screened goalie, traffic in front",
  B: "tipped or deflected shots, partly blocked shots",
  C: "goalie out of position, goalie set and square",
  D: "cross-ice pass, back-door play, one-timer",
  E: "wide open in the slot, quick release, shooter under pressure",
  F: "breakaway, rebound scramble",
};

interface Scenario { name: string; blurb: string; params: Record<string, string> }
const SCENARIOS: Scenario[] = [
  { name: "Our model", blurb: "Pre-shot and replay commentary; goalie state on the replay ignored.", params: {} },
  { name: "Before the shot only", blurb: "Ignore the replay, as in our earlier approach.", params: { replay: "off" } },
  { name: "Count goalie on replay", blurb: "Also count “goalie out of position” said on the replay.", params: { finish: "keep" } },
  { name: "Without passing", blurb: "Passes before the shot count for nothing.", params: { gD: "0" } },
  { name: "Without screens", blurb: "Screens and traffic count for nothing.", params: { gA: "0" } },
];

const SORTS = [
  { v: "rise", label: "Biggest rise" },
  { v: "fall", label: "Biggest fall" },
  { v: "long", label: "Long-shot goals" },
  { v: "best", label: "Best chances" },
] as const;

/** MoneyPuck's number and ours on one shared 0-to-max scale, so the move is the picture. */
function dumbbell(base: number, refined: number, max: number, width = 180, height = 22): string {
  const x = (p: number) => 6 + (Math.min(p, max) / max) * (width - 12);
  const up = refined >= base;
  const colour = up ? "var(--up)" : "var(--down)";
  return `<svg class="bar" width="${width}" height="${height}" role="img"
      aria-label="MoneyPuck ${(base * 100).toFixed(1)}%, refined ${(refined * 100).toFixed(1)}%">
    <line x1="6" y1="${height / 2}" x2="${width - 6}" y2="${height / 2}" stroke="var(--grid)" stroke-width="2"/>
    <line x1="${x(base).toFixed(1)}" y1="${height / 2}" x2="${x(refined).toFixed(1)}" y2="${height / 2}"
          stroke="${colour}" stroke-width="3" stroke-linecap="round"/>
    <circle cx="${x(base).toFixed(1)}" cy="${height / 2}" r="5" fill="var(--surface-1)" stroke="var(--muted)" stroke-width="2"/>
    <circle cx="${x(refined).toFixed(1)}" cy="${height / 2}" r="6" fill="${colour}"/></svg>`;
}

const ordinal = (n: number) => {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return `${n}${s[(v - 20) % 10] ?? s[v] ?? s[0]}`;
};
const periodName = (p: number | null) =>
  p === null ? "" : p <= 3 ? `${ordinal(p)} period` : p === 4 ? "OT" : `${p - 3}OT`;

app.get("/goals", async (c) => {
  const qp = (k: string) => c.req.query(k) ?? "";
  const replay = qp("replay") !== "off";
  const dropFinish = qp("finish") !== "keep";
  const groupScale: Record<string, number> = {};
  for (const g of GROUPS) {
    const raw = qp(`g${g.key}`);
    const v = Number(raw);
    if (raw !== "" && SCALES.some((s) => s.v === v) && v !== 1) groupScale[g.key] = v;
  }
  const sort = SORTS.some((s) => s.v === qp("sort")) ? qp("sort") : "rise";
  const search = qp("q").trim();

  const all = await fetchGoals();
  const res = refineGoals(all, {
    useReplay: replay, dropReplayFinish: dropFinish,
    ...(Object.keys(groupScale).length ? { groupScale } : {}),
  });
  const ours = refineGoals(all); // "Our model", for comparison when a scenario is active

  // Ranks are over all goals, so the search hides rows but never changes the numbers.
  const rankOf = (key: (r: RefinedGoal) => number) => {
    const m = new Map<RefinedGoal, number>();
    [...res.goals].sort((a, b) => key(b) - key(a)).forEach((r, i) => m.set(r, i + 1));
    return m;
  };
  const rankBase = rankOf((r) => r.xgBase);
  const rankRef = rankOf((r) => r.xgRefined);

  const LONG_SHOT = 0.05;
  const change = (r: RefinedGoal) => r.xgRefined - r.xgBase;
  const fromFeatures = (r: RefinedGoal) => sigmoid(logit(r.xgBase) + r.featureShift) - r.xgBase;
  const orders: Record<string, (a: RefinedGoal, b: RefinedGoal) => number> = {
    rise: (a, b) => change(b) - change(a),
    fall: (a, b) => change(a) - change(b),
    long: (a, b) => change(b) - change(a),
    best: (a, b) => b.xgRefined - a.xgRefined,
  };
  const needle = search.toLowerCase();
  const rows = res.goals
    .filter((r) => sort !== "long" || r.xgBase < LONG_SHOT)
    .filter((r) => !needle ||
      `${r.goal.shooter ?? ""} ${r.goal.team ?? ""} ${r.goal.game_id}`.toLowerCase().includes(needle))
    .sort(orders[sort]!);

  const movedBy = (x: typeof res) => x.goals.filter((r) => r.featureShift !== 0).length;
  const longShots = res.goals.filter((r) => r.xgBase < LONG_SHOT);
  const longUp = longShots.filter((r) => r.xgRefined > r.xgBase).length;
  const nGames = new Set(all.map((g) => g.game_id)).size;
  const scaleMax = Math.min(1, Math.ceil(Math.max(...res.goals.map((r) => Math.max(r.xgBase, r.xgRefined)), 0.1) * 10) / 10);

  // Which scenario is on: a preset if the settings match one exactly, otherwise "Custom".
  const current: Record<string, string> = {};
  if (!replay) current.replay = "off";
  if (!dropFinish) current.finish = "keep";
  for (const [k, v] of Object.entries(groupScale)) current[`g${k}`] = String(v);
  const same = (a: Record<string, string>, b: Record<string, string>) =>
    Object.keys(a).length === Object.keys(b).length && Object.entries(a).every(([k, v]) => b[k] === v);
  const active = SCENARIOS.find((s) => same(s.params, current));

  const href = (params: Record<string, string>) => {
    const u = new URLSearchParams(params);
    if (sort !== "rise") u.set("sort", sort);
    if (search) u.set("q", search);
    const s = u.toString();
    return `/goals${s ? `?${s}` : ""}`;
  };
  const sortHref = (v: string) => {
    const u = new URLSearchParams(current);
    if (v !== "rise") u.set("sort", v);
    if (search) u.set("q", search);
    const s = u.toString();
    return `/goals${s ? `?${s}` : ""}`;
  };
  const hidden = (name: string, value: string) => `<input type="hidden" name="${name}" value="${esc(value)}">`;
  const tile = (k: string, v: string, sub: string) =>
    `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="sub">${sub}</div></div>`;

  const why = (r: RefinedGoal) => {
    const used = usableFeatures(r.goal.features, { useReplay: replay, dropReplayFinish: dropFinish });
    const live = liveCodes(r);
    const shown = used.filter((f) => live.some((l) => l.code === f.code && l.source === f.source));
    if (!shown.length) return `<span class="muted">Nothing that changes the chance</span>`;
    return shown.map((f) => {
      const c = live.find((l) => l.code === f.code && l.source === f.source)!;
      const up = c.applied > 0;
      return `<span class="tag ${up ? "up" : "down"}" title="“${esc(f.quote)}” — said ${f.source === "pre" ? "before the shot" : "on the replay"}">${up ? "↑" : "↓"} ${esc(featureLabel(f.code))}${f.source === "pre" ? ` <i>before shot</i>` : ""}</span>`;
    }).join("");
  };

  const body = `
  <section class="g-hero">
    <h2>Goals, re-ranked by what the commentators said</h2>
    <p>For each of our ${res.goals.length} goals, we start from MoneyPuck’s chance of scoring and adjust it
       using the broadcast commentary: a pass, a screen or a breakaway raises it. The
       <strong>total stays fixed at ${res.totalBase.toFixed(1)} expected goals</strong>, so when one goal
       goes up, the others give up a little. The commentary decides which goals were the better chances.</p>
  </section>

  <div class="tiles" style="margin-bottom:16px">
    ${tile("Goals", String(res.goals.length), `from ${nGames} games`)}
    ${tile("Expected goals", res.totalRefined.toFixed(1), "same total before and after")}
    ${tile("Moved by the commentary", String(movedBy(res)), `of ${res.goals.length} goals`)}
    ${tile("Long-shot goals raised", `${longUp} of ${longShots.length}`,
           "goals MoneyPuck gave under a 5% chance")}
  </div>

  <form method="get" class="card g-panel">
    <div class="g-panel-head">
      <h2>Try a scenario</h2>
      <span class="muted">${active ? esc(active.blurb) : "Custom settings."}</span>
    </div>
    <div class="g-scenarios">
      ${SCENARIOS.map((s) => `<a class="g-btn${s === active ? " on" : ""}" href="${href(s.params)}">${esc(s.name)}</a>`).join("")}
    </div>
    ${active === SCENARIOS[0] ? "" : `<p class="g-banner">In this scenario the commentary moves
      <strong>${movedBy(res)}</strong> goals (our model: ${movedBy(ours)}).
      <a href="${href({})}">Back to our model</a></p>`}
    <details${Object.keys(groupScale).length ? " open" : ""}>
      <summary>Fine-tune: how much each kind of detail counts</summary>
      <div class="g-groups">
        ${GROUPS.map((g) => `<div class="g-group">
          <div><strong>${esc(g.label)}</strong><div class="muted">e.g. ${esc(GROUP_EXAMPLES[g.key] ?? "")}</div></div>
          <div class="g-seg">${SCALES.map((s) => `<label><input type="radio" name="g${g.key}" value="${s.v}"
              ${(groupScale[g.key] ?? 1) === s.v ? "checked" : ""} onchange="this.form.submit()"><span>${s.label}</span></label>`).join("")}</div>
        </div>`).join("")}
      </div>
      <p class="muted">Replay commentary:
        <label><input type="radio" name="replay" value="on" ${replay ? "checked" : ""} onchange="this.form.submit()"> use</label>
        <label><input type="radio" name="replay" value="off" ${replay ? "" : "checked"} onchange="this.form.submit()"> ignore</label>
        &nbsp;·&nbsp; Goalie state said on the replay:
        <label><input type="radio" name="finish" value="drop" ${dropFinish ? "checked" : ""} onchange="this.form.submit()"> ignore</label>
        <label><input type="radio" name="finish" value="keep" ${dropFinish ? "" : "checked"} onchange="this.form.submit()"> count</label>
        <noscript><button type="submit">Apply</button></noscript></p>
    </details>
    ${sort !== "rise" ? hidden("sort", sort) : ""}${search ? hidden("q", search) : ""}
  </form>

  <form method="get" class="g-search">
    ${Object.entries(current).map(([k, v]) => hidden(k, v)).join("")}
    ${sort !== "rise" ? hidden("sort", sort) : ""}
    <input type="search" name="q" value="${esc(search)}" placeholder="Search a player, team or game ID" aria-label="Search goals">
    <button type="submit">Search</button>
    ${search ? `<a href="${sortHref(sort).replace(/([?&])q=[^&]*&?/, "$1").replace(/[?&]$/, "")}">Clear</a>` : ""}
  </form>

  <nav class="g-tabs">
    ${SORTS.map((s) => `<a href="${sortHref(s.v)}" class="${s.v === sort ? "on" : ""}">${s.label}</a>`).join("")}
    <span class="muted">${rows.length} goal${rows.length === 1 ? "" : "s"}${sort === "long" ? " MoneyPuck gave under a 5% chance" : ""}</span>
  </nav>

  <div class="card">
  ${rows.length === 0 ? `<p class="empty">No goals match${search ? ` “${esc(search)}”` : ""}.</p>` : `
  <table class="g-table">
    <thead><tr><th>Goal</th>
      <th title="Grey dot: MoneyPuck. Coloured dot: after the commentary.">Chance of scoring</th>
      <th class="num">Change</th>
      <th class="num" title="Rank among all ${res.goals.length} goals by chance of scoring, 1st = best chance">Rank</th>
      <th class="hide-sm">Why it moved</th><th></th></tr></thead>
    <tbody>
    ${rows.map((r) => {
      const g = r.goal;
      const d = change(r);
      return `<tr>
      <td><a href="/shot/${g.game_id}/${g.event_id}" class="g-name">${esc(g.shooter ?? "unknown")}</a>
          <div class="muted g-when" title="Game ${esc(g.game_id)}">${esc(g.team ?? "")} · ${periodName(g.period)}, ${esc(g.clock ?? "")}</div></td>
      <td><div class="g-xg">${dumbbell(r.xgBase, r.xgRefined, scaleMax)}
          <span>${pct(r.xgBase)} <span class="muted">→</span> <strong>${pct(r.xgRefined)}</strong></span></div></td>
      <td class="num"><span class="g-delta ${Math.abs(d) < 0.0005 ? "flat" : d > 0 ? "up" : "down"}">${Math.abs(d) < 0.0005 ? "0.0" : signedPts(d)}</span>
          <div class="muted g-split" title="From this goal's own commentary, and from the small amount every goal gives up to keep the total fixed">
            ${r.featureShift !== 0 ? `commentary ${signedPts(fromFeatures(r))} · shared ${signedPts(d - fromFeatures(r))}` : "shared only"}</div></td>
      <td class="num g-rank">${ordinal(rankBase.get(r)!)} → <strong>${ordinal(rankRef.get(r)!)}</strong></td>
      <td class="hide-sm">${why(r)}</td>
      <td><a href="/shot/${g.game_id}/${g.event_id}" class="g-watch" title="watch this goal">▶ Watch</a></td>
    </tr>`;
    }).join("")}
    </tbody></table>`}
  </div>
  <p class="muted">Changes are in percentage points. Weights are our hand-set starting values
     (coefficients ${COEFF_VERSION}); nothing here is fitted yet. This page shows how the commentary
     re-orders goals; whether the new order is <em>better</em> needs an outside reference, such as
     hand ratings of the clips. Reproduce the numbers with <code>make goals</code>.</p>`;
  return c.html(page("Goals – commentary xG", "/goals", body));
});

app.get("/summary", async (c) => {
  const [agg] = await query<{
    n: number; adjusted: number; adjusted_pre: number; leaked: number; mean_abs: number; raised: number; lowered: number;
    sum_base: number; sum_adj: number; goals: number;
  }>(
    `SELECT COUNT(*)::int AS n,
            COUNT(*) FILTER (WHERE ${MOVED})::int AS adjusted,
            COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(c.features) f
                   WHERE f->>'source' = 'pre' AND f->>'code' = ANY($1)))::int AS adjusted_pre,
            COUNT(*) FILTER (WHERE c.outcome_leaked)::int AS leaked,
            COALESCE(AVG(abs(c.delta_full)) FILTER (WHERE ${MOVED}), 0) AS mean_abs,
            COUNT(*) FILTER (WHERE ${MOVED} AND c.delta_full > 0)::int AS raised,
            COUNT(*) FILTER (WHERE ${MOVED} AND c.delta_full < 0)::int AS lowered,
            COALESCE(SUM(c.x_goal), 0) AS sum_base,
            COALESCE(SUM(c.xg_adjusted_pre), 0) AS sum_adj,
            COUNT(*) FILTER (WHERE s.goal)::int AS goals
       FROM commentary_xg c JOIN shot_commentary_split s USING (game_id, event_id)`,
    [WEIGHTED_CODES],
  );
  const freq = await query<{ code: string; pre: number; post: number; n: number }>(
    `SELECT f->>'code' AS code,
            COUNT(*) FILTER (WHERE f->>'source' = 'pre')::int  AS pre,
            COUNT(*) FILTER (WHERE f->>'source' = 'post')::int AS post,
            COUNT(*)::int AS n
       FROM commentary_xg c, jsonb_array_elements(c.features) f
      GROUP BY 1 ORDER BY n DESC, code`,
  );
  const featTotal = freq.reduce((a, f) => a + f.n, 0);
  const preTotal = freq.reduce((a, f) => a + f.pre, 0);
  const [cov] = await query<{ none: number; some: number }>(
    `SELECT COUNT(*) FILTER (WHERE jsonb_array_length(features) = 0)::int AS none,
            COUNT(*) FILTER (WHERE jsonb_array_length(features) > 0)::int AS some
       FROM commentary_xg`,
  );
  const [cal] = await query<{
    calib_version: string; alpha: number; n_shots: number; brier_base: number; brier_adj: number;
    logloss_base: number; logloss_adj: number; reliability: RelBin[] | null;
  }>(`SELECT * FROM calibrations ORDER BY created_at DESC LIMIT 1`);

  const tile = (k: string, v: string, sub = "") =>
    `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;

  const n = agg?.n ?? 0;
  const metricRow = (label: string, base: number, adj: number) => {
    const better = adj < base;
    return `<tr><td>${label}</td><td class="num">${base.toFixed(4)}</td>
      <td class="num">${adj.toFixed(4)}</td>
      <td style="color:${better ? "var(--good)" : "var(--crit)"}">${better ? "better" : "worse"}
        by ${Math.abs(adj - base).toFixed(5)}</td></tr>`;
  };

  const body = `
  <div class="tiles" style="margin-bottom:16px">
    ${tile("Shots annotated", String(n))}
    ${tile("Moved by replay talk", String(agg?.adjusted ?? 0),
           n ? `${(((agg?.adjusted ?? 0) / n) * 100).toFixed(0)}% of shots` : "")}
    ${tile("Moved pre-shot only", String(agg?.adjusted_pre ?? 0),
           n ? `${(((agg?.adjusted_pre ?? 0) / n) * 100).toFixed(0)}% — the scoreable number` : "")}
    ${tile("Raised / lowered", `${agg?.raised ?? 0} / ${agg?.lowered ?? 0}`)}
    ${tile("Mean |Δ| when moved", `${((agg?.mean_abs ?? 0) * 100).toFixed(2)} pts`)}
    ${tile("Pre-window leaks", String(agg?.leaked ?? 0),
           n ? `${(((agg?.leaked ?? 0) / n) * 100).toFixed(1)}% of windows` : "")}
  </div>

  <div class="card"><h2>Total expected goals</h2>
    <p>MoneyPuck <strong>${(agg?.sum_base ?? 0).toFixed(2)}</strong> ·
       adjusted <strong>${(agg?.sum_adj ?? 0).toFixed(2)}</strong> ·
       actually scored <strong>${agg?.goals ?? 0}</strong></p>
    <p class="muted">Calibration holds the adjusted total to the MoneyPuck total, so commentary
       moves expected goals between shots rather than inventing them.</p></div>

  <div class="card"><h2>Every feature found, what it means, and where the evidence came from</h2>
    <p class="muted">${featTotal} features across ${freq.length} of the ${FEATURES.length} codes in
       the vocabulary; the other ${FEATURES.length - freq.length} are rarer things that have not
       happened yet. ${preTotal} came from before the shot and feed the scoreable number, ${featTotal - preTotal}
       from the replay. Every shot has commentary in both windows — ${cov?.none ?? 0} of the
       ${n} simply say nothing an xG model is missing, which is the expected answer.</p>
    ${featureTable(
      freq.map((f) => ({
        code: f.code,
        hint: featureDef(f.code)?.hint ?? "",
        weight: WEIGHTS[f.code] ?? 0,
        pre: f.pre,
        post: f.post,
      })),
      Math.max(...freq.map((f) => f.n), 1),
    )}
    <p class="muted">The badge is the feature's weight in log-odds, added to
       <code>logit(xG)</code> and scaled by the model's confidence and by how relevant the
       commentary is. A weight of 0 means the code is recorded but never moves the number.</p></div>

  <div class="card"><h2>Does it beat MoneyPuck?</h2>
    ${cal ? `
      <table><thead><tr><th>Metric</th><th class="num">MoneyPuck</th>
        <th class="num">Adjusted (pre-shot)</th><th>Verdict</th></tr></thead>
        <tbody>${metricRow("Brier score", cal.brier_base, cal.brier_adj)}
               ${metricRow("Log loss", cal.logloss_base, cal.logloss_adj)}</tbody></table>
      <p class="muted">Scored on ${cal.n_shots} shots with a known outcome, using the pre-shot
         number only — the full number has seen the replay talk, so scoring it would flatter it.
         Lower is better.</p>
      ${cal.reliability ? `<div style="max-width:440px;margin-top:12px">
        ${reliabilityChart([], cal.reliability, 0.3)}</div>` : ""}
      ${cal.brier_adj >= cal.brier_base
        ? `<p style="color:var(--crit)">On this sample the adjustment does not beat MoneyPuck.
            With few adjusted shots that is the honest reading, not a bug.</p>` : ""}
    ` : `<p class="empty">No calibration yet. Run <code>make calibrate</code>.</p>`}
  </div>`;
  return c.html(page("Summary – commentary xG", "/summary", body));
});

// Only bind a port when this file is what node was asked to run, so importing the app (a test,
// another entry point) does not start a server as a side effect.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  loadEnv();
  await assertSchema();
  const port = Number(process.env.XG_DASH_PORT ?? 5058);
  serve({ fetch: app.fetch, port, hostname: "127.0.0.1" });
  console.log(`Commentary-xG dashboard on http://127.0.0.1:${port}`);
}

export default app;
