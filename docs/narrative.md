# Narrative: briefings and status updates

Scout writes three kinds of report from its own state, with a language model when one is configured and from a
plain template when it isn't (or can't be reached). A report describes and recommends. The model's text is stored
and shown, never parsed: a report that says "approve X" approves nothing.

| Kind | Written | Covers |
|---|---|---|
| `morning` | at each `schedule.briefings` slot of that kind | open items, recommendations awaiting a decision and approved ones, rejections, the tariff period ahead, your notes. Under 150 words. |
| `evening` | the same | what's still open, what was approved, what to watch overnight. Under 150 words. |
| `status` | by the status cycle, when open items pass the heartbeat policy's report rules and at least one is news: not yet announced in its current form (new, escalated or changed, or cleared and back), or its re-announce interval passed; or at a briefing slot | only the reportable items, most severe first, and rejections. Under 80 words. |

Rest hours (heartbeat policy) skip the status cycle entirely; briefings still run. Write one now, from the CLI or
over HTTP (`POST /api/reports`, which also delivers it unless told not to; see
[api.md](api.md#on-request-reports)):

```bash
argus-scout brief --config scout.yaml --kind morning            # asks the model, stores and prints it
argus-scout brief --config scout.yaml --kind status --dry-run   # stores nothing (the model is still asked)
argus-scout brief --config scout.yaml --no-llm                  # the template only
argus-scout deliver --config scout.yaml --latest                # send the newest report to the channels
```

```bash
curl -s -X POST localhost:8400/api/reports -H "Authorization: Bearer $SCOUT_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"kind": "morning"}'     # store, deliver and return it
```

A scheduled briefing and an on-request one are written by the same code (`Briefings` in `cycles.py`), so they get
the same facts, persona, caps, GPU lease and fallback, and `meta.trigger` says which it was (`scheduled`, `request`
or `cli`). Only one report of a kind is written at a time: a request that arrives while one is being written gets a
409, and a scheduled job that comes due waits its turn. Both are held to the daily caps, even though other work
done for an HTTP request may go over them.

## What the model sees

Only facts from scout's store reach the model: open inbox items (grouped), recommendations awaiting a decision and
approved ones, recent rejections with the reasons people gave, the tariff period and its next change, and your
notes. Nothing else, so nothing else can be claimed. Each fact line is clipped to 300 characters.

### Time and greeting

The facts open with the local time, its timezone (`schedule.timezone`) and which report this is:

```text
Saturday 21 November 2026, 22:56 local time (Europe/London, GMT). This is the morning briefing.
```

**A report greets for its kind, never for the clock.** A `morning` briefing is the morning briefing whatever time
it runs, so one written by hand at 22:56 says "Good morning" or doesn't greet, never "Evening". The instructions
say so per kind: `morning` may say "Good morning", `evening` "Good evening", and a `status` update has no
greeting. Scout's rules also tell the model never to guess the time of day.

### Severity first, no all-clear

When the facts hold a warning or critical item (an open item, or for a briefing a recommendation), scout's rules
and the instructions say: lead with the most severe item, and never call things nominal, fine, normal, calm or
quiet, or say there are no crises or nothing to report. "The rest is fine" or "nothing else is urgent" is fine
once every warning and critical item has been named. The answer check enforces the worst of it
([`contradicts_severity`](#a-model-answer-is-checked-before-its-used)).

The prompt is sized for a small local model. The room for facts is `context_tokens` minus `max_tokens`, the system
prompt and the instructions (estimated at four characters a token, with a 10% margin). Lines go in by priority
(critical items, warnings, rejections, notices, info, notes) until the next doesn't fit, and the prompt says how
many were left out per section, so a cut always drops the least severe facts.

Every stored report's `meta` records how it was written: `path` (`llm` or `template`), `fallback_reason`, the
model, the persona, token counts, and the ids of the facts it used and how many it left out. See it with
`GET /api/reports/latest` or `GET /api/reports/{id}`.

## Backends

```yaml
services:
  llm:
    backend: ollama                  # Ollama's /api/chat
    base_url: http://ollama:11434
    model: qwen3:8b
    context_tokens: 8192             # the model's window (default 3072)
    # max_tokens: 400                # one output allowance for every kind (default: per kind, below)
    think: false                     # ollama: no <think> block first (the default)
    timeout: 120
```

- **`ollama`** posts to `<base_url>/api/chat`.
- **`openai`** posts to `<base_url>/chat/completions` on any OpenAI-compatible server: vLLM, llama.cpp, LM Studio,
  or a hosted provider. Include the API prefix in `base_url` (`http://llm:8000/v1`). For a hosted one, put the
  key in `.env` and name it with `api_key_env`.

Both go through argus-cortex's remote backends. A request that never reached the server is retried; a slow model
is never asked twice. Every key: [config reference](config.md#servicesllm).

### Output allowance

`max_tokens` is how many tokens the model may write for one report. Left unset, it depends on the kind: 800 for
`morning` and `evening`, 400 for `status`, each capped at half of `context_tokens`. A 150-word briefing is about
200 tokens, so the rest is headroom for a model that writes long or thinks anyway. Set it and it applies to every
kind; it may be at most half of `context_tokens`, because the prompt gets the rest (`context_tokens - max_tokens`,
above). Each report's `meta.max_tokens` records the allowance it ran with.

### Reasoning models

Reasoning models (qwen3, deepseek-r1 and the like) write a `<think>` block before the answer, and it counts
against `max_tokens`. Scout strips the block, so a model that spends its budget thinking leaves almost nothing.

- **Ollama.** Scout sends `think: false` (`services.llm.think`, the default), which Ollama 0.9 and later honour
  for thinking models: the answer comes back without a `<think>` block. Older Ollamas ignore the field. Set
  `think: null` to send nothing and leave the model's default, or `true` to let it think (and raise `max_tokens`
  to match). If a server answers 400 naming `think`, scout logs `llm.think_unsupported`, asks once more without
  the field and stops sending it until it restarts.
- **OpenAI-compatible.** Servers disagree on the switch, so scout sends none. Pass the one your server takes with
  `extra_body`, which is merged into the request as it is (it can't replace `model`, `messages`, `max_tokens`,
  `stream` or `temperature`):

```yaml
services:
  llm:
    backend: openai
    base_url: http://llm:8000/v1
    model: <MODEL>
    extra_body:
      chat_template_kwargs: {enable_thinking: false}   # vLLM / SGLang with a qwen3 template
      # reasoning_effort: low                          # servers and providers that take this instead
```

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
| `truncated` | the model hit its token limit (Ollama `done_reason`, OpenAI `finish_reason`: `length`), so its answer was cut off |
| `hollow` | the answer was too short for its kind, or left out an item that needs attention (below) |
| `contradicted` | the answer called things all clear in a report that is `warning` or above (below) |

The model's output is cleaned before it's stored: control characters and `<think>` blocks are stripped and its
length capped.

### A model answer is checked before it's used

An answer that came back is still not delivered unless it is whole. Scout falls back to the template when:

- **it was cut off** (`truncated`): the server said it stopped at the token limit. Whatever came after the cut,
  an item that needs acknowledgement included, is missing.
- **it is near-empty** (`hollow`, rule `too_short`): under 60 characters for a `morning` or `evening` report, or
  40 for `status`. This is only a backstop for output that is next to nothing; whether an answer covers what
  matters is the next rule's job, which checks content directly. So a terse answer that names every item needing
  attention passes, however short: "Good morning. Three critical CPU temperatures: host-a at 91°C, host-b at 88°C,
  host-c at 87°C. All exceed 85°C. Check cooling on all three. No other issues." (156 characters) is whole, while
  "All quiet." is not. When there's nothing to report, one short sentence (20 characters) is enough. An answer
  that was all `<think>` block is empty after cleaning, so it lands here.
- **it leaves out something that needs attention** (`hollow`, rule `missing_items`): it must name
  - every open item that is a warning or critical, or needs an acknowledgement, and
  - in a `morning` or `evening` briefing, every open recommendation (proposed or approved) at warning or above. A
    `status` update lists only open items and rejections, so only its items are required.

  Only what the prompt actually held is required: a fact the budget cut (above) was never shown to the model, so
  it can't be held to it. At most 12 things are required, most severe first. This is a floor, not a judgement of
  quality: a model that paraphrases every word away gets the template, which is the safe side. How an item counts
  as named:
  - The answer and the item are split into words on anything that isn't a letter or digit, so source
    `ai-code-auditor` is `ai`, `code`, `auditor`, and `decision(s)` is `decision`.
  - Spelled-out numbers become digits first (0 to 999: "twenty-seven" is 27, "one hundred and five" is 105), since
    the prompt asks the model to write numbers for speech.
  - Words under three characters (`ai`, `pr`, a single digit like `0` or `4`) and a list of stopwords (generic
    words like "warning", "status", "system", "server", "home") are dropped. A number of two digits or more is kept.
  - Words match whole, through a crude stem: "decisions" matches "decision", "reboots" "reboot", "batteries"
    "battery", but "restarting" is not "rest" and "backups" is not "ups".
  - An item's **strong** words come from its source and from the host and label values in its payload (`host`,
    `hostname`, `instance`, `node`, `device`, `mountpoint`, `service`, `job`, `container`, and every value of
    `labels`). One strong word names the item. Its **weak** words come from its summary, and it takes two distinct
    ones (one, if the summary has only one).
  - A recommendation's **strong** words are the label values in its key (its rule's `labels_key`, such as the
    `instance` or host), and its **weak** words come from its title. A recommendation for `instance="orion"` titled
    "CPU on orion at 91°C" is named by "orion", or by "CPU" and "91".

  For an item from source `ai-code-auditor` with the summary "Rest completed: 27 confirmed, 0 draft PRs, 4
  decision(s)", "The code auditor finished its rest run with twenty-seven confirmed and four decisions to
  acknowledge" names it (`code`, `auditor`); "Twenty-seven findings were confirmed" names it (27 and `confirm`);
  "Systems are fine; restarting nothing" doesn't, and neither does "Get some rest tonight" (one weak word).
- **it calls things all clear while warnings or criticals are open** (`contradicted`, rule
  `contradicts_severity`): the report's `severity` (its worst open item, and for a briefing its worst
  recommendation too) is `warning` or `critical`, and the answer contains one of these phrases, matched
  case-insensitively and as whole words (any run of spaces or newlines counts as one space):

  | Phrase | Also matches |
  |---|---|
  | all systems nominal | "all systems are nominal", "... normal", "... green", "... go" |
  | all clear | |
  | all quiet | |
  | nothing to report | |
  | nothing exploding | "nothing is exploding" |
  | nothing needs attention | "nothing needs your attention" |
  | no crises | "no crisis", "no immediate / urgent / major / real crises" |
  | everything is fine | "everything's fine"; "... ok", "okay", "good", "normal", "nominal", "calm", "quiet" |

  The list is kept tight, to absolute claims, so it doesn't refuse good answers: "nothing else is urgent", "the
  rest is fine" and "otherwise quiet" pass, as do "all clearances" and "all quietly" (whole words only). A listed
  phrase is refused wherever it appears, even after the critical items ("host-a is critical; otherwise all
  clear"): the template is the safe side. Below `warning` the phrases are allowed. A real answer this catches, a
  morning briefing written at 22:56 with three critical recommendations open: "Evening. All systems nominal,
  nothing exploding. For once. host-a's CPU is at 91°C ... No immediate crises, but keep an eye on host-a."

The template lists the items and recommendations from the same facts (ten a section, but every warning, critical
and item awaiting acknowledgement, and every warning or critical recommendation, however many there are), so a
model that fails never costs the report one of those. The stored `meta` still records what the model did:
`completion_tokens` (the raw count, thinking included), `finish_reason`, `llm_chars` (what was left after
cleaning), and `error` naming the check that failed. Scout logs `narrative.rejected_completion` with the same.

**A refused answer is kept for audit, and never delivered.** So a verdict can be checked after the fact, the
report's `meta` also holds:

- `rejected_text`: the model's answer, cleaned (newlines normalised, control and format characters such as
  terminal escapes and bidi overrides stripped) and clipped to 2000 characters;
- `rejected_check`: `{reason, rule, detail, missing}`, where `reason` is `truncated`, `hollow` or `contradicted`,
  `rule` is the check that failed (`token_limit`, `too_short`, `missing_items` or `contradicts_severity`, whose
  `detail` quotes the phrases found) and `missing` the ids of the items the answer didn't name.

Channels read the report's text, which is the template's. `rejected_text` is also dropped from the `meta` every
delivery carries (webhook payloads included): it is unvetted text that the check refused, possibly dropping or
misstating what needs attention, so it never leaves scout. Read it from the stored report: `GET
/api/reports/{id}` (behind `server.read_auth` when that is set), or `/api/reports`. `rejected_check`
holds no model text and does go out with deliveries.

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

A persona (`files.persona`) sets how reports sound, never what scout may do or what is true. Scout's own rules come
first in every system prompt and a persona can't remove them: advisory only, use only the facts given, don't
propose rejected items again or argue with the reason, respect the operator's notes, lead with the most severe
item and never call things nominal while warnings or criticals are open, greet for the report kind and never guess
the time of day. The last rule says they win wherever the persona's voice, style, catchphrases or sign-off
conflict with them.

That isn't left to the model alone. When a report is `warning` or above, any persona line that carries an
all-clear phrase (the [list above](#a-model-answer-is-checked-before-its-used)) is withheld from that report: a
`style` or `do` entry is dropped, a `voice` falls back to the default one, and a `sign_off` is left off, from the
template as well as the prompt. A catchphrase like "All systems nominal, nothing exploding. For once." still signs
off a quiet briefing, but never one with a critical item. The report's `meta.persona_withheld` lists what was held
back. Whatever a persona says, an answer that calls things all clear is then refused by the check.

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

**What counts as a note.** Every `*.md` file in the directory, except:

- `README.md`, in any case (`readme.md`, `Readme.MD`): it is for people browsing the directory, not for the model.
- Any name starting with `_` (`_draft.md`, `_template.md`): set a note aside by renaming it, without deleting it.
- `scout-recent-rejections.md`, scout's own file (below).

`argus-scout check config` reports how many notes it found.

Scout writes one file there, `scout-recent-rejections.md`, rewritten whenever it writes a report: the recent
rejections and the reasons given, so you can see what it has been told not to raise again. Every path stays under
the directory: absolute names, `..` and symlinks out are refused, and scout's own file is never written through a
symlink.

**A read-only directory.** Set `memory.write_rejections: false` and scout writes nothing there. Reports still quote
the recent rejections and their reasons: those come from the store, not from this file. If the directory can't be
written anyway (checked at startup, or the first write is refused), scout logs `memory.not_writable` once and stops
trying until it restarts.
