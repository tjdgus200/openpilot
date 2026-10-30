import assert from "node:assert/strict";
import test from "node:test";
import {fusionRows} from "../src/features/tools/input_fusion.js";

const row = (id) => ({id, n: 1201, seconds: 60, mean_ms: 25.123, p99_ms: 26.456,
  max_ms: 27, over50: 0});

test("two paths remain visible without invented results", () => {
  assert.deepEqual(fusionRows(null).map((r) => r.cells), [Array(5).fill("--"), Array(5).fill("--")]);
});
test("receive to TX-ready retains sub-millisecond precision", () => {
  const rows = fusionRows({effective: 1, rows: [row(0), row(1)]});
  assert.equal(rows[1].active, true);
  assert.deepEqual(rows[1].cells, ["1201", "25.123", "26.456", "27.000", "0"]);
  assert.equal(rows[1].ready, true);
});
test("invalid values and insufficient samples cannot qualify", () => {
  const rows = fusionRows({rows: [{...row(0), p99_ms: NaN}, {...row(1), n: 999}]});
  assert.deepEqual(rows[0].cells, Array(5).fill("--"));
  assert.equal(rows[1].ready, false);
});
