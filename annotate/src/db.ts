/**
 * Postgres access for the annotator. Reads DATABASE_URL from the repo-root .env, the same
 * variable and the same database the Python pipeline uses.
 *
 * This program never issues DDL. The schema belongs to scripts/db/migrations/, which the Python
 * layer applies on connect; if the tables are missing we say so and stop rather than guessing.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = join(HERE, "..", "..");

/** Load the repo-root .env without clobbering anything already in the environment. */
export function loadEnv(): void {
  let text: string;
  try {
    text = readFileSync(join(REPO_ROOT, ".env"), "utf8");
  } catch {
    return;
  }
  for (const line of text.split("\n")) {
    const m = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
    if (!m || line.trimStart().startsWith("#")) continue;
    const [, key, rawValue] = m;
    if (!key || key in process.env) continue;
    let value = (rawValue ?? "").trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    process.env[key] = value;
  }
}

let pool: pg.Pool | undefined;

export function db(): pg.Pool {
  if (pool) return pool;
  loadEnv();
  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error(
      "DATABASE_URL is not set. The annotator reads the live warehouse; it has no SQLite path.",
    );
  }
  // The URL says sslmode=require, which in libpq (and so in the Python pipeline's psycopg)
  // means "encrypt, don't verify the chain". pg 8.23 reinterprets `require` as `verify-full`,
  // which fails against Supabase's pooler because its chain is self-signed. `uselibpqcompat`
  // restores the semantics the URL actually asks for, rather than silently turning TLS checking
  // off: change the URL to sslmode=verify-full to get verification.
  const url = new URL(connectionString);
  if (url.searchParams.get("sslmode") === "require" && !url.searchParams.has("uselibpqcompat")) {
    url.searchParams.set("uselibpqcompat", "true");
  }

  pool = new pg.Pool({
    connectionString: url.toString(),
    max: 4,
    // The pipeline's login has a low connection limit and the session pooler drops idle ones.
    idleTimeoutMillis: 10_000,
    connectionTimeoutMillis: 20_000,
  });
  return pool;
}

/** Everything lives in the `warehouse` schema; the login cannot see anything else. */
export async function query<T extends pg.QueryResultRow = pg.QueryResultRow>(
  sql: string,
  params: unknown[] = [],
): Promise<T[]> {
  const client = await db().connect();
  try {
    await client.query("SET search_path TO warehouse");
    const res = await client.query<T>(sql, params);
    return res.rows;
  } finally {
    client.release();
  }
}

export async function close(): Promise<void> {
  await pool?.end();
  pool = undefined;
}

/** Fail loudly and usefully if the Python migrations have not been applied. */
export async function assertSchema(): Promise<void> {
  const rows = await query<{ present: string }>(
    `SELECT table_name AS present FROM information_schema.tables
      WHERE table_schema = 'warehouse'
        AND table_name IN ('commentary_xg', 'calibrations', 'shot_commentary_split')`,
  );
  const missing = ["commentary_xg", "calibrations", "shot_commentary_split"].filter(
    (t) => !rows.some((r) => r.present === t),
  );
  if (missing.length) {
    throw new Error(
      `Missing in the warehouse schema: ${missing.join(", ")}.\n` +
        "Apply the Python migrations first:  .venv/bin/python scripts/db/migrate.py status",
    );
  }
}

export interface ShotRow {
  game_id: string;
  event_id: string;
  video_id: string;
  period: number;
  clock: string;
  team: string | null;
  shooter: string | null;
  result: string;
  goal: boolean | null;
  shot_type: string | null;
  distance_ft: string | null;
  angle_deg: string | null;
  shot_rebound: boolean | null;
  shot_rush: boolean | null;
  x_goal: number | null;
  video_sec: number;
  video_time: string;
  link: string;
  commentary_pre: string | null;
  commentary_post: string | null;
  goalie: string | null;
  home_skaters: number | null;
  away_skaters: number | null;
  is_home_team: boolean | null;
  empty_net: boolean | null;
}

/** The players MoneyPuck records in a game, so mangled names can be matched to real ones. */
export interface Roster {
  game_id: string;
  skaters: string[];
  goalies: string[];
}

export async function fetchShots(opts: { game?: string; limit?: number; redo?: boolean }): Promise<ShotRow[]> {
  const where: string[] = ["s.commentary_pre IS NOT NULL"];
  const params: unknown[] = [];
  if (opts.game) {
    params.push(opts.game);
    where.push(`s.game_id = $${params.length}`);
  }
  if (!opts.redo) where.push("a.game_id IS NULL");
  const limit = opts.limit ? `LIMIT ${Number(opts.limit)}` : "";
  return query<ShotRow>(
    `SELECT s.*,
            sh.goalie_name_for_shot AS goalie,
            sh.home_skaters_on_ice  AS home_skaters,
            sh.away_skaters_on_ice  AS away_skaters,
            sh.is_home_team,
            (sh.home_empty_net OR sh.away_empty_net) AS empty_net
       FROM shot_commentary_split s
       JOIN shots sh USING (game_id, event_id)
       LEFT JOIN commentary_xg a USING (game_id, event_id)
      WHERE ${where.join(" AND ")}
      ORDER BY s.game_id, s.video_sec
      ${limit}`,
    params,
  );
}

/**
 * Who was on the ice, per game.
 *
 * The captions are machine-transcribed, so names arrive mangled -- Bonino as "Benino", Rakell as
 * "Raquel", Nugent-Hopkins as "Nan Hopkins". Without the real names to compare against, a model
 * reads a mangled spelling as a different player and reports a data error that is not there.
 * Giving it the roster turns that guess into a lookup.
 */
export async function fetchRosters(gameIds: string[]): Promise<Map<string, Roster>> {
  if (!gameIds.length) return new Map();
  const rows = await query<{ game_id: string; player: string; is_goalie: boolean }>(
    `SELECT game_id, shooter_name AS player, false AS is_goalie
       FROM shots WHERE game_id = ANY($1) AND shooter_name IS NOT NULL
     UNION
     SELECT game_id, goalie_name_for_shot, true
       FROM shots WHERE game_id = ANY($1) AND goalie_name_for_shot IS NOT NULL`,
    [gameIds],
  );
  const out = new Map<string, Roster>();
  for (const r of rows) {
    const e = out.get(r.game_id) ?? { game_id: r.game_id, skaters: [], goalies: [] };
    (r.is_goalie ? e.goalies : e.skaters).push(r.player);
    out.set(r.game_id, e);
  }
  for (const e of out.values()) {
    e.skaters.sort();
    e.goalies.sort();
  }
  return out;
}
