# Narrative: briefings and status updates

Scout writes three kinds of report from its own state, with a language model when one is configured and from a
plain template when it isn't (or can't be reached). A report describes and recommends. The model's text is stored
and shown, never parsed: a report that says "approve X" approves nothing.

| Kind | Written | Covers |
|---|---|---|
| `morning` | at each `schedule.briefings` slot of that kind | open items, recommendations awaiting a decision and approved ones, rejections, the tariff period ahead, your notes. Under 150 words. |
| `evening` | the same | what's still open, what was approved, what to watch overnight. Under 150 words. |
| `status` | by the status cycle, when open items pass the heartbeat policy's report rules and at least one is news: not yet announced in its current form (new, escalated or changed, or cleared and back), or its re-announce interval passed; or at a briefing slot | only the reportable items, most severe first, and rejections. Under 80 words. |

Rest hours (heartbeat policy) skip the status cycle entirely; briefings still run. Write one now:

```bash
argus-scout brief --config scout.yaml --kind morning            # asks the model, stores and prints it
argus-scout brief --config scout.yaml --kind status --dry-run   # stores nothing (the model is still asked)
argus-scout brief --config scout.yaml --no-llm                  # the template only
argus-scout deliver --config scout.yaml --latest                # send the newest report to the channels
```

## What the model sees

Only facts from scout's store reach the model: open inbox items (grouped), recommendations awaiting a decision and
approved ones, recent rejections with the reasons people gave, the tariff period and its next change, and your
notes. Nothing else, so nothing else can be claimed. Each fact line is clipped to 300 characters.

The prompt is sized for a small local model. The room for facts is `context_tokens` minus `max_tokens`, the system
prompt and the instructions (estimated at four characters a token, with a 10% margin). Lines go in by priority
(critical items, warnings, rejections, notices, info, notes) until the next doesn't fit, and the prompt says how
many were left out per section, so a cut always drops the least severe facts.

Every stored report's `meta` records how it was written: `path` (`llm` or `template`), `fallback_reason`, the
model, the persona, token counts, and the ids of the facts it used and how many it left out. See it with
`GET /api/reports/latest`.

## Backends

```yaml
services:
  llm:
    backend: ollama                  # Ollama's /api/chat
    base_url: http://ollama:11434
    model: qwen3:8b
    context_tokens: 8192             # the model's window (default 3072)
    max_tokens: 400                  # the longest report (default 400)
    timeout: 120
```

- **`ollama`** posts to `<base_url>/api/chat`.
- **`openai`** posts to `<base_url>/chat/completions` on any OpenAI-compatible server: vLLM, llama.cpp, LM Studio,
  or a hosted provider. Include the API prefix in `base_url` (`http://llm:8000/v1`). For a hosted one, put the
  key in `.env` and name it with `api_key_env`.

Both go through argus-cortex's remote backends. A request that never reached the server is retried; a slow model
is never asked twice. Every key: [config reference](config.md#servicesllm).

## Caps and fallback

Each model call is charged to the `llm_calls` daily cap (`budget.limits.llm_calls`) **before** the model is asked,
so a failing backend still counts and a loop can't run up an unbounded bill. Counts live in the store and survive
restarts; see [operations.md](operations.md#usage-caps).

A report is never skipped. When the model can't write it, the template does, from the same facts, and says why at
the bottom. `meta.fallback_reason` is one of:

| Reason | Meaning |
|---|---|
| `no_llm` | `services.llm` isn't set |
| `disabled` | `brief --no-llm` |
| `budget` | today's `llm_calls` cap is spent (the model wasn't called) |
| `gpu_unavailable` | no GPU lease was granted in time (the model wasn't called or charged) |
| `error` | the model was unreachable, failed, timed out or returned nothing |

The model's output is cleaned before it's stored: control characters and `<think>` blocks are stripped and its
length capped.

## GPU leases

When scout's model runs on a shared GPU, give the narrative a lease so the suite's broker
(argus-iris) can queue it behind more urgent work, place it on a node with
room and evict an idle model first:

```yaml
services:
  llm:
    backend: ollama
    base_url: http://ollama:11434
    model: qwen3:8b
    gpu: {}                          # present, even empty: lease around every model call
```

- **The lease** covers exactly one model call's HTTP round-trip, never prompt building or storage. It is kind
  `narrative`, preemptible, at priority `background` by default (a briefing can wait), asking for `min_vram_mb`
  (6000) of which `model_mb` (5200) stays loaded in the model server. The target is `ollama:<model>` for Ollama;
  an OpenAI-compatible server needs `gpu.target` (`vllm:<model>`). Leave `gpu` unset for a hosted backend.
- **The coordinator** is not in `scout.yaml`. It's the host-wide environment every argus service on the host
  shares, read by argus-cortex: `ARGUS_GPU_COORDINATOR` (`none` by default: every lease granted at once; `lease`
  for a host lock; `broker` for argus-iris), `ARGUS_GPU_BROKER_URL`, `ARGUS_GPU_BROKER_TOKEN`,
  `ARGUS_GPU_TENANT_MANIFEST` and `ARGUS_GPU_LEASE_TIMEOUT_S`. A per-service setting would let scout skip the queue
  its siblings wait in.
- **The manifest.** With the broker, scout registers [`gpu-tenant.yaml`](../gpu-tenant.yaml) on its first lease. The
  container image ships it at `/app/gpu-tenant.yaml`; point `ARGUS_GPU_TENANT_MANIFEST` there. Its figures match the
  defaults above, and a test fails if they drift.
- **Placement.** If the broker grants the lease on another node, scout calls the model server there.
- **No lease in time** means the template, with `fallback_reason: gpu_unavailable`, and no charge to the cap.

Speech is not scout's GPU work: argus-echo takes its own leases, and scout never holds one around a synthesis.

## Persona

A persona (`files.persona`) sets how reports sound, never what scout may do. Scout's own rules come first in every
system prompt and a persona can't remove them: advisory only, use only the facts given, don't propose rejected
items again or argue with the reason, respect the operator's notes.

```yaml
name: Scout
voice: Calm, plain and brief, like a colleague handing over a shift.
style: [Lead with what needs attention; say when nothing does.]
do: [Order items by severity, most severe first.]
dont: [Don't speculate beyond the facts.]
sign_off: ""                       # a closing line on every report, model or template
speakable:                         # reports may be read aloud
  max_sentence_words: 25
  spell_out_symbols: true
  no_markdown: true
```

Every key is optional; [`examples/persona.yaml`](../examples/persona.yaml) holds the neutral defaults with comments.
Check one with `argus-scout check persona FILE`. A bad file falls back to the default and is logged, and the
report's `meta.persona_error` says why.

## Memory

`files.memory` names a directory of your own markdown notes (`*.md`, one level): preferences, standing facts,
things to ignore. They're folded into morning and evening briefings, after everything else. Scout reads them and
never edits them; each is read up to 16 KiB, at most 50 files, and about 2000 characters in total reach a prompt.

Scout writes one file there, `scout-recent-rejections.md`, rewritten whenever it writes a report: the recent
rejections and the reasons given, so you can see what it has been told not to raise again. Every path stays under
the directory: absolute names, `..` and symlinks out are refused, and scout's own file is never written through a
symlink.
