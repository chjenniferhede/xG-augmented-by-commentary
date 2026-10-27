/**
 * Page chrome. Colours are the validated data-viz palette, declared once as CSS custom
 * properties under both the OS media query and an explicit data-theme, so the toggle wins
 * either way.
 */

export const esc = (s: unknown): string =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

const CSS = `
:root {
  color-scheme: light;
  --surface-1: #fcfcfb; --plane: #f9f9f7;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --up: #2a78d6;   /* diverging: xG raised */
  --down: #e34948; /* diverging: xG lowered */
  --neutral: #f0efec;
  --good: #0ca30c; --warn: #fab219; --crit: #d03b3b;
  --pre: #2a78d6; --post: #eb6834;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface-1: #1a1a19; --plane: #0d0d0d;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --up: #3987e5; --down: #e66767; --neutral: #383835;
    --pre: #3987e5; --post: #d95926;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-1: #1a1a19; --plane: #0d0d0d;
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --up: #3987e5; --down: #e66767; --neutral: #383835;
  --pre: #3987e5; --post: #d95926;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--plane); color: var(--ink);
  font: 14px/1.5 ui-sans-serif, -apple-system, "Segoe UI", system-ui, sans-serif;
}
a { color: inherit; }
header {
  display: flex; gap: 20px; align-items: baseline; flex-wrap: wrap;
  padding: 14px 16px; border-bottom: 1px solid var(--border); background: var(--surface-1);
}
header h1 { font-size: 15px; margin: 0; font-weight: 650; letter-spacing: -0.01em; }
header nav a { margin-right: 14px; color: var(--ink-2); text-decoration: none; font-size: 13px; }
header nav a.on { color: var(--ink); font-weight: 600; }
main { padding: 16px; max-width: 1280px; }
.card {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px; margin-bottom: 16px;
}
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em;
     color: var(--ink-2); margin: 0 0 12px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 11px;
     text-transform: uppercase; letter-spacing: 0.05em; white-space: nowrap; }
th a { text-decoration: none; }
tbody tr:hover { background: color-mix(in oklab, var(--ink) 4%, transparent); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.muted { color: var(--muted); }
.chip {
  display: inline-block; padding: 1px 7px; margin: 1px 3px 1px 0; border-radius: 999px;
  border: 1px solid var(--border); font-size: 11px; color: var(--ink-2); white-space: nowrap;
}
.chip.pos { border-color: color-mix(in oklab, var(--up) 45%, transparent); }
.chip.neg { border-color: color-mix(in oklab, var(--down) 45%, transparent); }
/* A scramble is information, not a problem: a quiet neutral chip, not the warning amber. */
.seq { display:inline-block; margin-left:6px; padding:0 6px; border-radius:999px;
       border:1px solid var(--border); font-size:10.5px; color:var(--muted); cursor:help;
       background: color-mix(in oklab, var(--ink) 5%, transparent); }
.clip { position: relative; width: 100%; max-width: 640px; aspect-ratio: 16/9;
         border-radius: 10px; overflow: hidden; border: 1px solid var(--border);
         background: #000; margin: 4px 0 8px; }
.clip iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
.at { font-variant-numeric: tabular-nums; }
.flist { display: grid; gap: 2px; }
.flegend { display: flex; gap: 18px; flex-wrap: wrap; font-size: 12px;
           color: var(--ink-2); margin-bottom: 10px; }
.flegend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
             margin-right: 5px; vertical-align: -1px; }
.frow { padding: 7px 0; border-bottom: 1px solid var(--grid); }
.fhead { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.fcode { font-size: 12.5px; font-weight: 600; color: var(--ink); }
.fw { font-size: 12px; font-variant-numeric: tabular-nums; padding: 0 5px;
      border-radius: 4px; border: 1px solid var(--border); }
.fw.pos { color: var(--up); } .fw.neg { color: var(--down); } .fw.zero { color: var(--muted); }
.fdir { font-size: 11px; color: var(--muted); }
.fhint { font-size: 12.5px; color: var(--ink-2); margin: 2px 0 5px; max-width: 72ch; }
.fbarline { display: flex; align-items: center; gap: 9px; }
.ftrack { display: flex; gap: 2px; height: 12px; min-width: 10px; }
.seg { border-radius: 3px; min-width: 4px; }
.fcount { font-size: 12px; font-variant-numeric: tabular-nums; color: var(--ink-2); min-width: 18px; }
.fsplit { font-size: 11px; color: var(--muted); }
.grp { display: flex; align-items: baseline; gap: 4px; flex-wrap: wrap; margin: 2px 0; }
.grp-k { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
         color: var(--muted); min-width: 44px; cursor: help; }
.tiles { display: flex; gap: 10px; flex-wrap: wrap; }
.tile { flex: 1 1 150px; background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 10px; padding: 12px 14px; }
.tile .v { font-size: 24px; font-weight: 650; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.tile .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.tile .sub { font-size: 12px; color: var(--ink-2); margin-top: 2px; }
.bar { display: block; }
.filters { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
select, input[type=search] {
  font: inherit; padding: 5px 8px; border-radius: 7px;
  border: 1px solid var(--border); background: var(--surface-1); color: var(--ink);
}
.quote { border-left: 2px solid var(--axis); padding-left: 10px; color: var(--ink-2);
         margin: 6px 0; font-size: 13px; }
mark { background: color-mix(in oklab, var(--warn) 35%, transparent); color: inherit;
       padding: 0 2px; border-radius: 3px; }
.empty { color: var(--muted); padding: 28px 0; text-align: center; }
code { font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }
@media (max-width: 720px) {
  main { padding: 12px; } th, td { padding: 6px; }
  .hide-sm { display: none; }
}
`;

export function page(title: string, active: string, body: string): string {
  const nav = [["/", "Shots"], ["/pre", "Pre-shot"], ["/summary", "Summary"]] as const;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title><style>${CSS}</style></head><body>
<header>
  <h1>Commentary-adjusted xG</h1>
  <nav>${nav.map(([h, l]) => `<a href="${h}" class="${active === h ? "on" : ""}">${l}</a>`).join("")}</nav>
</header>
<main>${body}</main></body></html>`;
}
