import assert from "node:assert/strict";
import { createRequire } from "node:module";
const { makeDebouncer } = createRequire(import.meta.url)("../../resourcemonitor/web/app.js");

// A fake clock: schedule() queues callbacks, advance() runs the ones that are due.
let now = 0, nextId = 1;
const timers = new Map();
const schedule = (fn, ms) => { const id = nextId++; timers.set(id, { at: now + ms, fn }); return id; };
const cancel = (id) => { timers.delete(id); };
const advance = (ms) => {
  now += ms;
  for (const [id, t] of [...timers].sort((a, b) => a[1].at - b[1].at)) {
    if (t.at <= now && timers.has(id)) { timers.delete(id); t.fn(); }
  }
};
const sent = [];
const d = makeDebouncer(600, (key, value) => sent.push([key, value]), schedule, cancel);

// Arrow keys on a closed select: one change per keystroke, only the last name is sent.
d.push(3, "a"); advance(100); d.push(3, "b"); advance(100); d.push(3, "c");
assert.deepEqual(sent, []);
assert.equal(d.pending(3), true);
advance(599); assert.deepEqual(sent, []);
advance(1);   assert.deepEqual(sent, [[3, "c"]]);
assert.equal(d.pending(3), false);

// Each GPU has its own timer.
d.push(1, "x"); d.push(2, "y"); advance(600);
assert.deepEqual(sent.slice(1), [[1, "x"], [2, "y"]]);

// Enter / blur: a pending pick is sent at once, and not again when its timer would fire.
d.push(4, "p"); d.push(4, "q"); d.flush(4);
assert.deepEqual(sent.slice(3), [[4, "q"]]);
advance(1000);
assert.equal(sent.length, 4);

// flush with nothing pending does nothing.
d.flush(4); d.flush(7);
assert.equal(sent.length, 4);
console.log("ok");
