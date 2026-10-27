/**
 * Feature tags, split by which window the evidence came from.
 *
 * Keeping them apart matters: only the pre-shot ones feed the number that can be checked against
 * real goals, and in practice most features arrive during the replay, so a single mixed list
 * quietly overstates how much of the adjustment is scoreable.
 */

import { esc } from "./layout.ts";
import { featureDef } from "../vocabulary.ts";
import { WEIGHTS, type ScoredFeature } from "../coefficients.ts";

export const chip = (f: ScoredFeature): string => {
  const d = featureDef(f.code);
  const cls = d?.polarity === "positive" ? "pos" : d?.polarity === "negative" ? "neg" : "";
  const w = WEIGHTS[f.code] ?? 0;
  return `<span class="chip ${cls}" title="${esc(f.quote)}">${esc(f.code)} ${f.confidence.toFixed(1)}${
    w ? ` \u00b7 ${w > 0 ? "+" : ""}${w}` : ""
  }</span>`;
};

export const chips = (features: ScoredFeature[] | null, only?: "pre" | "post"): string => {
  const all = features ?? [];
  if (only) {
    const list = all.filter((f) => f.source === only);
    return list.length ? list.map(chip).join("") : "";
  }
  if (!all.length) return "";
  const group = (label: string, fs: ScoredFeature[], hint: string) =>
    fs.length
      ? `<div class="grp"><span class="grp-k" title="${hint}">${label}</span>${fs.map(chip).join("")}</div>`
      : "";
  return (
    group("before", all.filter((f) => f.source === "pre"),
          "Said before the shot - feeds the scoreable pre-shot number") +
    group("replay", all.filter((f) => f.source === "post"),
          "Said after the shot - richer, but it has seen the outcome")
  );
};
