"""
Thin, readable wrapper around the Eclipse Sparkplug B protobuf payload.

This is intentionally small so the encode/decode logic stays legible in the
blog. It compiles the bundled sparkplug_b.proto on first import if the
generated module is not present, so the lab works both in Docker and in a
plain virtualenv.
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PB2 = os.path.join(_HERE, "sparkplug_b_pb2.py")

# Compile the .proto on first use so there is no separate build step.
if not os.path.exists(_PB2):
    from grpc_tools import protoc

    rc = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{_HERE}",
            f"--python_out={_HERE}",
            os.path.join(_HERE, "sparkplug_b.proto"),
        ]
    )
    if rc != 0:
        raise RuntimeError("Failed to compile sparkplug_b.proto")

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import sparkplug_b_pb2 as sp  # noqa: E402

Payload = sp.Payload


# --- Sparkplug B DataType enum (subset we use) -----------------------------
class DataType:
    Int8 = 1
    Int16 = 2
    Int32 = 3
    Int64 = 4
    UInt8 = 5
    UInt16 = 6
    UInt32 = 7
    UInt64 = 8
    Float = 9
    Double = 10
    Boolean = 11
    String = 12
    DateTime = 13
    Text = 14
    UUID = 15


DATATYPE_NAMES = {v: k for k, v in vars(DataType).items() if not k.startswith("_")}


def now_ms():
    return int(time.time() * 1000)


def _set_value(metric, datatype, value):
    if value is None:
        metric.is_null = True
        return
    if datatype in (DataType.Int8, DataType.Int16, DataType.Int32,
                    DataType.UInt8, DataType.UInt16, DataType.UInt32):
        metric.int_value = int(value)
    elif datatype in (DataType.Int64, DataType.UInt64, DataType.DateTime):
        metric.long_value = int(value)
    elif datatype == DataType.Float:
        metric.float_value = float(value)
    elif datatype == DataType.Double:
        metric.double_value = float(value)
    elif datatype == DataType.Boolean:
        metric.boolean_value = bool(value)
    else:  # String / Text / UUID / fallback
        metric.string_value = str(value)


def _set_property(prop_value, datatype, value):
    prop_value.type = datatype
    if datatype in (DataType.Int8, DataType.Int16, DataType.Int32,
                    DataType.UInt8, DataType.UInt16, DataType.UInt32):
        prop_value.int_value = int(value)
    elif datatype in (DataType.Int64, DataType.UInt64):
        prop_value.long_value = int(value)
    elif datatype == DataType.Float:
        prop_value.float_value = float(value)
    elif datatype == DataType.Double:
        prop_value.double_value = float(value)
    elif datatype == DataType.Boolean:
        prop_value.boolean_value = bool(value)
    else:
        prop_value.string_value = str(value)


def new_payload(seq=None, timestamp=None):
    p = Payload()
    p.timestamp = timestamp if timestamp is not None else now_ms()
    if seq is not None:
        p.seq = seq
    return p


def add_metric(payload, name=None, datatype=DataType.Double, value=0.0,
               alias=None, timestamp=None, properties=None):
    """properties: dict of {key: (datatype, value)} -> Sparkplug PropertySet."""
    m = payload.metrics.add()
    if name is not None:
        m.name = name
    if alias is not None:
        m.alias = alias
    m.timestamp = timestamp if timestamp is not None else now_ms()
    m.datatype = datatype
    _set_value(m, datatype, value)
    if properties:
        for key, (dt, val) in properties.items():
            m.properties.keys.append(key)
            _set_property(m.properties.values.add(), dt, val)
    return m


def get_metric_value(metric):
    which = metric.WhichOneof("value")
    if which is None:
        return None
    return getattr(metric, which)


def get_property(metric, key):
    props = metric.properties
    for i, k in enumerate(props.keys):
        if k == key:
            pv = props.values[i]
            which = pv.WhichOneof("value")
            return getattr(pv, which) if which else None
    return None


def encode(payload):
    return payload.SerializeToString()


def decode(data):
    p = Payload()
    p.ParseFromString(data)
    return p


def topic(group, msg_type, node, device=None):
    base = f"spBv1.0/{group}/{msg_type}/{node}"
    return f"{base}/{device}" if device else base
