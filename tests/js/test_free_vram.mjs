import assert from "node:assert/strict";
import { createRequire } from "node:module";
const { freeVramMiB } = createRequire(import.meta.url)("../../resourcemonitor/web/app.js");
const H = 3600e3, card = 81559;
const c = (gpu, vram_mib, s, e, cancelled_at = null) => ({ gpu, vram_mib, start: new Date(s * H).toISOString(), end: new Date(e * H).toISOString(), cancelled_at });
assert.equal(freeVramMiB([], 4, 0, 4 * H, card), card);
assert.equal(freeVramMiB([c(4, 40960, 0, 2)], 4, 0, 4 * H, card), card - 40960);
assert.equal(freeVramMiB([c(4, 30720, 0, 2), c(4, 30720, 1, 3)], 4, 0, 4 * H, card), card - 61440);  // worst instant 1–2 h
assert.equal(freeVramMiB([c(4, 40960, 0, 2)], 4, 2 * H, 4 * H, card), card);                          // back-to-back
assert.equal(freeVramMiB([c(4, 40960, 0, 2, "x")], 4, 0, 4 * H, card), card);                         // cancelled
assert.equal(freeVramMiB([c(5, 40960, 0, 2)], 4, 0, 4 * H, card), card);                              // other GPU
console.log("ok");
