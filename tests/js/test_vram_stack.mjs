import assert from "node:assert/strict";
import { createRequire } from "node:module";
const { stackSeries, vramBucketOrder, bookedSteps } =
  createRequire(import.meta.url)("../../resourcemonitor/web/app.js");

// Stacking: bottom-up in the given order, a missing bucket counts as 0.
const pts = [
  { by_user: { b: 10, a: 5, "(unattributed)": 1 } },
  { by_user: {} },
  { by_user: { a: 7 } },
];
assert.deepEqual(stackSeries(pts, ["a", "b", "(unattributed)"]), [
  { bucket: "a", lower: [0, 0, 0], upper: [5, 0, 7] },
  { bucket: "b", lower: [5, 0, 7], upper: [15, 0, 7] },
  { bucket: "(unattributed)", lower: [15, 0, 7], upper: [16, 0, 7] },
]);
assert.deepEqual(stackSeries([], ["a"]), [{ bucket: "a", lower: [], upper: [] }]);
assert.deepEqual(stackSeries(pts, []), []);

// Order: users alphabetically, unattributed last; only buckets that held something.
assert.deepEqual(vramBucketOrder(pts), ["a", "b", "(unattributed)"]);
assert.deepEqual(vramBucketOrder([{ by_user: { "(unattributed)": 3, zed: 0, ann: 2 } }]),
  ["ann", "(unattributed)"]);
assert.deepEqual(vramBucketOrder([{ by_user: {} }]), []);

// Booked share: a step line of the sum of live bookings on that GPU.
const H = 3600e3;
const c = (gpu, vram_mib, s, e, cancelled_at = null) => ({
  gpu, vram_mib, start: new Date(s * H).toISOString(), end: new Date(e * H).toISOString(),
  cancelled_at: cancelled_at === null ? null : new Date(cancelled_at * H).toISOString(),
});
assert.deepEqual(bookedSteps([], 4, 0, 24 * H), []);                          // no bookings: no line
assert.deepEqual(bookedSteps([c(5, 100, 0, 30)], 4, 0, 24 * H), []);          // other GPU
assert.deepEqual(bookedSteps([c(4, 100, -10, -1)], 4, 0, 24 * H), []);        // before the window
assert.deepEqual(bookedSteps([c(4, 100, 2, 6), c(4, 50, 4, 30)], 4, 0, 24 * H), [
  { t: 0, mib: 0 }, { t: 2 * H, mib: 100 }, { t: 4 * H, mib: 150 }, { t: 6 * H, mib: 50 },
  { t: 24 * H, mib: 50 },
]);
assert.deepEqual(bookedSteps([c(4, 100, -5, 30, 3)], 4, 0, 24 * H), [          // cancelled at 3 h
  { t: 0, mib: 100 }, { t: 3 * H, mib: 0 }, { t: 24 * H, mib: 0 },
]);
console.log("ok");
