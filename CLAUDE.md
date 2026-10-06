# CLAUDE.md — ResourceMonitor

A read-only watcher for the shared GPU box. It attributes GPU occupancy and electricity
to users and posts to Slack.

## The box

`ssh cwinkelmann@10.188.1.1` — **the name "carrot" does not resolve in DNS, use the IP.**
Key-based auth, no password. 8 × H100 80GB HBM3, shared with `dorian.zwanzig` (UID 1053);
the split is dorian 0–3, cwinkelmann 4–7.

Run it with `/usr/bin/python3` (3.12.3). conda is deliberately not used and is not on
`PATH` over non-interactive ssh — the tool is stdlib-only so it keeps working when envs
come and go.

## Non-negotiables

- **It never kills, signals or reprioritises anything.** Observation only. If a change
  would add a write path to another user's process, it does not belong here.
- **It runs on the host, outside Docker.** A rootless container rewrites every foreign
  UID to 65534, so attribution silently becomes `nobody`. Verified 2026-10-06; see the
  plan's Measured facts. Do not "containerise it for consistency".
- **Stdlib only.** No requests, no pynvml, no venv — it must survive a shared box with
  no maintenance.
- **Dry-run is the default.** `--post` is opt-in.
- **The Slack webhook is a bearer credential.** It lives in `~/.config/resourcemonitor/env`
  at mode 0600, is read from the environment, and never appears in a log, a repr or a
  traceback.

## Layout

`probe.py` is the only module that shells out. Everything else is a pure function of a
`Snapshot`, which is why the rules are testable on a laptop with no GPU. Keep that seam.

## Energy numbers are estimates

This driver has no `total_energy_consumption` counter, so energy is `Σ power × Δt` over
the tool's own polls: it measures **only while the service was running**. `power.draw` is
per card, so when several processes share a GPU the split is by memory share — a proxy.
Any report that states a kWh figure must also state both caveats.

## The GPU split

dorian 0–3, cwinkelmann 4–7 (agreed 2026-08-19). It lives in
`~/.config/resourcemonitor/policy.toml`, not in code.

## Skills

- `deploy-resourcemonitor` — install or update the service on carrot
- `gpu-energy-report` — produce a per-user energy and occupancy report

---

# context-mode — MANDATORY routing rules

You have context-mode MCP tools available. These rules are NOT optional — they protect your context window from flooding. A single unrouted command can dump 56 KB into context and waste the entire session.

## BLOCKED commands — do NOT attempt these

### curl / wget — BLOCKED
Any Bash command containing `curl` or `wget` is intercepted and replaced with an error message. Do NOT retry.
Instead use:
- `ctx_fetch_and_index(url, source)` to fetch and index web pages
- `ctx_execute(language: "javascript", code: "const r = await fetch(...)")` to run HTTP calls in sandbox

### Inline HTTP — BLOCKED
Any Bash command containing `fetch('http`, `requests.get(`, `requests.post(`, `http.get(`, or `http.request(` is intercepted and replaced with an error message. Do NOT retry with Bash.
Instead use:
- `ctx_execute(language, code)` to run HTTP calls in sandbox — only stdout enters context

### WebFetch — BLOCKED
WebFetch calls are denied entirely. The URL is extracted and you are told to use `ctx_fetch_and_index` instead.
Instead use:
- `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` to query the indexed content

## REDIRECTED tools — use sandbox equivalents

### Bash (>20 lines output)
Bash is ONLY for: `git`, `mkdir`, `rm`, `mv`, `cd`, `ls`, `npm install`, `pip install`, and other short-output commands.
For everything else, use:
- `ctx_batch_execute(commands, queries)` — run multiple commands + search in ONE call
- `ctx_execute(language: "shell", code: "...")` — run in sandbox, only stdout enters context

### Read (for analysis)
If you are reading a file to **Edit** it → Read is correct (Edit needs content in context).
If you are reading to **analyze, explore, or summarize** → use `ctx_execute_file(path, language, code)` instead. Only your printed summary enters context. The raw file content stays in the sandbox.

### Grep (large results)
Grep results can flood context. Use `ctx_execute(language: "shell", code: "grep ...")` to run searches in sandbox. Only your printed summary enters context.

## Tool selection hierarchy

1. **GATHER**: `ctx_batch_execute(commands, queries)` — Primary tool. Runs all commands, auto-indexes output, returns search results. ONE call replaces 30+ individual calls.
2. **FOLLOW-UP**: `ctx_search(queries: ["q1", "q2", ...])` — Query indexed content. Pass ALL questions as array in ONE call.
3. **PROCESSING**: `ctx_execute(language, code)` | `ctx_execute_file(path, language, code)` — Sandbox execution. Only stdout enters context.
4. **WEB**: `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` — Fetch, chunk, index, query. Raw HTML never enters context.
5. **INDEX**: `ctx_index(content, source)` — Store content in FTS5 knowledge base for later search.

## Subagent routing

When spawning subagents (Agent/Task tool), the routing block is automatically injected into their prompt. Bash-type subagents are upgraded to general-purpose so they have access to MCP tools. You do NOT need to manually instruct subagents about context-mode.

## Output constraints

- Keep responses under 500 words.
- Write artifacts (code, configs, PRDs) to FILES — never return them as inline text. Return only: file path + 1-line description.
- When indexing content, use descriptive source labels so others can `ctx_search(source: "label")` later.

## ctx commands

| Command | Action |
|---------|--------|
| `ctx stats` | Call the `ctx_stats` MCP tool and display the full output verbatim |
| `ctx doctor` | Call the `ctx_doctor` MCP tool, run the returned shell command, display as checklist |
| `ctx upgrade` | Call the `ctx_upgrade` MCP tool, run the returned shell command, display as checklist |
