import { test } from "node:test";
import assert from "node:assert/strict";
import { gameLabel } from "./gameLabel.ts";

const base = { away_team: "FLA", home_team: "VGK", away_goals: 3, home_goals: 9 };

test("a playoff game reads its round and game number from the ID", () => {
  assert.equal(
    gameLabel({ ...base, game_id: "2022030415", game_date: "2023-06-13" }),
    "2023 Stanley Cup Final, Game 5 · FLA @ VGK, 3–9",
  );
  assert.match(gameLabel({ ...base, game_id: "2019030121", game_date: "2020-08-11" }), /^2020 First Round, Game 1/);
});

test("a regular-season game shows its date", () => {
  assert.equal(
    gameLabel({ ...base, game_id: "2025020651", game_date: "2026-01-05" }),
    "Regular season, Jan 5, 2026 · FLA @ VGK, 3–9",
  );
});

test("missing teams or date still give a usable name", () => {
  assert.equal(
    gameLabel({ game_id: "2017030226", game_date: null, away_team: null, home_team: null, away_goals: null, home_goals: null }),
    "2018 Second Round, Game 6",
  );
});
