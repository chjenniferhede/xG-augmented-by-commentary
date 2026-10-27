/**
 * Inline SVG charts. Server-rendered so the page works without JavaScript; hover titles give
 * the per-mark detail the data-viz guidance asks for without shipping a chart library.
 *
 * Mark specs followed: 2px lines, >=8px markers, 4px rounded data-ends on bars anchored to the
 * baseline, recessive grid and axis, text in ink tokens rather than series colour.
 */

import { esc } from "./layout.ts";

/** A signed bar for one row's delta, drawn around a shared zero line. */
export function deltaBar(delta: number, max: number, width = 120, height = 16): string {
  const half = width / 2;
  const span = max > 0 ? Math.min(Math.abs(delta) / max, 1) * half : 0;
  const up = delta >= 0;
  const x = up ? half : half - span;
  const colour = up ? "var(--up)" : "var(--down)";
  if (Math.abs(delta) < 1e-9) {
    return `<svg class="bar" width="${width}" height="${height}" role="img" aria-label="no change">
      <line x1="${half}" y1="2" x2="${half}" y2="${height - 2}" stroke="var(--axis)" stroke-width="1"/></svg>`;
  }
  return `<svg class="bar" width="${width}" height="${height}" role="img"
      aria-label="${up ? "raised" : "lowered"} by ${(Math.abs(delta) * 100).toFixed(1)} points">
    <line x1="${half}" y1="1" x2="${half}" y2="${height - 1}" stroke="var(--axis)" stroke-width="1"/>
    <rect x="${x.toFixed(1)}" y="3" width="${Math.max(span, 1.5).toFixed(1)}" height="${height - 6}"
          rx="3" fill="${colour}"/></svg>`;
}

interface BarDatum { label: string; value: number; negative?: boolean }

/** Horizontal bars for counts. One measure, so one hue unless a datum is explicitly negative. */
export function barChart(data: BarDatum[], opts: { width?: number; barH?: number } = {}): string {
  if (!data.length) return `<p class="empty">Nothing to show yet.</p>`;
  const width = opts.width ?? 560;
  const barH = opts.barH ?? 20;
  const gap = 6; // >=2px surface gap between adjacent fills
  const labelW = 190;
  const valueW = 46;
  const plotW = width - labelW - valueW;
  const max = Math.max(...data.map((d) => d.value), 1);
  const height = data.length * (barH + gap);
  const rows = data
    .map((d, i) => {
      const y = i * (barH + gap);
      const w = Math.max((d.value / max) * plotW, 2);
      const colour = d.negative ? "var(--down)" : "var(--up)";
      return `<g><title>${esc(d.label)}: ${d.value}</title>
        <text x="${labelW - 8}" y="${y + barH / 2}" text-anchor="end" dominant-baseline="central"
              font-size="12" fill="var(--ink-2)">${esc(d.label)}</text>
        <rect x="${labelW}" y="${y}" width="${w.toFixed(1)}" height="${barH}" rx="4" fill="${colour}"/>
        <text x="${labelW + w + 7}" y="${y + barH / 2}" dominant-baseline="central"
              font-size="12" fill="var(--ink-2)" font-variant-numeric="tabular-nums">${d.value}</text></g>`;
    })
    .join("");
  return `<svg width="100%" viewBox="0 0 ${width} ${height}" role="img"
      aria-label="bar chart of ${data.length} categories">${rows}</svg>`;
}

export interface RelBin { lo: number; hi: number; n: number; meanPred: number; observed: number }

/**
 * Reliability: predicted probability against observed goal rate, for the baseline and the
 * adjusted model. The diagonal is perfect calibration. Two series, so both are legended and
 * direct-labelled -- identity never rests on colour alone.
 */
export function reliabilityChart(base: RelBin[], adj: RelBin[], maxP = 0.5): string {
  const W = 420, H = 300, P = { t: 12, r: 14, b: 34, l: 42 };
  const pw = W - P.l - P.r, ph = H - P.t - P.b;
  const sx = (v: number) => P.l + (Math.min(v, maxP) / maxP) * pw;
  const sy = (v: number) => P.t + ph - (Math.min(v, maxP) / maxP) * ph;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * maxP);

  const grid = ticks
    .map(
      (t) =>
        `<line x1="${P.l}" y1="${sy(t)}" x2="${W - P.r}" y2="${sy(t)}" stroke="var(--grid)" stroke-width="1"/>
         <text x="${P.l - 7}" y="${sy(t)}" text-anchor="end" dominant-baseline="central"
               font-size="11" fill="var(--muted)">${(t * 100).toFixed(0)}%</text>
         <text x="${sx(t)}" y="${H - P.b + 16}" text-anchor="middle"
               font-size="11" fill="var(--muted)">${(t * 100).toFixed(0)}%</text>`,
    )
    .join("");

  const series = (bins: RelBin[], colour: string, label: string) => {
    const pts = bins.filter((b) => b.n > 0 && Number.isFinite(b.meanPred) && Number.isFinite(b.observed));
    if (!pts.length) return "";
    const d = pts.map((b, i) => `${i ? "L" : "M"}${sx(b.meanPred).toFixed(1)},${sy(b.observed).toFixed(1)}`).join(" ");
    const dots = pts
      .map(
        (b) =>
          `<g><title>${label}: predicted ${(b.meanPred * 100).toFixed(1)}%, observed ${(b.observed * 100).toFixed(1)}% (n=${b.n})</title>
           <circle cx="${sx(b.meanPred).toFixed(1)}" cy="${sy(b.observed).toFixed(1)}" r="4.5"
                   fill="${colour}" stroke="var(--surface-1)" stroke-width="2"/></g>`,
      )
      .join("");
    const last = pts[pts.length - 1]!;
    return `<path d="${d}" fill="none" stroke="${colour}" stroke-width="2"
              stroke-linejoin="round" stroke-linecap="round"/>${dots}
      <text x="${Math.min(sx(last.meanPred) + 8, W - P.r - 4)}" y="${sy(last.observed) - 9}"
            font-size="11" fill="var(--ink-2)">${label}</text>`;
  };

  return `<svg width="100%" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="reliability curve: predicted versus observed goal rate">
    ${grid}
    <line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(maxP)}" y2="${sy(maxP)}"
          stroke="var(--axis)" stroke-width="1" stroke-dasharray="4 4"/>
    <text x="${sx(maxP) - 4}" y="${sy(maxP) + 14}" text-anchor="end"
          font-size="10" fill="var(--muted)">perfect calibration</text>
    <line x1="${P.l}" y1="${P.t}" x2="${P.l}" y2="${H - P.b}" stroke="var(--axis)" stroke-width="1"/>
    <line x1="${P.l}" y1="${H - P.b}" x2="${W - P.r}" y2="${H - P.b}" stroke="var(--axis)" stroke-width="1"/>
    ${series(base, "var(--up)", "MoneyPuck")}
    ${series(adj, "var(--down)", "adjusted")}
    <text x="${P.l + pw / 2}" y="${H - 2}" text-anchor="middle" font-size="11" fill="var(--muted)">predicted</text>
  </svg>`;
}

export interface StackDatum { label: string; a: number; b: number; note?: string }

/**
 * Stacked horizontal bars for one count split two ways (here: which window the evidence came
 * from). Two series, so both are legended; a 2px surface gap separates the segments so the
 * boundary is readable without relying on the colour difference alone.
 */
export function stackedBarChart(
  data: StackDatum[],
  legend: { a: string; b: string },
  opts: { width?: number; barH?: number } = {},
): string {
  if (!data.length) return `<p class="empty">Nothing to show yet.</p>`;
  const width = opts.width ?? 620;
  const barH = opts.barH ?? 18;
  const gap = 6;
  const labelW = 200;
  const valueW = 54;
  const plotW = width - labelW - valueW;
  const max = Math.max(...data.map((d) => d.a + d.b), 1);
  const height = data.length * (barH + gap);

  const rows = data
    .map((d, i) => {
      const y = i * (barH + gap);
      const wa = (d.a / max) * plotW;
      const wb = (d.b / max) * plotW;
      const segA = d.a
        ? `<rect x="${labelW}" y="${y}" width="${Math.max(wa, 2).toFixed(1)}" height="${barH}"
                 rx="4" fill="var(--pre)"><title>${esc(d.label)} — ${legend.a}: ${d.a}</title></rect>`
        : "";
      // +2 on x: a surface-coloured gap between the two fills.
      const segB = d.b
        ? `<rect x="${(labelW + wa + (d.a ? 2 : 0)).toFixed(1)}" y="${y}"
                 width="${Math.max(wb - (d.a ? 2 : 0), 2).toFixed(1)}" height="${barH}"
                 rx="4" fill="var(--post)"><title>${esc(d.label)} — ${legend.b}: ${d.b}</title></rect>`
        : "";
      return `<g>
        <text x="${labelW - 8}" y="${y + barH / 2}" text-anchor="end" dominant-baseline="central"
              font-size="11.5" fill="var(--ink-2)">${esc(d.label)}</text>
        ${segA}${segB}
        <text x="${labelW + wa + wb + 8}" y="${y + barH / 2}" dominant-baseline="central"
              font-size="11.5" fill="var(--ink-2)" font-variant-numeric="tabular-nums">${d.a + d.b}${
                d.note ? ` <tspan fill="var(--muted)">${esc(d.note)}</tspan>` : ""
              }</text></g>`;
    })
    .join("");

  return `<div>
    <div style="display:flex;gap:16px;margin-bottom:8px;font-size:12px;color:var(--ink-2)">
      <span><svg width="10" height="10" style="vertical-align:-1px"><rect width="10" height="10" rx="2" fill="var(--pre)"/></svg>
        ${esc(legend.a)}</span>
      <span><svg width="10" height="10" style="vertical-align:-1px"><rect width="10" height="10" rx="2" fill="var(--post)"/></svg>
        ${esc(legend.b)}</span>
    </div>
    <svg width="100%" viewBox="0 0 ${width} ${height}" role="img"
         aria-label="stacked bar chart of ${data.length} feature codes by evidence source">${rows}</svg>
  </div>`;
}

export interface FeatureRow {
  code: string;
  hint: string;
  weight: number;
  pre: number;
  post: number;
}

/**
 * The feature catalogue as rows rather than a bare bar chart: each code needs its meaning and
 * its weight next to the count, and a text-heavy row is easier to build and to read in HTML
 * than in SVG. The bar is still a bar -- one stacked track per row, 2px gap between the two
 * fills -- but the identity of each segment is carried by a label as well as by colour.
 */
export function featureTable(rows: FeatureRow[], max: number): string {
  if (!rows.length) return `<p class="empty">No features extracted yet.</p>`;
  const seg = (n: number, v: string, label: string) =>
    n ? `<span class="seg" style="flex:${n};background:${v}" title="${n} from ${label}"></span>` : "";

  return `<div class="flist">
    <div class="flegend">
      <span><i style="background:var(--pre)"></i>before the shot — feeds the scoreable number</span>
      <span><i style="background:var(--post)"></i>from the replay — richer, but it has seen the outcome</span>
    </div>
    ${rows
      .map((r) => {
        const total = r.pre + r.post;
        const w = r.weight;
        const dir = w > 0 ? "raises" : w < 0 ? "lowers" : "no effect";
        const cls = w > 0 ? "pos" : w < 0 ? "neg" : "zero";
        const only = r.pre === 0 ? "replay only" : r.post === 0 ? "pre-shot only" : `${r.pre} pre · ${r.post} replay`;
        return `<div class="frow">
          <div class="fhead">
            <code class="fcode ${cls}">${esc(r.code)}</code>
            <span class="fw ${cls}" title="log-odds added at full confidence">${
              w === 0 ? "0" : (w > 0 ? "+" : "") + w.toFixed(2)
            }</span>
            <span class="fdir">${dir} xG</span>
          </div>
          <div class="fhint">${esc(r.hint)}</div>
          <div class="fbarline">
            <span class="ftrack" style="width:${Math.max((total / max) * 100, 3).toFixed(1)}%">
              ${seg(r.pre, "var(--pre)", "before the shot")}${seg(r.post, "var(--post)", "the replay")}
            </span>
            <span class="fcount">${total}</span>
            <span class="fsplit">${only}</span>
          </div>
        </div>`;
      })
      .join("")}
  </div>`;
}
