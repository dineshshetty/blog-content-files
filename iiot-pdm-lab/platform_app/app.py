"""
North Plant asset health platform — the thing being attacked.

Subscribes to spBv1.0/#, decodes payloads, trends vibration, runs ISO 10816
assessment, opens work orders on alarm. Dashboard at HTTP_PORT.

Trusts everything it receives from the broker. That's the point.
"""

import logging
import os
import threading
import time
from collections import defaultdict, deque

import paho.mqtt.client as mqtt
from flask import Flask, jsonify, render_template_string

from common import sparkplug as spb

logging.basicConfig(level=logging.INFO, format="%(asctime)s [platform] %(message)s")
log = logging.getLogger("platform")

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8000"))

# ISO 10816-style velocity bands (mm/s RMS) for medium machines.
WARN = 4.5
ALARM = 7.1
# Seconds a machine must stay in the alarm band before the alarm latches
# (nuisance-trip suppression, as real CM systems do).
SUSTAIN = float(os.environ.get("ALARM_SUSTAIN", "6"))

app = Flask(__name__)


class AssetStore:
    def __init__(self):
        self.lock = threading.Lock()
        # device_key -> {"alias": {alias:name}, "metrics": {name:value},
        #                "history": {name: deque}, "props": {name:{}},
        #                "health": str, "last_seen": ts, "online": bool}
        self.devices = {}
        self.work_orders = []
        self._wo_seq = 1000

    def _dev(self, key):
        if key not in self.devices:
            self.devices[key] = {
                "alias": {}, "metrics": {}, "history": defaultdict(lambda: deque(maxlen=120)),
                "props": {}, "health": "OK", "last_seen": 0, "online": True,
                "over_since": None,
            }
        return self.devices[key]

    def on_birth(self, key, payload):
        with self.lock:
            d = self._dev(key)
            d["online"] = True
            d["last_seen"] = time.time()
            for m in payload.metrics:
                name = m.name
                if not name:
                    continue
                if m.alias:
                    d["alias"][m.alias] = name
                val = spb.get_metric_value(m)
                d["metrics"][name] = val
                d["history"][name].append((time.time(), val))
                d["props"][name] = {
                    "engUnit": spb.get_property(m, "engUnit"),
                    "writable": spb.get_property(m, "writable"),
                }

    def on_data(self, key, payload):
        with self.lock:
            d = self._dev(key)
            d["online"] = True
            d["last_seen"] = time.time()
            resolved = False
            for m in payload.metrics:
                name = m.name or d["alias"].get(m.alias)
                if not name:
                    continue
                resolved = True
                val = spb.get_metric_value(m)
                d["metrics"][name] = val
                d["history"][name].append((time.time(), val))
            self._assess(key, d)
            # Sparkplug host rule: if we got DATA we can't map (no BIRTH yet),
            # ask the node to re-announce. Return True to trigger that.
            return not resolved and not d["alias"]

    def on_death(self, key):
        with self.lock:
            if key in self.devices:
                self.devices[key]["online"] = False

    def _assess(self, key, d):
        de = d["metrics"].get("Bearing/DE Vibration")
        if de is None:
            return
        now = time.time()
        # Alarm persistence / debounce: a machine must stay in the alarm band
        # for SUSTAIN seconds before we latch an alarm. Real condition-monitoring
        # systems do this to avoid nuisance trips on transient spikes. It also
        # means a single healthy reading resets the timer.
        if de >= ALARM:
            if d["over_since"] is None:
                d["over_since"] = now
            health = "ALARM" if (now - d["over_since"]) >= SUSTAIN else "WARNING"
        elif de >= WARN:
            d["over_since"] = None
            health = "WARNING"
        else:
            d["over_since"] = None
            health = "OK"
        prev = d["health"]
        d["health"] = health
        # Open a work order on the OK/WARNING -> ALARM transition, once.
        if health == "ALARM" and prev != "ALARM":
            self._wo_seq += 1
            self.work_orders.insert(0, {
                "id": self._wo_seq,
                "asset": key.split("/")[-1],
                "text": f"High bearing vibration {de:.1f} mm/s (>{ALARM}) "
                        f"— inspect drive-end bearing",
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            log.warning("WORK ORDER #%s opened for %s (%.1f mm/s)",
                        self._wo_seq, key, de)

    def snapshot(self):
        with self.lock:
            pumps = []
            for key, d in sorted(self.devices.items()):
                hist = [round(v, 2) for (_, v) in d["history"].get("Bearing/DE Vibration", [])]
                pumps.append({
                    "key": key,
                    "name": key.split("/")[-1],
                    "health": d["health"],
                    "online": d["online"],
                    "metrics": d["metrics"],
                    "props": d["props"],
                    "de_history": hist[-60:],
                })
            return {"pumps": pumps, "work_orders": self.work_orders[:10],
                    "warn": WARN, "alarm": ALARM}


store = AssetStore()
_rebirth_asked = {}  # node -> last request time (throttle)


# --- MQTT ingest -----------------------------------------------------------
def on_connect(client, userdata, flags, rc):
    log.info("Ingest connected to broker (rc=%s), subscribing spBv1.0/#", rc)
    client.subscribe("spBv1.0/#")


def request_rebirth(client, group, node):
    now = time.time()
    if now - _rebirth_asked.get(node, 0) < 10:
        return
    _rebirth_asked[node] = now
    p = spb.new_payload(seq=0)
    spb.add_metric(p, name="Node Control/Rebirth",
                   datatype=spb.DataType.Boolean, value=True)
    client.publish(spb.topic(group, "NCMD", node), spb.encode(p), qos=0)
    log.info("Requested rebirth from %s/%s (saw DATA before BIRTH)", group, node)


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) < 4:
        return
    _, group, mtype, node = parts[:4]
    device = parts[4] if len(parts) >= 5 else None
    key = f"{group}/{node}/{device}" if device else f"{group}/{node}"
    try:
        payload = spb.decode(msg.payload)
    except Exception:
        return

    if mtype in ("NBIRTH", "DBIRTH"):
        store.on_birth(key, payload)
    elif mtype in ("NDATA", "DDATA"):
        if store.on_data(key, payload):
            request_rebirth(client, group, node)
    elif mtype in ("NDEATH", "DDEATH"):
        store.on_death(key)


def start_ingest():
    c = mqtt.Client(client_id="platform-ingest")
    c.on_connect = on_connect
    c.on_message = on_message
    while True:
        try:
            c.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            break
        except Exception as e:
            log.warning("Broker not ready (%s), retrying...", e)
            time.sleep(2)
    c.loop_forever()


# --- Web -------------------------------------------------------------------
@app.route("/api/state")
def api_state():
    return jsonify(store.snapshot())


@app.route("/")
def index():
    return render_template_string(DASHBOARD)


DASHBOARD = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>North Plant — Asset Health</title>
<style>
  :root { --bg:#0e1116; --card:#161b22; --line:#232a34; --ok:#2ea043;
          --warn:#d29922; --alarm:#f85149; --txt:#c9d1d9; --dim:#7d8590; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  header { padding:14px 22px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:14px; background:#0b0e13; }
  header h1 { font-size:16px; margin:0; font-weight:600; letter-spacing:.3px; }
  header .sub { color:var(--dim); font-size:12px; }
  .wrap { padding:20px; display:grid; grid-template-columns:2fr 1fr; gap:20px; }
  .grid { display:grid; grid-template-columns:repeat(2,1fr); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:10px; padding:16px; }
  .card h2 { margin:0 0 4px; font-size:14px; }
  .badge { font-size:11px; font-weight:700; padding:2px 8px; border-radius:20px;
           text-transform:uppercase; letter-spacing:.4px; }
  .OK { background:rgba(46,160,67,.15); color:var(--ok); }
  .WARNING { background:rgba(210,153,34,.15); color:var(--warn); }
  .ALARM { background:rgba(248,81,73,.16); color:var(--alarm); }
  .big { font-size:30px; font-weight:700; margin:8px 0 2px; }
  .unit { color:var(--dim); font-size:13px; font-weight:400; }
  .row { display:flex; justify-content:space-between; font-size:12px;
         color:var(--dim); padding:2px 0; }
  .row b { color:var(--txt); font-weight:500; }
  canvas { width:100%; height:48px; display:block; margin-top:8px; }
  .side h2 { font-size:13px; text-transform:uppercase; letter-spacing:.5px;
             color:var(--dim); margin:0 0 10px; }
  .wo { border-left:3px solid var(--alarm); background:#1a1d23;
        padding:10px 12px; border-radius:4px; margin-bottom:10px; font-size:13px; }
  .wo .id { color:var(--alarm); font-weight:700; }
  .wo .ts { color:var(--dim); font-size:11px; }
  .none { color:var(--dim); font-size:13px; }
  .offline { opacity:.5; }
  .tags { margin-top:8px; font-size:11px; color:var(--dim);
          word-break:break-all; line-height:1.5; }
  .banner { background:rgba(248,81,73,.16); border:1px solid var(--alarm);
            color:#ffb3ae; padding:10px 16px; margin:0 20px; border-radius:8px;
            font-size:13px; display:none; }
</style>
</head>
<body>
<header>
  <h1>North Plant · Asset Health</h1>
  <span class="sub">Pump House 1 — Condition Monitoring &nbsp;|&nbsp; Sparkplug B</span>
</header>
<div class="banner" id="banner"></div>
<div class="wrap">
  <div class="grid" id="pumps"></div>
  <div class="side">
    <div class="card">
      <h2>Maintenance Work Orders</h2>
      <div id="wos"><div class="none">No open work orders.</div></div>
    </div>
  </div>
</div>

<script>
function spark(canvas, data, alarm, warn) {
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.clientWidth * devicePixelRatio;
  const h = canvas.height = canvas.clientHeight * devicePixelRatio;
  ctx.clearRect(0,0,w,h);
  if (!data.length) return;
  const max = Math.max(alarm*1.15, ...data);
  const min = 0;
  const sx = w / Math.max(1, data.length - 1);
  const sy = v => h - ((v - min)/(max - min)) * h;
  // alarm / warn guide lines
  [[alarm,'#f85149'],[warn,'#d29922']].forEach(([lvl,c]) => {
    ctx.strokeStyle = c; ctx.globalAlpha = .35; ctx.setLineDash([4,4]);
    ctx.beginPath(); ctx.moveTo(0, sy(lvl)); ctx.lineTo(w, sy(lvl)); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  });
  ctx.strokeStyle = '#58a6ff'; ctx.lineWidth = 2*devicePixelRatio;
  ctx.beginPath();
  data.forEach((v,i) => { const x=i*sx, y=sy(v); i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
  ctx.stroke();
}

async function tick() {
  const s = await (await fetch('/api/state')).json();
  const box = document.getElementById('pumps');
  box.innerHTML = '';
  let alarms = [];
  s.pumps.filter(p => p.name.startsWith('Pump')).forEach(p => {
    const de = p.metrics['Bearing/DE Vibration'];
    const nde = p.metrics['Bearing/NDE Vibration'];
    const temp = p.metrics['Motor/Winding Temperature'];
    const spd = p.metrics['Process/Speed'];
    const run = p.metrics['Control/Run'];
    if (p.health === 'ALARM') alarms.push(p.name);
    const card = document.createElement('div');
    card.className = 'card' + (p.online ? '' : ' offline');
    // NOTE: metric/asset labels are rendered as trusted markup.
    card.innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:center">' +
        '<h2>' + p.name + '</h2>' +
        '<span class="badge ' + p.health + '">' + p.health + '</span>' +
      '</div>' +
      '<div class="big">' + (de==null?'--':Number(de).toFixed(2)) +
        ' <span class="unit">mm/s DE</span></div>' +
      '<div class="row"><span>NDE vibration</span><b>' +
        (nde==null?'--':Number(nde).toFixed(2)) + ' mm/s</b></div>' +
      '<div class="row"><span>Winding temp</span><b>' +
        (temp==null?'--':Number(temp).toFixed(1)) + ' &deg;C</b></div>' +
      '<div class="row"><span>Speed</span><b>' +
        (spd==null?'--':Number(spd).toFixed(0)) + ' %</b></div>' +
      '<div class="row"><span>Drive</span><b>' +
        (run===false?'STOPPED':'running') + '</b></div>' +
      // Tag list straight from the device's BIRTH — rendered as trusted markup.
      '<div class="tags">' + Object.keys(p.metrics).join(' &middot; ') + '</div>' +
      '<canvas></canvas>';
    box.appendChild(card);
    spark(card.querySelector('canvas'), p.de_history || [], s.alarm, s.warn);
  });

  const wos = document.getElementById('wos');
  if (s.work_orders.length) {
    wos.innerHTML = s.work_orders.map(w =>
      '<div class="wo"><div><span class="id">WO#' + w.id + '</span> · ' +
      w.asset + '</div><div>' + w.text + '</div>' +
      '<div class="ts">' + w.ts + '</div></div>').join('');
  } else {
    wos.innerHTML = '<div class="none">No open work orders.</div>';
  }

  const banner = document.getElementById('banner');
  if (alarms.length) {
    banner.style.display = 'block';
    banner.innerHTML = '&#9888; ALARM: high bearing vibration on ' + alarms.join(', ');
  } else { banner.style.display = 'none'; }
}
tick(); setInterval(tick, 2000);
</script>
</body>
</html>
"""


def main():
    threading.Thread(target=start_ingest, daemon=True).start()
    log.info("Dashboard on http://0.0.0.0:%s", HTTP_PORT)
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)


if __name__ == "__main__":
    main()
