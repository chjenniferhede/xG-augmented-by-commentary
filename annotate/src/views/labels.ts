/**
 * Plain-English names for the feature codes, for pages an outsider reads. The codes and their
 * hints in vocabulary.ts are written for the model; these are written for people.
 */

const LABELS: Readonly<Record<string, string>> = {
  screen_opponent: "Screened goalie",
  screen_own_defender: "Screened by own defender",
  traffic_heavy: "Traffic in front",
  clear_sight: "Goalie saw it clearly",
  deflect_off_defender: "Deflected off a defender",
  deflect_off_teammate: "Tipped by a teammate",
  partial_block: "Partly blocked",
  shot_blocked_lane: "Shooting lane blocked",
  off_skate_or_body: "Off a skate or body",
  goalie_out_of_position: "Goalie out of position",
  goalie_down_scrambling: "Goalie down, scrambling",
  goalie_set_square: "Goalie set and square",
  goalie_cold: "Goalie cold",
  backup_just_in: "Backup goalie just in",
  cross_ice_pass: "Cross-ice pass",
  one_timer: "One-timer",
  behind_net_feed: "Pass from behind the net",
  backdoor: "Back-door play",
  slot_feed: "Pass into the slot",
  no_pass_perimeter: "Perimeter shot, no pass",
  shooter_unpressured: "Shooter unpressured",
  shooter_wide_open_slot: "Wide open in the slot",
  quick_release: "Quick release",
  shooter_pressured: "Shooter under pressure",
  shooter_falling: "Shooter falling",
  fanned_or_weak: "Weak or fanned shot",
  bouncing_puck: "Bouncing puck",
  broken_stick: "Broken stick",
  breakaway: "Breakaway",
  penalty_shot: "Penalty shot",
  delayed_penalty_extra_attacker: "Extra attacker",
  second_chance_scramble: "Rebound scramble",
  net_off_moorings: "Net off its moorings",
  puck_off_official: "Off an official",
};

/** "cross_ice_pass" -> "Cross-ice pass"; unknown codes fall back to their words. */
export const featureLabel = (code: string): string => {
  const known = LABELS[code];
  if (known) return known;
  const words = code.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
};
