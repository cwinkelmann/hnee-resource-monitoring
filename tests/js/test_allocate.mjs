import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
const { allocate, takesPreview } = createRequire(import.meta.url)("../../resourcemonitor/web/app.js");

// Shared with the Python tests: claims.allocate and allocate() in app.js must agree.
const fixture = JSON.parse(readFileSync(new URL("../fixtures/allocate_cases.json", import.meta.url), "utf8"));
assert.ok(fixture.cases.length > 0);
for (const k of fixture.cases) {
  assert.deepEqual(allocate(k.bookings, k.total), k.expect, k.name);
}

// takesPreview: what a new important booking would take from lendable claims.
const H = 3600e3, card = 81920, G = 1024;
const c = (id, user, gpu, vram_mib, s, e, priority = "lendable", cancelled_at = null) =>
  ({ id, user, gpu, vram_mib, priority, cancelled_at,
     start: new Date(s * H).toISOString(), end: new Date(e * H).toISOString() });
// alice holds the whole card 0–10 h; an important 40 GiB at 2–4 h takes 40 GiB from 2 h
assert.deepEqual(takesPreview([c(1, "alice", 4, 80 * G, 0, 10)], 4, 2 * H, 4 * H, 40 * G, card),
  [{ id: 1, user: "alice", mib: 40 * G, from: 2 * H, start: 0 }]);
// room left: nothing taken
assert.deepEqual(takesPreview([c(1, "alice", 4, 40 * G, 0, 10)], 4, 2 * H, 4 * H, 40 * G, card), []);
// other GPU, cancelled and important claims are never taken
assert.deepEqual(takesPreview([c(1, "alice", 5, 80 * G, 0, 10), c(2, "bob", 4, 80 * G, 0, 10, "lendable", "x"),
  c(3, "carol", 4, 40 * G, 0, 10, "important")], 4, 2 * H, 4 * H, 40 * G, card), []);
// newest lendable shrinks first; a lendable starting inside the window is taken from its start
assert.deepEqual(takesPreview([c(1, "alice", 4, 40 * G, 0, 10), c(2, "bob", 4, 40 * G, 3, 10)],
  4, 2 * H, 6 * H, 60 * G, card),
  [{ id: 1, user: "alice", mib: 20 * G, from: 2 * H, start: 0 },
   { id: 2, user: "bob", mib: 40 * G, from: 3 * H, start: 3 * H }]);
// an existing important booking already squeezes alice: only the extra is new
assert.deepEqual(takesPreview([c(1, "alice", 4, 80 * G, 0, 10), c(2, "bob", 4, 20 * G, 0, 10, "important")],
  4, 0, 2 * H, 20 * G, card),
  [{ id: 1, user: "alice", mib: 20 * G, from: 0, start: 0 }]);
console.log("ok");
