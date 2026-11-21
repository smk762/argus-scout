# Signals and recommendations

Signal rules are how scout notices things. Each rule is a PromQL question with a threshold and a recommendation
to propose when the answer crosses it. They live in one YAML file (`files.signals` in `scout.yaml`) and replace
hard-coded analyzers: anything of the form "ask Prometheus, compare each series, propose something about the ones
that cross" is a rule, not code.

Scout only asks Prometheus (`services.prometheus_url`), over its read-only HTTP API. It never scrapes or connects
to the hosts it reports on. And a recommendation is advice: scout proposes it, a standing rule may approve it,
and only a person carries it out.

The commented starter set is [`examples/signals.yaml`](../examples/signals.yaml): disks, load, memory, backups,
temperatures, DNS, network errors, UPS, and the two tariff-aware kinds.

```bash
argus-scout check signals signals.yaml                 # every rule parses, templates are safe
argus-scout analyze --config scout.yaml --dry-run      # one cycle against your Prometheus, nothing written
```

The file is re-read when it changes. A bad rule is skipped and reported, and the rest still load; a file that
won't parse at all skips the cycle and keeps the pending `for` clocks.

## Threshold rules

```yaml
rules:
  - name: disk-filling
    query: >-
      100 * (1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"}
             / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"})
    op: ">"
    threshold: 85
    for: 15m
    severity: warning
    escalate:
      - {threshold: 95, severity: critical}
    labels_key: [instance, mountpoint]
    rejection_ttl: 14d
    recommendation:
      title: "{mountpoint} on {instance} is {value:.0f}% full"
      action: Free space on {mountpoint} (old snapshots, logs, caches) or grow the volume.
      rationale: Used space has been above {threshold:.0f}% for {for}.
      impact: low
      confidence: high
```

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Unique; letters, digits, `.`, `_`, `-`, up to 100. Standing approvals match it as `analyzer`. |
| `kind` | `threshold` | `threshold`, `tariff_shift` or `tariff_plan` (below) |
| `query` | required | A PromQL instant query; one sample per instance you care about |
| `op` | `>` | `>`, `>=`, `<`, `<=`, `==`, `!=` |
| `threshold` | required | A finite number the value is compared with. NaN never crosses. |
| `for` | `0` | How long it must hold before it's raised: `30s`, `15m`, `1h30m`, `2d` |
| `severity` | `warning` | `info`, `notice`, `warning`, `critical` |
| `escalate` | none | Further `{threshold, severity}` steps, each past the last and more severe; needs an ordering `op` |
| `labels_key` | `[instance]` | The labels that make an instance distinct |
| `labels` | none | Extra labels the text may quote, without affecting identity |
| `rejection_ttl` | none | A rejected proposal may be raised again after this long |
| `expires_after` | `7d` | An unanswered proposal is retired this long after the signal was last seen |
| `recommendation` | required | `title`, `action`, `rationale`, `impact` (default `low`), `confidence` (`low`, `medium` default, `high`) |

**`for`.** Each cycle (`schedule.analysis_interval`, 15 minutes by default) runs every query once. A sample that
crosses the threshold is *pending*; once the same instance has crossed it on every cycle for `for`, it's a hit. The
pending start is kept in the store, so a restart doesn't reset the clock. A cycle where the instance doesn't cross
starts it over. Keep `for` a multiple of the interval, or it rounds up to the next cycle.

**Escalation.** The severity of a hit is the most severe step its value has crossed. An open recommendation's
severity goes up with it and never down.

**Identity.** One recommendation per rule and `labels_key` values (`disk-filling{instance="nas",mountpoint="/"}`),
refreshed each cycle while it holds.

## Templates

`title`, `action`, `rationale` and `impact` are templates. Placeholders:

- `{value}`, `{threshold}`, `{op}`, `{rule}`, `{severity}`, `{for}`
- any label in `labels_key` or `labels` (a label missing from a sample renders as `?`)
- the extra ones a tariff kind adds (below)

Numbers take a precision and nothing else: `{value:.1f}`, `{value:.0%}`. The renderer is not Python's
`str.format`: no attribute or index access (`{value.__class__}`, `{labels[0]}`), no `!r` or `!s`, no other format
specs, and no placeholder outside that allowlist. A template that breaks a rule makes its rule invalid when the
file loads, so `check signals` catches it. Label values come from Prometheus, so they can't inject anything either:
they are only ever substituted as text.

## Tariff-aware rules

Two kinds read the tariff file (`files.tariff`; see [`examples/tariff.yaml`](../examples/tariff.yaml)) as well as
Prometheus. They feed the same engine, so dedup, expiry, standing approvals and rejection memory apply unchanged.
Without a tariff, or with one whose periods leave gaps or aren't priced per kWh, they are skipped and reported as
*unknown*, never as clear. Amounts are in the currency's minor unit (cents, pence); `{saving}`, `{plan_cost}` and
`{current_cost}` render in major units (`1.25`).

### `tariff_shift`: move a load to a cheaper period

```yaml
  - name: shift-heavy-loads
    kind: tariff_shift
    query: sum by (load) (load_power_watts{shiftable="true"})
    unit: W               # W | kW: power that keeps running for `duration`; kWh: energy still to use
    min_shiftable: 300    # in `unit`
    duration: 2h          # power units only (default 1h)
    periods: [peak]       # the expensive periods to watch
    horizon: 8h           # a cheaper period must start within this, up to 24h (default 12h)
    min_saving: 20        # optional, minor units
    for: 15m
    severity: notice
    labels_key: [load]
    recommendation:
      title: "Run {load} after {starts_at} ({cheaper_period}) instead of now"
      action: Pause or reschedule {load} until {cheaper_period} starts in {starts_in}.
      rationale: "{energy} kWh at {rate} rather than {cheaper_rate} per kWh saves about {saving} {currency}."
```

While the current period is one of `periods` and a cheaper one starts within `horizon`, each sample at or above
`min_shiftable` is a candidate, saving `energy x (current rate - cheapest rate ahead)`. Outside the watched
periods the rule is evaluated and clear. Extra placeholders: `{period}`, `{rate}`, `{cheaper_period}`,
`{cheaper_rate}`, `{starts_at}`, `{starts_in}`, `{energy}`, `{saving}`, `{currency}`, `{unit}`.

### `tariff_plan`: is another plan cheaper?

```yaml
  - name: cheaper-plan
    kind: tariff_plan
    query: sum(increase(home_energy_kwh_total[1h]))
    unit: kWh             # kWh per step, or W | kW averaged over each step
    window: 30d           # 1d-400d; tier caps apply to its total, so match your billing period
    step: 1h
    every: 1d             # at least, and by default, daily
    min_saving: 500       # minor units over the window
    min_coverage: 0.9     # share of the window that must have data
    severity: info
    recommendation:
      title: "{plan} would have cost {saving} {currency} less than {current_plan} over {window}"
      action: Check {plan}'s current terms with your provider before deciding whether to switch.
```

The query runs as a range query over `window`; each step is priced under the active plan and every
`comparison_plans` entry in the tariff file. If the cheapest isn't the active plan and saves at least
`min_saving`, it's recommended, one recommendation per suggested plan, refreshed by each run. The last run time is
kept in the store, so a restart doesn't re-run it. Too little data (`min_coverage`) makes the run unknown. Extra
placeholders: `{plan}`, `{current_plan}`, `{plan_cost}`, `{current_cost}`, `{saving}`, `{usage}`, `{window}`,
`{days}`, `{currency}`.

## How the engine decides

Each analysis cycle hands the rules' hits to the recommendation engine. For each hit, keyed by rule and
`labels_key` values:

- **Dedup.** An open recommendation (draft, proposed or approved) with the same key is refreshed: its evidence,
  last-seen time and expiry move on, its severity goes up if the hit's did, and a proposal's text is re-rendered.
  An approved one keeps the wording that was approved; if a person approved it, a severity rise is noted in its
  evidence. Nothing is ever duplicated.
- **Rejection memory.** If a person rejected this key before, the hit is suppressed, unless it is now more severe
  than what was rejected (an escalation is new news), or the rule's `rejection_ttl` has passed since the
  rejection. Without a `rejection_ttl`, a rejection holds until the signal escalates. Rejections and their reasons
  also go into every briefing (marked "do not propose again") and, with `files.memory` set, into that directory's
  `scout-recent-rejections.md`, so the model doesn't argue with them.
- **New.** Otherwise a recommendation is created as `proposed`, and standing approvals are applied right then.
- **Expiry.** A proposal is retired, with the reason in its audit trail, when its rule was evaluated and the
  instance has been absent for more than an hour, or when it's past its expiry (`expires_after`, default 7 days
  after the signal was last seen). Approved recommendations are left alone.
- **Unknown is not clear.** If Prometheus can't answer a rule's query, the rule is *unknown* for that cycle: its
  pending clocks are kept, it yields nothing, and the engine doesn't read its silence as the condition clearing.
  Only expiry can retire its proposals, so a long outage can't keep stale ones open forever. `analyze` exits 1 when
  any rule was unknown.

### Standing approvals

A standing approval (`files.approvals`; see [`examples/approvals.yaml`](../examples/approvals.yaml)) moves a
matching proposal to `approved` and records the rule's id. It matches on `analyzer` (the signal rule's `name`, or
`*`), optional `title_pattern` and `action_pattern` regexes, `max_impact` (`low` default, or `medium`) and
`min_confidence` (`medium` default). `scope: until` with `expires_at` makes a rule temporary.

Two guardrails no rule can override, both enforced by tests:

- **Never past `approved`.** An approval is a decision, not an action. Nothing in scout deploys, dispatches or
  executes a recommendation, and there is no route that marks one deployed. Carrying it out is a person's job.
- **Never high-impact or critical.** A proposal whose `impact` reads as high (`high`, `critical`, `major`,
  `significant`, `severe`), or that was raised from a critical signal, is never auto-approved, whatever the rules
  say. It waits for a person. That holds after approval too: when a standing-approved recommendation escalates to
  where a guardrail applies, the engine retires it ("escalated past standing approval: critical signal") and
  raises a fresh `proposed` one for the same key at the new severity, which no rule can approve.

Rules come from two places, both listed by `GET /api/approvals/rules` with their `origin`:

- **The file** (`origin: file`): edit `files.approvals`; it is reloaded when it changes. The API can't delete these.
- **Runtime** (`origin: api`): an approve with `standing` ("approve, and keep approving things like this, for this
  analyzer, until a date") creates one from the recommendation it approves, kept in the store (so it survives a
  restart) with `created_by`. `DELETE /api/approvals/rules/{rule_id}` removes one. The guardrails apply when it is
  created too: asking for a standing rule from a high-impact or critical proposal is a 422 and approves nothing, so
  the person isn't left with half of what they asked for. See [api.md](api.md#standing-rules).

People approve and reject through the API (`POST /api/recommendations/{id}/approve` or `/reject`, with `by` and
an optional `reason`); see [api.md](api.md). The lifecycle is draft, then proposed, then approved, rejected or
retired, with every transition in the audit trail (`GET /api/recommendations/{id}`). `deployed` exists in the
model for a person to record later; nothing in scout sets it.
