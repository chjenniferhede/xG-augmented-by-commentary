/**
 * A readable name for a game, for menus: "2023 Stanley Cup Final, Game 5 · FLA @ VGK, 3–9".
 *
 * The round and game number come from the NHL game ID itself. For a playoff game the ID reads
 * SSSS 03 RR S G: season, game type 03, round, series within the round, game within the series
 * (2022030415 = 2022-23 season, playoffs, round 4, series 1, game 5).
 */

export interface GameInfo {
  game_id: string;
  game_date: string | null; // YYYY-MM-DD
  away_team: string | null;
  home_team: string | null;
  away_goals: number | null;
  home_goals: number | null;
}

const ROUNDS: Record<number, string> = {
  1: "First Round", 2: "Second Round", 3: "Conference Final", 4: "Stanley Cup Final",
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function gameLabel(g: GameInfo): string {
  const id = String(g.game_id);
  const [y, m, d] = (g.game_date ?? "").split("-").map(Number);
  const year = y || Number(id.slice(0, 4)) + 1;
  const date = y && m && d ? `${MONTHS[m - 1]} ${d}, ${y}` : String(year);

  let what: string;
  if (id.slice(4, 6) === "03") {
    const round = Number(id.slice(6, 8));
    const game = Number(id.slice(9, 10));
    what = `${year} ${ROUNDS[round] ?? `Playoffs round ${round}`}, Game ${game}`;
  } else {
    what = `Regular season, ${date}`;
  }

  const teams = g.away_team && g.home_team ? `${g.away_team} @ ${g.home_team}` : "";
  const score = g.away_goals !== null && g.home_goals !== null && teams ? `, ${g.away_goals}–${g.home_goals}` : "";
  return teams ? `${what} · ${teams}${score}` : what;
}
