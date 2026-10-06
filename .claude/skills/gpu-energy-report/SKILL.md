---
name: gpu-energy-report
description: Use when asked how much electricity or GPU time the shared box or a particular person has used, to produce a weekly energy report, or to explain why a kWh figure looks lower than expected. Covers the uptime-only caveat and the per-card attribution proxy.
---

# GPU energy and occupancy reports

```bash
ssh cwinkelmann@10.188.1.1 'cd ~/ResourceMonitor && python3 -m resourcemonitor report'
```

Add `--post` to send it to Slack instead of printing it.

## Two caveats that must travel with every number

1. **It measures uptime, not history.** The driver on carrot exposes no
   `total_energy_consumption` counter, so energy is `Σ power × Δt` over the monitor's own
   polls. Any period when the service was down is simply missing — the total is a floor,
   not a meter reading. If a figure looks low, check `systemctl --user status` before
   concluding usage was low.
2. **Per-user figures are an estimate.** `power.draw` is per *card*. One process on a card
   gives exact attribution; several are split by memory share, which is a proxy for
   compute, not a measurement. Say so when quoting a per-person number.

## Reference points (measured 2026-10-06)

- An **idle** H100 still draws ~66 W, so six idle cards ≈ 400 W ≈ 9.6 kWh/day.
- A busy card drew 557–575 W against a 700 W limit.
- The box totalled 1,532 W with 2 of 8 cards busy ≈ 36.8 kWh/day.

Idle draw is attributed to nobody and reported on its own line. It is usually the most
actionable number in the report: it is the cost of cards nobody is using.

## Sanity checks before sending a report

- Does `per_gpu` sum to roughly `per_user + idle`? A large gap means unattributed
  processes — check for `None` owners.
- Is `since` when you think the service started? If it is more recent, it restarted.
