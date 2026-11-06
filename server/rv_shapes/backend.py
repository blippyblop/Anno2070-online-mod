"""Wire-shape builders for ``AVBackEndServiceProtocol`` replies.

One pure function per method of the RendezVous backend service.  Each function
returns the reply *payload* only -- i.e. the method name block

    u16 len (INCLUDING the trailing NUL) | "BackEndServiceProtocol::<M>*" | 00

followed by the raw little-endian fields decoded from the client's reply reader
(``decomp/batch/step1_backend/__AVBackEndServiceProtocolClient_rdv__.c``, see
``decomp/batch/step1_backend/REPLIES.md``):

    #  method                  reply fields
    1  GetVersion_V1           u32
    2  GetProtocolVersions_V1  u32 count + count elements (layout unknown -> raw blobs)
    3  CheckLoginAllowed_V1    String msg + u16 kind      (an embedded Quazal Result)
    4  IsMaintenanceOngoing_V1 u8 bool
    5  GetMaintenanceTime_V1   u64 (Quazal DateTime)
    6  ScheduleMaintenance_V1  <void>
    7  StopMaintenance_V1      <void>
    8  GetProfilingUnits_V1    u32 count + N x (String name, u32, u32)
    9  GetSandboxName_V1       String

The envelope (size | proto name | type | status | call id) is added by the
caller -- ``mimic_server.rv_rmc_reply`` -- and is *not* produced here.  The
payload itself carries no call id: the server must echo the request's.

Rules honoured by every builder:

  * the name block is built by :func:`rv_shapes.name_block` (u16 length that
    INCLUDES the NUL, matching ``FUN_14135e680(stream, buf, 0xff)``);
  * Quazal ``String`` = u16 length EXCLUDING the NUL, then the bytes, no NUL on
    the wire (``FUN_141371620``);
  * all integers are little-endian and unpadded (the client ``memcpy``s them);
  * a bool is a single byte, non-zero == true.

Every function is *total*: no I/O, no globals mutated, no exceptions -- junk
arguments are coerced to their documented default instead of raising, because
a malformed reply is far worse than a defaulted one (the client silently
accepts truncated payloads).

``METHOD_NAMES`` maps the bare method name (``"GetVersion_V1"``) to its builder;
:func:`build` resolves a method given either the bare name, the name without
the ``_V1`` suffix, or the fully qualified ``"Proto::Method*"`` string, and
returns the complete payload (name block included) or ``None`` when the method
is not one of the nine handled here.
"""

import inspect
import struct

try:  # normal package import
    from . import name_block as _name_block
    from . import qstr as _qstr
    from . import u8 as _u8
    from . import u16 as _u16
    from . import u32 as _u32
    from . import u64 as _u64
except ImportError:  # pragma: no cover - flat sys.path (server/ on sys.path)
    from rv_shapes import name_block as _name_block
    from rv_shapes import qstr as _qstr
    from rv_shapes import u8 as _u8
    from rv_shapes import u16 as _u16
    from rv_shapes import u32 as _u32
    from rv_shapes import u64 as _u64

DEFAULT_PROTO = "BackEndServiceProtocol"

_STR_MAX = 0xFFFF          # Quazal String / name lengths are u16
_NAME_MAX = 0xFF           # name reader caps the buffer at 0xff (incl NUL)


# --------------------------------------------------------------------------
# coercion helpers -- total on any input, never raise
# --------------------------------------------------------------------------
def _proto_name(proto):
    """Return a usable protocol name (falls back to the documented default)."""
    if isinstance(proto, str) and proto:
        text = proto
    elif isinstance(proto, (bytes, bytearray, memoryview)):
        try:
            text = bytes(proto).decode("ascii")
        except Exception:
            return DEFAULT_PROTO
        if not text:
            return DEFAULT_PROTO
    else:
        return DEFAULT_PROTO
    try:
        # a protocol name must be a plain ASCII identifier: no separators or
        # control bytes, otherwise the client's "*"-suffixed compare can never
        # match and the name block would be malformed.
        if any(c in text for c in (":", "*", "\x00")):
            return DEFAULT_PROTO
        if not all(0x21 <= ord(c) <= 0x7E for c in text):
            return DEFAULT_PROTO
        # keep the block inside the reader's 0xff-byte buffer
        if len(text.encode("ascii")) > _NAME_MAX - 2:
            text = text[:_NAME_MAX - 2]
        text.encode("ascii")
    except Exception:
        return DEFAULT_PROTO
    return text


def _name(proto, method):
    """Name block for ``proto::method``; never raises."""
    try:
        return _name_block(_proto_name(proto), method)
    except Exception:  # pragma: no cover - defensive
        return _name_block(DEFAULT_PROTO, method)


def _int(value, default=0):
    """Best-effort integer; anything unconvertible becomes ``default``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except Exception:
        return default


def _text(value, default=""):
    """Best-effort byte string for a Quazal String / name field."""
    if isinstance(value, str):
        try:
            return value.encode("ascii")
        except Exception:
            try:
                return value.encode("utf-8", "replace")
            except Exception:  # pragma: no cover - defensive
                return default.encode("ascii", "replace")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return default.encode("ascii", "replace") if isinstance(default, str) else b""


def _s(value, default=""):
    """Quazal String field: u16 length EXCLUDING NUL + bytes (clipped to u16)."""
    data = _text(value, default)[:_STR_MAX]
    return _qstr(data)


def _bool_byte(value):
    """1 byte: 0x01 when truthy, 0x00 otherwise; never raises."""
    try:
        return _u8(1 if value else 0)
    except Exception:  # pragma: no cover - __bool__ may raise
        return b"\x00"


def _iter(value):
    """Iterate ``value`` defensively; scalars / str / bytes yield nothing."""
    if value is None or isinstance(value, (str, bytes, bytearray, memoryview)):
        return ()
    try:
        return list(value)
    except Exception:
        return ()


def _blob(entry):
    """One raw element blob for GetProtocolVersions (layout unknown)."""
    if isinstance(entry, (bytes, bytearray, memoryview)):
        return bytes(entry)
    if isinstance(entry, bool):
        return _u32(int(entry))
    if isinstance(entry, int):
        return _u32(entry)
    return b""


def _unit(item):
    """(name, a, b) for one GetProfilingUnits element; never raises."""
    if isinstance(item, dict):
        return (
            item.get("name", ""),
            _int(item.get("a", item.get("version", 0))),
            _int(item.get("b", item.get("flags", 0))),
        )
    if isinstance(item, (tuple, list, bytearray)):
        seq = list(item)
        if len(seq) >= 3:
            return seq[0], _int(seq[1]), _int(seq[2])
        if len(seq) == 2:
            return seq[0], _int(seq[1]), 0
        if len(seq) == 1:
            return seq[0], 0, 0
        return "", 0, 0
    if isinstance(item, (str, bytes, memoryview)):
        return item, 0, 0
    return "", 0, 0


# --------------------------------------------------------------------------
# 1..9 -- one pure builder per method, payload only
# --------------------------------------------------------------------------
def get_version(version=1, proto=DEFAULT_PROTO):
    """#1 GetVersion_V1 -> u32 (build number, not a string)."""
    return _name(proto, "GetVersion_V1") + _u32(_int(version, 1))


def get_protocol_versions(entries=(), proto=DEFAULT_PROTO):
    """#2 GetProtocolVersions_V1 -> u32 count + elements.

    The element layout (``FUN_140de9710``) is not decompiled, so elements are
    passed through verbatim: give a list of raw ``bytes`` blobs (an ``int`` is
    packed as a u32 for convenience) and they are counted and concatenated.
    An empty sequence is the safe probe: ``00 00 00 00``.
    """
    blobs = [_blob(entry) for entry in _iter(entries)]
    return _name(proto, "GetProtocolVersions_V1") + _u32(len(blobs)) + b"".join(blobs)


def check_login_allowed(msg="", kind=0, proto=DEFAULT_PROTO):
    """#3 CheckLoginAllowed_V1 -> String msg + u16 kind (embedded Result).

    ``msg=""`` with ``kind=0`` is the "clean" value (``Result{"",0}``); a
    non-zero kind turns the login screen into an error box showing ``msg``.
    """
    return _name(proto, "CheckLoginAllowed_V1") + _s(msg) + _u16(_int(kind, 0))


def is_maintenance_ongoing(flag=False, proto=DEFAULT_PROTO):
    """#4 IsMaintenanceOngoing_V1 -> u8 bool."""
    return _name(proto, "IsMaintenanceOngoing_V1") + _bool_byte(flag)


def get_maintenance_time(when=0, proto=DEFAULT_PROTO):
    """#5 GetMaintenanceTime_V1 -> u64 (Quazal DateTime; 0 = unset)."""
    return _name(proto, "GetMaintenanceTime_V1") + _u64(_int(when, 0))


def schedule_maintenance(proto=DEFAULT_PROTO):
    """#6 ScheduleMaintenance_V1 -> no fields (the client reads none)."""
    return _name(proto, "ScheduleMaintenance_V1")


def stop_maintenance(proto=DEFAULT_PROTO):
    """#7 StopMaintenance_V1 -> no fields (the client reads none)."""
    return _name(proto, "StopMaintenance_V1")


def get_profiling_units(units=(), proto=DEFAULT_PROTO):
    """#8 GetProfilingUnits_V1 -> u32 count + N x (String name, u32, u32).

    ``units`` is an iterable of ``(name, u32, u32)`` tuples/lists (a dict with
    ``name``/``a``/``b`` keys also works).  ``units=()`` yields the safe
    ``00 00 00 00`` reply.
    """
    body = []
    for item in _iter(units):
        name, first, second = _unit(item)
        body.append(_s(name) + _u32(first) + _u32(second))
    return _name(proto, "GetProfilingUnits_V1") + _u32(len(body)) + b"".join(body)


def get_sandbox_name(name="127.0.0.1", proto=DEFAULT_PROTO):
    """#9 GetSandboxName_V1 -> String (sandbox address shown by the client)."""
    return _name(proto, "GetSandboxName_V1") + _s(name, "127.0.0.1")


# --------------------------------------------------------------------------
# registry + generic builder
# --------------------------------------------------------------------------
METHOD_NAMES = {
    "GetVersion_V1": get_version,
    "GetProtocolVersions_V1": get_protocol_versions,
    "CheckLoginAllowed_V1": check_login_allowed,
    "IsMaintenanceOngoing_V1": is_maintenance_ongoing,
    "GetMaintenanceTime_V1": get_maintenance_time,
    "ScheduleMaintenance_V1": schedule_maintenance,
    "StopMaintenance_V1": stop_maintenance,
    "GetProfilingUnits_V1": get_profiling_units,
    "GetSandboxName_V1": get_sandbox_name,
}


def resolve_method(method):
    """Map a loosely-specified method name to its ``METHOD_NAMES`` key or None.

    Accepts ``"GetVersion_V1"``, ``"GetVersion"``,
    ``"BackEndServiceProtocol::GetVersion_V1"`` and the on-wire
    ``"..._V1*"`` form.
    """
    if isinstance(method, (bytes, bytearray, memoryview)):
        try:
            method = bytes(method).decode("ascii")
        except Exception:
            return None
    if not isinstance(method, str):
        return None
    text = method.strip().strip("\x00").strip()
    while text.endswith("*"):
        text = text[:-1].strip()
    if "::" in text:
        text = text.split("::")[-1].strip()
    if text in METHOD_NAMES:
        return text
    if text + "_V1" in METHOD_NAMES:
        return text + "_V1"
    return None


def build(proto, method, **kw):
    """Complete payload (name block + fields) for ``proto::method``.

    Returns ``None`` when the method is not one of the nine handled here.
    Unknown keyword arguments are ignored rather than raising.
    """
    key = resolve_method(method)
    if key is None:
        return None
    func = METHOD_NAMES[key]
    proto_name = _proto_name(proto)
    try:
        return func(proto=proto_name, **kw)
    except TypeError:
        pass
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        allowed = set(inspect.signature(func).parameters) - {"proto"}
        return func(proto=proto_name, **{k: v for k, v in kw.items() if k in allowed})
    except Exception:  # pragma: no cover - defensive
        return None
