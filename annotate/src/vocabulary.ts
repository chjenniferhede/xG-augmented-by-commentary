/**
 * What commentary can tell us about a shot that MoneyPuck's xG does not already know.
 *
 * MoneyPuck's model is built on the NHL's public play-by-play: location, angle, shot type,
 * rebound (<3 s) and rush flags, strength state, empty net, score differential. Re-encoding any
 * of that here would double-count it and make the adjustment worse, so none of it appears below.
 * What the public feed has never had -- and what broadcasters say out loud -- is whether the
 * goalie could see the puck, what happened to it on the way, where the goalie was, how the puck
 * got to the shooter, and what state the shooter was in. That is groups A-E.
 *
 * Group G is the one exception to the no-double-counting rule: there the commentary contradicts
 * the stored row, which is a data-quality signal rather than an xG adjustment (weight 0).
 */

export type Polarity = "positive" | "negative" | "neutral";

export interface FeatureDef {
  code: string;
  group: string;
  /** Shown to the model. One line, concrete, phrased as what a commentator would actually say. */
  hint: string;
  polarity: Polarity;
}

export const FEATURES: readonly FeatureDef[] = [
  // A. Goalie sight line -- absent from every public xG model.
  { code: "screen_opponent", group: "A", polarity: "positive",
    hint: "goalie's view blocked by an attacking player ('he never saw it', 'screened')" },
  { code: "screen_own_defender", group: "A", polarity: "positive",
    hint: "goalie screened by his OWN defenceman" },
  { code: "traffic_heavy", group: "A", polarity: "positive",
    hint: "bodies/scramble in front of the crease at the moment of the shot" },
  { code: "clear_sight", group: "A", polarity: "negative",
    hint: "goalie had a clean look, nobody in front ('saw it all the way')" },

  // B. What happened to the puck on the way to the net.
  { code: "deflect_off_defender", group: "B", polarity: "positive",
    hint: "changed direction off a defender's stick or body and still reached the net" },
  { code: "deflect_off_teammate", group: "B", polarity: "positive",
    hint: "tipped by a teammate (ONLY if shot_type is not already TIP-IN/DEFLECTED)" },
  { code: "partial_block", group: "B", polarity: "negative",
    hint: "a defender got a piece of it, taking pace or accuracy off the shot" },
  { code: "shot_blocked_lane", group: "B", polarity: "negative",
    hint: "a defender in the lane forced the shooter to alter or rush the shot" },
  { code: "off_skate_or_body", group: "B", polarity: "positive",
    hint: "fortunate carom off a skate, body or the boards into a dangerous spot" },

  // C. Goalie state.
  { code: "goalie_out_of_position", group: "C", polarity: "positive",
    hint: "caught moving, on the wrong post, too deep or too far out" },
  { code: "goalie_down_scrambling", group: "C", polarity: "positive",
    hint: "down and out, scrambling, no push left" },
  { code: "goalie_set_square", group: "C", polarity: "negative",
    hint: "squared up, in position, controlled the shot comfortably" },
  { code: "goalie_cold", group: "C", polarity: "positive",
    hint: "has not faced a shot in a long time; explicitly remarked on" },
  { code: "backup_just_in", group: "C", polarity: "positive",
    hint: "goalie has only just entered the game" },

  // D. Pre-shot puck movement -- the other big gap in public xG.
  { code: "cross_ice_pass", group: "D", polarity: "positive",
    hint: "east-west / cross-ice feed across the slot immediately before the shot (royal road)" },
  { code: "one_timer", group: "D", polarity: "positive",
    hint: "shot first time, without settling the puck" },
  { code: "behind_net_feed", group: "D", polarity: "positive",
    hint: "pass out from behind the goal line" },
  { code: "backdoor", group: "D", polarity: "positive",
    hint: "weak-side player at the far post with the goalie on the other side" },
  { code: "slot_feed", group: "D", polarity: "positive",
    hint: "pass into the high-danger slot immediately before the shot" },
  { code: "no_pass_perimeter", group: "D", polarity: "negative",
    hint: "shooter carried or held it with no puck movement; a perimeter shot into the goalie" },

  // E. Shooter state.
  { code: "shooter_unpressured", group: "E", polarity: "positive",
    hint: "time and space, nobody closing him down" },
  { code: "shooter_wide_open_slot", group: "E", polarity: "positive",
    hint: "alone and unmarked in the slot" },
  { code: "quick_release", group: "E", polarity: "positive",
    hint: "got it away before the goalie was set" },
  { code: "shooter_pressured", group: "E", polarity: "negative",
    hint: "stick-checked, hounded, shooting off the back foot" },
  { code: "shooter_falling", group: "E", polarity: "negative",
    hint: "off balance, falling or stumbling as he shot" },
  { code: "fanned_or_weak", group: "E", polarity: "negative",
    hint: "mishit, fluttered, 'nothing on it', fanned on it" },
  { code: "bouncing_puck", group: "E", polarity: "negative",
    hint: "rolling or bouncing puck, bad ice, puck on edge" },
  { code: "broken_stick", group: "E", polarity: "negative",
    hint: "the shooter's stick broke on the shot" },

  // F. Rare situations -- emit only when they actually occur.
  { code: "breakaway", group: "F", polarity: "positive",
    hint: "clean break with no defender between shooter and goalie" },
  { code: "penalty_shot", group: "F", polarity: "positive", hint: "an awarded penalty shot" },
  { code: "delayed_penalty_extra_attacker", group: "F", polarity: "positive",
    hint: "delayed penalty, goalie pulled for a sixth attacker" },
  { code: "second_chance_scramble", group: "F", polarity: "positive",
    hint: "several whacks at a loose puck in the crease, beyond a single rebound" },
  { code: "net_off_moorings", group: "F", polarity: "neutral", hint: "the net came off its moorings" },
  { code: "puck_off_official", group: "F", polarity: "neutral", hint: "the puck hit an official" },

  // G. The stored row looks wrong. No xG effect; a data-quality signal. Needs corrected_value.
  { code: "shot_type_correction", group: "G", polarity: "neutral",
    hint: "commentary clearly describes a different shot type than the data records" },
  { code: "shooter_correction", group: "G", polarity: "neutral",
    hint: "commentary clearly names a different shooter than the data records" },
  { code: "location_dispute", group: "G", polarity: "neutral",
    hint: "commentary describes a position irreconcilable with the recorded distance" },

  // Escape hatch: weight 0, logged so the vocabulary can grow from what actually turns up.
  { code: "other", group: "H", polarity: "neutral",
    hint: "something materially affecting the chance that no code above covers; explain in quote" },
] as const;

export const FEATURE_CODES = FEATURES.map((f) => f.code) as [string, ...string[]];
const BY_CODE = new Map(FEATURES.map((f) => [f.code, f]));
export const featureDef = (code: string): FeatureDef | undefined => BY_CODE.get(code);

/**
 * Codes that cannot both be true of one shot. The model does emit both sometimes -- usually when
 * the pre-shot and post-shot windows describe different moments -- so the scorer keeps only the
 * higher-confidence member rather than letting them cancel out to a misleading zero.
 */
export const EXCLUSIVE_GROUPS: readonly (readonly string[])[] = [
  ["screen_opponent", "clear_sight"],
  ["screen_own_defender", "clear_sight"],
  ["traffic_heavy", "clear_sight"],
  ["goalie_out_of_position", "goalie_set_square"],
  ["goalie_down_scrambling", "goalie_set_square"],
  ["shooter_unpressured", "shooter_pressured"],
  ["shooter_wide_open_slot", "shooter_pressured"],
  ["cross_ice_pass", "no_pass_perimeter"],
  ["slot_feed", "no_pass_perimeter"],
  ["behind_net_feed", "no_pass_perimeter"],
];

/** What MoneyPuck already encodes. Named in the prompt so the model does not re-report it. */
export const ALREADY_IN_MODEL = [
  "shot distance and angle",
  "shot type (wrist/slap/snap/backhand/tip-in/deflected/wrap-around)",
  "whether it was a rebound (within 3 s of a previous shot)",
  "whether it came off the rush",
  "strength state (power play, penalty kill, 4-on-4)",
  "empty net",
  "score and period",
] as const;
