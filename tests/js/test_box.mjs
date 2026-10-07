import assert from "node:assert/strict";
import { createRequire } from "node:module";
const { fmtCores, coresFloorLabel, gibFloorLabel, boxRunPeak, topBoxUsers } =
  createRequire(import.meta.url)("../../resourcemonitor/web/app.js");

// Cores: one decimal; null (first poll after a restart) is a dash, not 0.
assert.equal(fmtCores(37.44), "37.4");
assert.equal(fmtCores(0), "0.0");
assert.equal(fmtCores(null), "—");
assert.equal(fmtCores(undefined), "—");

// Timeline row sub-labels from the server's thresholds.
assert.equal(coresFloorLabel(1), "≥ 1 core");
assert.equal(coresFloorLabel(4), "≥ 4 cores");
assert.equal(coresFloorLabel(0.5), "≥ 0.5 cores");
assert.equal(gibFloorLabel(65536), "≥ 64 GiB");
assert.equal(gibFloorLabel(1536), "≥ 1.5 GiB");

// Run peaks: cores for CPU runs, GiB (from MiB) for RAM runs.
assert.equal(boxRunPeak("cpu", 12.34), "peak 12.3 cores");
assert.equal(boxRunPeak("ram", 81920), "peak 80.0 GiB RAM");

// The card lists at most n users, in the server's order; null box -> nothing.
const users = Array.from({ length: 10 }, (_, i) => ({ user: "u" + i, cores: 10 - i, rss_mib: 1024 }));
assert.deepEqual(topBoxUsers({ users }, 8).map((u) => u.user),
  ["u0", "u1", "u2", "u3", "u4", "u5", "u6", "u7"]);
assert.deepEqual(topBoxUsers(null, 8), []);
assert.deepEqual(topBoxUsers({ users: [] }, 8), []);
console.log("ok");
