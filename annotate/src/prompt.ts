/**
 * The prompt. Two jobs, in priority order:
 *   1. Stop the model inventing hockey. Most windows say nothing useful about the shot in hand,
 *      and a free-tier model will happily fill that silence. "Nothing here" must be the easy,
 *      expected answer, so the instructions say so repeatedly and the schema demands a verbatim
 *      quote for anything claimed.
 *   2. Keep the outcome out of the pre-shot judgement, so xg_adjusted_pre stays honest.
 *
 * Kept terse on purpose: without Anthropic-style prompt caching the system text is re-sent on
 * every call, and on a free tier the budget is tokens per day.
 */

import { FEATURES, ALREADY_IN_MODEL } from "./vocabulary.ts";
import type { Roster, ShotRow } from "./db.ts";

const byGroup = () => {
  const groups = new Map<string, string[]>();
  for (const f of FEATURES) {
    const line = `  ${f.code}: ${f.hint}`;
    const g = groups.get(f.group) ?? [];
    g.push(line);
    groups.set(f.group, g);
  }
  return groups;
};

const GROUP_TITLES: Record<string, string> = {
  A: "Goalie sight line",
  B: "What happened to the puck on the way",
  C: "Goalie state",
  D: "How the puck reached the shooter",
  E: "Shooter state",
  F: "Rare situations (only if they truly occur)",
  G: "The recorded data looks wrong (set corrected_value; these never change xG)",
  H: "Catch-all",
};

export function systemPrompt(): string {
  const groups = [...byGroup()]
    .map(([g, lines]) => `${g}. ${GROUP_TITLES[g]}\n${lines.join("\n")}`)
    .join("\n");

  return `You read NHL broadcast commentary and extract only what an expected-goals (xG) model cannot already see.

You are given a cluster of shots that happened close together in one game, and the commentary around them. Caption text is machine-transcribed from the broadcast, so names arrive mangled: Bonino as "Benino", Rakell as "Raquel", Nugent-Hopkins as "Nan Hopkins", Letestu as "Mark Latest". You are given the players who were actually in the game. A mangled name that sounds like someone on that list IS that player -- treat it as confirmation, never as a contradiction. Only report a different player when the commentary names someone who is plainly a different person from the one recorded.

You are also told which goalie faced each shot. A save by that goalie is the outcome of the shot, not a feature of it: "blocked away by <goalie>", "<goalie> got that" means the goalie stopped it. Never record that as a blocked shooting lane or a deflection -- a blocked lane means an outfield player was in the way before the puck reached the goalie.

THE DEFAULT ANSWER IS "NOTHING".
Most of these windows are pre-game talk, statistics, player backstory, or the call of a *different* shot a few seconds away. If the commentary does not clearly describe the shot in question, set relevance near 0 and return no features. An empty features list is a correct, expected, common answer. Do not reach.

EVIDENCE RULE.
Every feature must include a quote copied word-for-word from the commentary text given to you. If you cannot copy an exact supporting phrase, do not emit the feature. Paraphrases are rejected automatically.

DO NOT REPORT WHAT THE MODEL ALREADY KNOWS.
The xG model already accounts for: ${ALREADY_IN_MODEL.join("; ")}. Never emit a feature for these. The one exception is group G, where the commentary flatly contradicts the recorded data.

THE OUTCOME IS OFF LIMITS.
Never state, imply, or let your scoring depend on whether a shot scored or was saved. Judge only how good the chance was at the moment of release. You get two windows per shot: PRE (up to the shot) and POST (the replay talk after). Replay talk often contains the best detail about screens, deflections and goalie position -- use it, and tag those features source="post" -- but ignore the result itself. Set outcome_leaked=true ONLY when the BEFORE-the-shot window already gives the result away -- that happens when a caption line straddles the moment of the shot, and it is the thing we measure. The after-the-shot window reveals the result for nearly every shot; that is by design, so it is NOT a leak and must not set the flag.

CONFIDENCE.
Above 0.8 only when the commentary states the thing outright. Use 0.4-0.6 when it is implied. If you are guessing, omit the feature.

FEATURE CODES
${groups}

Return one entry per shot listed, using its event_id, even when there is nothing to report.`;
}

const fmt = (v: unknown, dash = "unknown") => (v === null || v === undefined ? dash : String(v));

/** One cluster of shots plus the commentary shared around them. */
export function userPrompt(shots: ShotRow[], roster?: Roster): string {
  const lines = shots.map((s) => {
    const forStrength = s.is_home_team ? s.home_skaters : s.away_skaters;
    const against = s.is_home_team ? s.away_skaters : s.home_skaters;
    const flags = [
      s.shot_rebound ? "rebound" : null,
      s.shot_rush ? "off the rush" : null,
      forStrength && against && forStrength !== against ? `${forStrength}-on-${against}` : null,
      s.empty_net ? "empty net" : null,
    ].filter(Boolean);
    return [
      `SHOT event_id=${s.event_id}`,
      `  period ${s.period} at ${s.clock}, ${fmt(s.team)}, shooter as recorded: ${fmt(s.shooter)}`,
      `  goalie facing it: ${fmt(s.goalie)}`,
      `  recorded type ${fmt(s.shot_type)}, ${fmt(s.distance_ft)} ft, angle ${fmt(s.angle_deg)} deg` +
        (flags.length ? ` (${flags.join(", ")})` : ""),
      `  video time ${s.video_time}`,
      "",
      `  COMMENTARY BEFORE THIS SHOT:`,
      `  ${s.commentary_pre ?? "(none)"}`,
      "",
      `  COMMENTARY AFTER THIS SHOT (replay talk; the result itself is off limits):`,
      `  ${s.commentary_post ?? "(none)"}`,
    ].join("\n");
  });

  const who = roster
    ? [
        "",
        "PLAYERS IN THIS GAME (the transcription mangles these; match by sound, do not report a mangled spelling as a different player):",
        `  goalies: ${roster.goalies.join(", ")}`,
        `  skaters: ${roster.skaters.join(", ")}`,
      ].join("\n")
    : "";

  return [
    `${shots.length} shot${shots.length === 1 ? "" : "s"} from game ${shots[0]?.game_id}, close together in time.`,
    who,
    shots.length > 1
      ? "Their commentary windows overlap, so the same words appear more than once below. Decide which shot each detail is about; if a detail belongs to a neighbouring shot, do not attribute it here."
      : "",
    "",
    lines.join("\n\n"),
  ]
    .filter(Boolean)
    .join("\n");
}
