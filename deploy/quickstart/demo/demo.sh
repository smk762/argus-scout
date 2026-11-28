#!/usr/bin/env bash
# The argus-scout demo, scripted so a recording can be repeated. Run from anywhere; it works in deploy/quickstart.
#
#   demo/demo.sh up [--voice]     start the stack (generates demo/config/.env with a random API token first)
#   demo/demo.sh run              the three scenarios, each ending in a spoken/written report
#   demo/demo.sh scenario <name>  switch the simulator: normal | peak-batch | gpu-hot | gpu-critical
#   demo/demo.sh report           ask scout for a report now (REPORT_KIND, default evening) and deliver it
#   demo/demo.sh reset            normal scenario, stopped containers started again
#   demo/demo.sh down             stop the stack and delete its volumes and the demo's runtime files
#
# Needs docker (or podman) compose, curl and python3 on the host. Scout stays advisory throughout: the script
# approves one recommendation as a person would, through the API with the token; scout itself never acts.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUICKSTART="$(dirname "$HERE")"
cd "$QUICKSTART"

SCOUT="${SCOUT_URL:-http://127.0.0.1:8400}"
SIM="${SIMULATOR_URL:-http://127.0.0.1:9101}"
ENV_FILE="$HERE/config/.env"
VOICE_MARK="$HERE/config/.voice"
COMPOSE="${COMPOSE:-docker compose}"
# A briefing kind: unlike `status` (inbox items only), it carries the recommendations and the tariff.
REPORT_KIND="${REPORT_KIND:-evening}"

files() {
  local f=(-f compose.yaml -f compose.demo.yaml)
  [[ -f "$VOICE_MARK" ]] && f+=(-f compose.voice.yaml)
  echo "${f[@]}"
}
dc() { # shellcheck disable=SC2046
  $COMPOSE $(files) "$@"
}
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
note() { printf '   %s\n' "$*"; }

token() { sed -n 's/^SCOUT_API_TOKEN=//p' "$ENV_FILE"; }

api() { # api METHOD PATH [JSON]
  local args=(-fsS -X "$1" "$SCOUT$2" -H "Authorization: Bearer $(token)")
  [[ $# -ge 3 ]] && args+=(-H 'Content-Type: application/json' --data "$3")
  curl "${args[@]}"
}

# Recommendations matching an analyzer (and optionally status/severity), as "id revision status severity title".
recs() { # recs ANALYZER [STATUS] [SEVERITY]
  api GET "/api/recommendations?limit=200" | python3 -c '
import json, sys
analyzer, status, severity = (sys.argv[1:] + ["", ""])[:3]
for r in json.load(sys.stdin)["recommendations"]:
    if r["analyzer"] == analyzer and (not status or r["status"] == status) and (not severity or r["severity"] == severity):
        print(r["id"], r["revision"], r["status"], r["severity"], r["title"])' "$@"
}

wait_for() { # wait_for SECONDS DESCRIPTION COMMAND...
  local deadline=$((SECONDS + $1)) what="$2"
  shift 2
  printf '   waiting for %s ' "$what"
  until out="$("$@" 2>/dev/null)" && [[ -n "$out" ]]; do
    if ((SECONDS > deadline)); then
      printf '\n   gave up after a while; check: docker compose logs scout\n'
      return 1
    fi
    printf '.'
    sleep 5
  done
  printf ' ok\n'
  printf '%s\n' "$out" | sed 's/^[^ ]* [^ ]* /   /'
}

scenario() {
  curl -fsS -X POST "$SIM/scenario/$1" >/dev/null
  note "simulator: $1"
}

report() {
  note "asking scout for a report (the model if it can, the template if it can't) ..."
  local no_llm=false
  [[ -n "${NO_LLM:-}" ]] && no_llm=true
  api POST /api/reports "{\"kind\": \"$REPORT_KIND\", \"deliver\": true, \"no_llm\": $no_llm}" | python3 -c '
import json, sys
r = json.load(sys.stdin)
report = r.get("report", r)
meta = report.get("meta", {})
reason = meta.get("fallback_reason")
print("   written by: " + str(meta.get("path")) + (" (" + reason + ")" if reason else ""))
for d in (r.get("delivery") or {}).get("results", []):
    why = d.get("detail") or d.get("error")
    print("   delivered to " + d["channel"] + ": " + d["status"] + (" (" + why + ")" if why else ""))
print()
for line in report.get("text", "").splitlines():
    print("   | " + line)'
}

ensure_env() {
  [[ -f "$ENV_FILE" ]] && grep -q '^SCOUT_API_TOKEN=.' "$ENV_FILE" && return
  umask 077
  printf 'SCOUT_API_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >>"$ENV_FILE"
  note "wrote a random API token to demo/config/.env"
}

cmd_up() {
  ensure_env
  if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    note "ANTHROPIC_API_KEY is not set: every report will come from the template (export it and run up again)"
  fi
  if [[ "${1:-}" == "--voice" ]]; then
    if [[ -z "${ARGUS_XTTS_URL:-}" || -z "${ARGUS_MASK_URL:-}" ]]; then
      echo "--voice needs ARGUS_XTTS_URL (an argus-echo XTTS engine) and ARGUS_MASK_URL (an argus-mask server)" >&2
      exit 2
    fi
    touch "$VOICE_MARK"
  else
    rm -f "$VOICE_MARK"
  fi
  dc up -d --build
  say "Up"
  note "Grafana:   http://${GRAFANA_PUBLISH:-127.0.0.1:3000}/d/scout-demo"
  note "Scout:     $SCOUT/wall"
  if [[ -f "$VOICE_MARK" ]]; then
    note "The face:  ${ARGUS_MASK_URL%/}/?face=stock-01   (click once to allow sound)"
  fi
  note "Then:      demo/demo.sh run"
}

cmd_run() {
  say "1. Tariff-aware workload shifting"
  note "A 1.85 kW batch render starts during the tariff's peak. Scout's tariff_shift rule reads the power from"
  note "Prometheus and the rates from the tariff file, and proposes running it off-peak. The proposal is low"
  note "impact and recurring, so the standing rule demo-tariff-shift approves it. Approved only: nothing moves."
  scenario peak-batch
  wait_for 240 "the shift recommendation" recs shift-heavy-loads approved
  report

  say "2. Governance: a standing approval meets a guardrail"
  note "The GPU runs hot (84°C): a warning, and the standing rule demo-gpu-thermal approves it."
  scenario gpu-hot
  wait_for 240 "gpu-hot approved by its standing rule" recs gpu-hot approved warning
  note "It keeps heating (93°C): critical. A standing approval never covers a critical signal, so scout"
  note "withdraws it and raises a fresh proposal for a person."
  scenario gpu-critical
  wait_for 240 "a critical gpu-hot proposal" recs gpu-hot proposed critical
  report
  note "A person approves it, at the revision they read (a newer one would be refused with a 409):"
  read -r id revision _ < <(recs gpu-hot proposed critical | head -1)
  api POST "/api/recommendations/$id/approve" \
    "{\"by\": \"demo-operator\", \"reason\": \"checked the fans\", \"revision\": $revision}" >/dev/null
  recs gpu-hot approved critical | sed 's/^[^ ]* [^ ]* /   /'
  scenario normal

  say "3. Failure: blind is not the same as clear"
  note "Prometheus stops. Grafana reads the same Prometheus, so its panels go blank: the witness is scout's"
  note "own /metrics. Every rule is unknown (0), never clear, and scout retires nothing while it can't see."
  dc stop prometheus >/dev/null
  sleep 45
  note "scout's /metrics, Prometheus down (1 answered, 0 unknown):"
  curl -fsS "$SCOUT/metrics" | grep '^scout_signal_rule_up' | sed 's/^/     /'
  note "open recommendations (none retired as cleared):"
  api GET "/api/recommendations?limit=200" | python3 -c '
import json, sys
for r in json.load(sys.stdin)["recommendations"]:
    if r["status"] in ("proposed", "approved"):
        print("     [" + r["severity"] + "] " + r["title"] + " (" + r["status"] + ")")'
  note "The model can't be reached either? The report comes from the deterministic template, with the"
  note "reason recorded. Here, without the model on purpose:"
  NO_LLM=1 report
  dc start prometheus >/dev/null
  note "Prometheus is back. The gap in Grafana marks the outage, and the template-fallback annotation marks"
  note "the report scout wrote without its model."
  say "Done. demo/demo.sh reset to go again."
}

cmd_reset() {
  scenario normal
  dc start prometheus node-exporter >/dev/null
}

cmd_down() {
  dc down -v
  rm -f "$VOICE_MARK" "$HERE"/config/scout.db* "$HERE"/config/scout-reports.log* "$HERE"/config/tariff.yaml
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  run) cmd_run ;;
  scenario) scenario "${2:?scenario name: normal | peak-batch | gpu-hot | gpu-critical}" ;;
  report) report ;;
  reset) cmd_reset ;;
  down) cmd_down ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
