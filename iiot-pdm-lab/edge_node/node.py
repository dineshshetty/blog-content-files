"""
Sparkplug B edge node — models a Cirrus Link / Ignition edge gateway.

Connects to the broker, publishes NBIRTH + DBIRTH per pump, streams DDATA
on aliases, handles NCMD rebirth and DCMD writes (mapped to Modbus). Sets
an NDEATH last-will so the platform knows if it drops.

Also runs a vibration model: Pump-101 has a seeded bearing defect that
grows over time, so the downstream analytics actually have something to catch.
"""

import logging
import math
import os
import random
import threading
import time

import paho.mqtt.client as mqtt
from pymodbus.client import ModbusTcpClient

from common import sparkplug as spb
from common.sparkplug import DataType as DT

logging.basicConfig(level=logging.INFO, format="%(asctime)s [edge] %(message)s")
log = logging.getLogger("edge")

GROUP = os.environ.get("SPB_GROUP", "NorthPlant")
NODE = os.environ.get("SPB_NODE", "EN-PumpHouse-01")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
PLC_HOST = os.environ.get("PLC_HOST", "localhost")
PLC_PORT = int(os.environ.get("PLC_PORT", "502"))
SAMPLE_INTERVAL = float(os.environ.get("SAMPLE_INTERVAL", "3"))

# How the seeded bearing fault develops (fast enough to demo, slow enough to
# look real). Vibration crosses the ISO 10816 alarm band (~7.1 mm/s) partway
# through the ramp.
FAULT_UNIT = 101
FAULT_DELAY = float(os.environ.get("FAULT_DELAY", "25"))      # s before it starts
FAULT_RAMP = float(os.environ.get("FAULT_RAMP", "240"))       # s to reach full
FAULT_MAX = float(os.environ.get("FAULT_MAX", "9.0"))         # mm/s added at full

PUMPS = [101, 102, 103, 104]
HR_SETPOINT, HR_RUN, HR_ACTUAL = 0, 1, 2

# Per-pump metric catalog. alias is stable and assigned in the BIRTH; DDATA
# then references the alias only. props carry engineering units + writability,
# which is exactly the kind of metadata a real edge node advertises.
def metric_catalog(pump):
    return [
        # (name, alias, datatype, initial, props)
        ("Bearing/DE Vibration", 1, DT.Double, 1.8,
         {"engUnit": (DT.String, "mm/s"), "writable": (DT.Boolean, False)}),
        ("Bearing/NDE Vibration", 2, DT.Double, 1.5,
         {"engUnit": (DT.String, "mm/s"), "writable": (DT.Boolean, False)}),
        ("Motor/Winding Temperature", 3, DT.Double, 52.0,
         {"engUnit": (DT.String, "degC"), "writable": (DT.Boolean, False)}),
        ("Process/Speed", 4, DT.Double, 0.0,
         {"engUnit": (DT.String, "%"), "writable": (DT.Boolean, False)}),
        ("Setpoint/Speed", 5, DT.Double, 0.0,
         {"engUnit": (DT.String, "%"), "writable": (DT.Boolean, True)}),
        ("Control/Run", 6, DT.Boolean, True,
         {"writable": (DT.Boolean, True)}),
        ("Diagnostics/Health", 7, DT.String, "OK",
         {"writable": (DT.Boolean, False)}),
    ]


class EdgeNode:
    def __init__(self):
        self.seq = 0
        self.bd_seq = 0
        self.alias_to_name = {p: {a: n for (n, a, *_1) in metric_catalog(p)} for p in PUMPS}
        self.name_to_alias = {p: {n: a for (n, a, *_1) in metric_catalog(p)} for p in PUMPS}
        self.start = time.monotonic()
        self.plc = ModbusTcpClient(PLC_HOST, port=PLC_PORT, timeout=2)
        self.local = {p: {"setpoint": 0.0, "run": True, "actual": 0.0} for p in PUMPS}

        self.client = mqtt.Client(client_id=f"{GROUP}-{NODE}")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        # Last will: NDEATH so subscribers learn immediately if we vanish.
        self.client.will_set(
            spb.topic(GROUP, "NDEATH", NODE),
            self._ndeath_payload(),
            qos=0,
            retain=False,
        )

    # --- sequence handling -------------------------------------------------
    def next_seq(self):
        s = self.seq
        self.seq = (self.seq + 1) % 256
        return s

    # --- payload builders --------------------------------------------------
    def _ndeath_payload(self):
        p = spb.new_payload(seq=None)  # NDEATH carries no seq
        spb.add_metric(p, name="bdSeq", datatype=DT.UInt64, value=self.bd_seq)
        return spb.encode(p)

    def publish_nbirth(self):
        self.seq = 0
        p = spb.new_payload(seq=self.next_seq())
        spb.add_metric(p, name="bdSeq", datatype=DT.UInt64, value=self.bd_seq)
        spb.add_metric(p, name="Node Control/Rebirth", datatype=DT.Boolean,
                       value=False, properties={"writable": (DT.Boolean, True)})
        spb.add_metric(p, name="Properties/Model", datatype=DT.String,
                       value="BV-EdgeGW-2000")
        spb.add_metric(p, name="Properties/Firmware", datatype=DT.String,
                       value="3.4.1")
        self.client.publish(spb.topic(GROUP, "NBIRTH", NODE), spb.encode(p), qos=0)
        log.info("NBIRTH published (bdSeq=%s)", self.bd_seq)

    def publish_dbirth(self, pump):
        self.read_plc(pump)
        st = self.local[pump]
        p = spb.new_payload(seq=self.next_seq())
        for (name, alias, dtype, initial, props) in metric_catalog(pump):
            value = initial
            if name == "Process/Speed":
                value = st["actual"]
            elif name == "Setpoint/Speed":
                value = st["setpoint"]
            elif name == "Control/Run":
                value = st["run"]
            spb.add_metric(p, name=name, alias=alias, datatype=dtype,
                           value=value, properties=props)
        self.client.publish(spb.topic(GROUP, "DBIRTH", NODE, f"Pump-{pump}"),
                            spb.encode(p), qos=0)
        log.info("DBIRTH published for Pump-%s", pump)

    def publish_ddata(self, pump):
        st = self.local[pump]
        de, nde, temp, health = self.sample_vibration(pump)
        if pump == FAULT_UNIT and health != "OK":
            # ground truth — what the machine is REALLY doing, regardless of
            # what an attacker may be telling the platform.
            log.info("GROUND TRUTH Pump-%s real DE=%.2f mm/s (%s)", pump, de, health)
        p = spb.new_payload(seq=self.next_seq())
        a = self.name_to_alias[pump]
        spb.add_metric(p, alias=a["Bearing/DE Vibration"], datatype=DT.Double, value=de)
        spb.add_metric(p, alias=a["Bearing/NDE Vibration"], datatype=DT.Double, value=nde)
        spb.add_metric(p, alias=a["Motor/Winding Temperature"], datatype=DT.Double, value=temp)
        spb.add_metric(p, alias=a["Process/Speed"], datatype=DT.Double, value=st["actual"])
        spb.add_metric(p, alias=a["Diagnostics/Health"], datatype=DT.String, value=health)
        self.client.publish(spb.topic(GROUP, "DDATA", NODE, f"Pump-{pump}"),
                            spb.encode(p), qos=0)

    # --- physical process --------------------------------------------------
    def read_plc(self, pump):
        st = self.local[pump]
        try:
            if not self.plc.connected:
                self.plc.connect()
            rr = self.plc.read_holding_registers(address=0, count=3, slave=pump)
            if not rr.isError():
                st["setpoint"] = float(rr.registers[HR_SETPOINT])
                st["run"] = bool(rr.registers[HR_RUN])
                st["actual"] = float(rr.registers[HR_ACTUAL])
        except Exception as e:  # keep running on a purely simulated process
            log.debug("PLC read failed for Pump-%s: %s", pump, e)
        return st

    def write_plc(self, pump, address, value):
        try:
            if not self.plc.connected:
                self.plc.connect()
            self.plc.write_register(address=address, value=int(value), slave=pump)
            return True
        except Exception as e:
            log.warning("PLC write failed for Pump-%s: %s", pump, e)
            return False

    def sample_vibration(self, pump):
        st = self.local[pump]
        speed = st["actual"]
        running = st["run"] and speed > 1

        # Baseline vibration rises gently with speed; near-zero when stopped.
        base = (1.0 + 0.02 * speed) if running else 0.2
        severity = 0.0
        if pump == FAULT_UNIT and running:
            t = time.monotonic() - self.start
            frac = max(0.0, min(1.0, (t - FAULT_DELAY) / FAULT_RAMP))
            # slightly super-linear: bearing damage accelerates
            severity = FAULT_MAX * (frac ** 1.3)

        de = base + severity + random.uniform(-0.15, 0.15)
        nde = 0.75 * base + 0.4 * severity + random.uniform(-0.12, 0.12)
        temp = (48 + 0.18 * speed + 2.6 * severity + random.uniform(-0.4, 0.4)
                if running else 24 + random.uniform(-0.3, 0.3))

        de = max(0.0, round(de, 2))
        nde = max(0.0, round(nde, 2))
        temp = round(temp, 1)
        health = "OK" if de < 4.5 else ("WARNING" if de < 7.1 else "ALARM")
        return de, nde, temp, health

    # --- MQTT callbacks ----------------------------------------------------
    def on_connect(self, client, userdata, flags, rc):
        log.info("Connected to broker %s:%s (rc=%s)", MQTT_HOST, MQTT_PORT, rc)
        # Subscribe to commands aimed at this node and its devices.
        client.subscribe(spb.topic(GROUP, "NCMD", NODE))
        client.subscribe(spb.topic(GROUP, "DCMD", NODE) + "/#")
        self.rebirth()

    def rebirth(self):
        self.publish_nbirth()
        for pump in PUMPS:
            self.publish_dbirth(pump)

    def on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        # spBv1.0 / group / type / node [ / device ]
        if len(parts) < 4:
            return
        msg_type = parts[2]
        device = parts[4] if len(parts) >= 5 else None
        try:
            payload = spb.decode(msg.payload)
        except Exception as e:
            log.warning("Undecodable command on %s: %s", msg.topic, e)
            return

        if msg_type == "NCMD":
            self.handle_ncmd(payload)
        elif msg_type == "DCMD" and device:
            self.handle_dcmd(device, payload)

    def handle_ncmd(self, payload):
        for m in payload.metrics:
            name = m.name or "(alias %s)" % m.alias
            if name == "Node Control/Rebirth" and spb.get_metric_value(m):
                log.info("NCMD Rebirth requested -> re-announcing")
                self.rebirth()

    def handle_dcmd(self, device, payload):
        try:
            pump = int(device.split("-")[1])
        except Exception:
            return
        if pump not in PUMPS:
            return
        for m in payload.metrics:
            name = m.name
            if not name and m.alias:
                name = self.alias_to_name.get(pump, {}).get(m.alias)
            value = spb.get_metric_value(m)
            if name == "Setpoint/Speed":
                sp = max(0, min(100, int(round(float(value)))))
                log.warning("DCMD -> Pump-%s Setpoint/Speed = %s%%", pump, sp)
                self.write_plc(pump, HR_SETPOINT, sp)
                self.local[pump]["setpoint"] = float(sp)
                self.publish_dbirth(pump)  # reflect the change
            elif name == "Control/Run":
                run = 1 if bool(value) else 0
                log.warning("DCMD -> Pump-%s Control/Run = %s", pump, bool(run))
                self.write_plc(pump, HR_RUN, run)
                self.local[pump]["run"] = bool(run)
                self.publish_dbirth(pump)

    # --- main loop ---------------------------------------------------------
    def run(self):
        while True:
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
                break
            except Exception as e:
                log.warning("Broker not ready (%s), retrying...", e)
                time.sleep(2)
        self.client.loop_start()

        while True:
            for pump in PUMPS:
                self.read_plc(pump)
                self.publish_ddata(pump)
            time.sleep(SAMPLE_INTERVAL)


if __name__ == "__main__":
    EdgeNode().run()
