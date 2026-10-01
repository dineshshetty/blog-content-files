"""
Modbus TCP server simulating four VFDs (unit IDs 101–104).

A write here is a write to the physical drive — this is where the command
injection lands. The edge node reads/writes these registers over Modbus;
the attacker never touches Modbus directly.

Holding registers per unit:
    HR[0]  speed setpoint   0-100 %
    HR[1]  run              0/1
    HR[2]  actual speed     ramps toward HR[0] in the physics loop
    HR[3]  fault code       0 = ok
"""

import logging
import os
import threading
import time

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [plc-sim] %(message)s")
log = logging.getLogger("plc-sim")

UNIT_IDS = [101, 102, 103, 104]
HR_SETPOINT, HR_RUN, HR_ACTUAL, HR_FAULT = 0, 1, 2, 3

# Initial operating point per pump (setpoint %, running).
INITIAL = {101: (78, 1), 102: (82, 1), 103: (70, 1), 104: (85, 1)}


def build_context():
    slaves = {}
    for uid in UNIT_IDS:
        sp, run = INITIAL[uid]
        block = ModbusSequentialDataBlock(0, [sp, run, 0, 0] + [0] * 16)
        slaves[uid] = ModbusSlaveContext(hr=block, zero_mode=True)
    return ModbusServerContext(slaves=slaves, single=False)


def physics_loop(context):
    """Ramp actual speed toward setpoint, like a real drive spinning up/down."""
    while True:
        for uid in UNIT_IDS:
            store = context[uid]
            setpoint = store.getValues(3, HR_SETPOINT, 1)[0]
            running = store.getValues(3, HR_RUN, 1)[0]
            actual = store.getValues(3, HR_ACTUAL, 1)[0]

            target = setpoint if running else 0
            # move at most 5 %/tick toward the target
            if actual < target:
                actual = min(target, actual + 5)
            elif actual > target:
                actual = max(target, actual - 5)

            store.setValues(3, HR_ACTUAL, [actual])
        time.sleep(0.5)


def main():
    host = os.environ.get("PLC_BIND", "0.0.0.0")
    port = int(os.environ.get("PLC_PORT", "502"))
    context = build_context()

    t = threading.Thread(target=physics_loop, args=(context,), daemon=True)
    t.start()

    log.info("VFD Modbus/TCP simulator listening on %s:%s (units %s)",
             host, port, UNIT_IDS)
    StartTcpServer(context=context, address=(host, port))


if __name__ == "__main__":
    main()
