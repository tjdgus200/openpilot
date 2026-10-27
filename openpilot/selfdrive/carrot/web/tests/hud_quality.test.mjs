import assert from "node:assert/strict";
import test from "node:test";
import {qualityRows, HUD_QUALITY_LABELS} from "../src/features/tools/hud_quality.js";

const row = (id, p99 = 27) => ({id, n: 1201, seconds: 60, mean_ms: 25, p99_ms: p99,
  max_ms: 30, over50: 0, server_p99_ms: 29, server_over50: 0});

test("all five frame rates remain visible before measurements", () => {
  const rows = qualityRows(null);
  assert.equal(rows.length, 5);
  assert.equal(HUD_QUALITY_LABELS.length, 8);
  assert.ok(rows.every((r) => r.cells.every((c) => c === "--") && !r.best));
});

test("actual observed values and selected profile are displayed", () => {
  const rows = qualityRows({effective: 5, best: 5, rows: [row(0, 29), row(5)]});
  assert.equal(rows[5].active, true);
  assert.equal(rows[5].best, true);
  assert.deepEqual(rows[5].cells, ["1201", "25.0 / 27.0", "0 (0.00%)", "29.0"]);
  assert.equal(rows[4].ready, false);
});

test("invalid samples are never formatted as measured results", () => {
  const rows = qualityRows({rows: [{...row(5), p99_ms: NaN}]});
  assert.deepEqual(rows[5].cells, ["--", "--", "--", "--"]);
});

test("a JPEG fallback marks the actual H264 output", () => {
  const rows = qualityRows({active: 5, effective: 0, state: "fallback", best: null, rows: []});
  assert.equal(rows[0].active, true);
  assert.equal(rows[5].active, false);
  assert.ok(rows.every((r) => !r.best));
});

 test("fixed Q92 trials use separate fps rows", () => {
  const rows = qualityRows({kind:"fps-q92", effective:4, rows:[row(4)]});
  assert.equal(rows.length, 5);
  assert.equal(rows[4].label, "Q92 · 30 fps");
  assert.equal(rows[4].active,true);
});
