# argus-scout demo

Three scenarios in about five minutes, shown side by side: Grafana with what Prometheus sees and what scout
concluded, scout's reports in a terminal and, with voice on, the same reports spoken by an argus-mask face.
The stack runs on one machine on loopback ports. Reports are written by Claude through the Messages API, and
speech goes through the argus-echo and argus-mask services you already run.

The demo sits on top of the quickstart in `deploy/quickstart` as two compose overlays:

| File | Adds |
|---|---|
| `compose.demo.yaml` | Grafana with a provisioned dashboard, a simulated house (`demo/simulator`), and scout reading `demo/config` with fast cycles, writes behind a generated token, and Claude as its model. The quickstart's Ollama stays off |
| `compose.voice.yaml` | No containers: scout speaks through your argus-echo XTTS engine (a built-in speaker, not a clone) and your argus-mask face, at `ARGUS_XTTS_URL` and `ARGUS_MASK_URL` |

## Run it

```bash
cd deploy/quickstart
echo 'ANTHROPIC_API_KEY=...' >> demo/config/.env  # gitignored; demo.sh adds the API token beside it
chmod 600 demo/config/.env
./demo/demo.sh up
./demo/demo.sh run

# With speech, through services you already run:
export ARGUS_XTTS_URL=http://<ECHO_HOST>:8141    # an argus-echo XTTS engine
export ARGUS_MASK_URL=http://<MASK_HOST>:8109    # an argus-mask server
./demo/demo.sh up --voice
```

The model is `claude-opus-5-5` at `effort: low`; change `services.llm.model` in `demo/config/scout.yaml` (and
`scout.voice.yaml`) to `claude-sonnet-5-5` for a cheaper run. A full run makes about four model calls, and the
daily cap is 40. Without a key every report comes from the template, which says so.

Then open:

- Grafana: `http://127.0.0.1:3000/d/scout-demo` (anonymous viewer)
- Scout's reports: `http://127.0.0.1:8400/wall`, or `tail -f demo/config/scout-reports.log`
- With voice, the face: `<ARGUS_MASK_URL>/?face=stock-01` on any screen. Click once to allow sound. If no screen
  is watching the face, mask answers "not ready" and scout skips it. The text log still gets every report. For a
  recording you publish, keep to a cleared face (stock or synthetic), never a personal one.

If a port is taken, move it: `SCOUT_PUBLISH`, `PROMETHEUS_PUBLISH`, `GRAFANA_PUBLISH`, `SIMULATOR_PUBLISH`,
(for example `SCOUT_PUBLISH=127.0.0.1:18400`), and point the script at it with `SCOUT_URL` and
`SIMULATOR_URL`.

`./demo/demo.sh reset` returns to the normal scenario. `./demo/demo.sh down` deletes the volumes and the demo's
runtime files.

## The scenarios

**1. Tariff-aware workload shifting.** A 1.85 kW batch render starts during the tariff's peak. The simulator
writes the tariff with its peak centred on that moment, so the demo works at any hour. Scout's `tariff_shift`
rule reads the power from Prometheus and the rates from the tariff file, and proposes running the job
off-peak. The proposal is low impact and recurs, so the standing rule `demo-tariff-shift` approves it.
Approved only: scout never carries out a recommendation.

**2. A standing approval meets a guardrail.** The GPU runs hot (84°C), which is a warning, and the standing
rule `demo-gpu-thermal` approves it. At 93°C the signal escalates to critical. No standing approval covers a
critical signal, so scout withdraws the approval and raises a fresh proposal for a person. The script then
approves it as that person, at the revision they read.

**3. Blind is not clear.** Prometheus stops. Grafana reads the same Prometheus, so its panels go blank and
scout's own `/metrics` is the witness. The script prints `scout_signal_rule_up` at 0 for every rule: unknown,
never clear. Scout retires nothing as cleared while it can't see. It then asks for a report without the model,
which comes from the deterministic template with the reason recorded, as it would if the API were unreachable. The state timeline turns grey whenever Prometheus is up but
a rule still can't be evaluated, such as a tariff rule with no usable tariff.

The timeline annotations show each scenario change, each change in a recommendation's status, each standing
approval and each template fallback.

## What the dashboard reads

| Panel | Series | From |
|---|---|---|
| Power by load | `load_power_watts{load,shiftable}` | the simulator, as a smart plug would publish it |
| GPU temperature and utilisation | `DCGM_FI_DEV_GPU_TEMP`, `DCGM_FI_DEV_GPU_UTIL` | the simulator, under dcgm-exporter's names |
| Tariff rate | `scout_tariff_rate{period}` | scout, from the tariff file |
| Signal rules | `scout_signal_rule_up{rule}`, `scout_signal_rule_hits{rule}` | scout's last analysis cycle |
| Recommendations, approvals | `scout_recommendations{status}`, `scout_approvals_total{source}` | scout's store |
| Reports, deliveries | `scout_reports_total{kind,path,fallback_reason}`, `scout_deliveries_total{channel,result}` | scout's store |
| Model calls, jobs | `scout_usage_used`, `scout_usage_limit`, `scout_job_*` | scout |

`tests/test_demo.py` fails if the dashboard or the demo's rules query a series that nothing publishes.

## Your own data instead of the simulator

The rules use real exporters' metric names, so pointing them at real data means running those exporters
where the hardware is and having your Prometheus scrape them:

| Signal | Exporter | Where it runs |
|---|---|---|
| GPU temperature and utilisation | NVIDIA's `dcgm-exporter` | each GPU host |
| CPU, load, memory, disks | `node-exporter` | each host |
| Power per load | a smart plug or power-meter exporter, relabelled to `load_power_watts{load,shiftable}` | wherever the plugs report |
| House energy | the meter's exporter, as a `home_energy_kwh_total` counter | wherever the meter reports |

Scout itself stays where it is. It only queries Prometheus.

## Licences

XTTS-v2's weights, used by argus-echo's XTTS engine, are under the Coqui Public Model Licence (non-commercial use
only). Mask's `stock-01` head is CC0.
