#!/usr/bin/env python3
"""Shape tests for ``server/rv_shapes/backend.py`` (BackEndServiceProtocol).

Plain python, no pytest: run with

    source ~/tools/env.sh && python3 tools/test_rv_shapes_backend.py

Exit code 0 means every check passed.  The expected bytes are re-derived here
from ``decomp/batch/step1_backend/REPLIES.md`` with ``struct`` directly -- the
builders' own helpers are deliberately *not* reused, so a bug in
``rv_shapes.name_block`` / ``qstr`` shows up as a failure instead of agreeing
with itself.

Checks:
  * name block: u16 len (incl NUL) + "BackEndServiceProtocol::<M>*\0", for all 9;
  * field bytes, byte-for-byte, for fixed inputs (incl. little-endianness and
    the String length convention: u16 EXCLUDING the NUL, no NUL on the wire);
  * totality: junk arguments never raise and always return bytes;
  * METHOD_NAMES covers exactly the 9 methods from REPLIES.md;
  * build() resolves bare / _V1-less / qualified names and returns None for
    unknown methods.
"""

import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

from rv_shapes import backend as B  # noqa: E402

PROTO = "BackEndServiceProtocol"
METHODS = [
    "GetVersion_V1",
    "GetProtocolVersions_V1",
    "CheckLoginAllowed_V1",
    "IsMaintenanceOngoing_V1",
    "GetMaintenanceTime_V1",
    "ScheduleMaintenance_V1",
    "StopMaintenance_V1",
    "GetProfilingUnits_V1",
    "GetSandboxName_V1",
]

CHECKS = 0
FAILURES = []


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if ok:
        print("PASS %s" % label)
    else:
        FAILURES.append(label)
        print("FAIL %s%s" % (label, ("  <- " + detail) if detail else ""))


def eq(label, got, want):
    ok = got == want and isinstance(got, bytes)
    if ok:
        check(label, True)
    else:
        check(
            label,
            False,
            "got %r (%s) want %r (%s)"
            % (
                got,
                type(got).__name__,
                want,
                " ".join("%02x" % b for b in want[:8]) + ("..." if len(want) > 8 else ""),
            ),
        )


# --- reference encoders (independent of rv_shapes.__init__) ----------------
def nb(method, proto=PROTO):
    """Name block: u16 len INCLUDING NUL, then 'Proto::Method*' + 00."""
    nm = ("%s::%s*" % (proto, method)).encode("ascii") + b"\x00"
    return struct.pack("<H", len(nm)) + nm


def qs(text):
    """Quazal String: u16 len INCLUDING the NUL, then bytes + NUL."""
    b = text.encode("ascii")
    return struct.pack("<H", len(b) + 1) + b + b"\x00"


U8 = lambda v: struct.pack("<B", v & 0xFF)      # noqa: E731
U16 = lambda v: struct.pack("<H", v & 0xFFFF)   # noqa: E731
U32 = lambda v: struct.pack("<I", v & 0xFFFFFFFF)          # noqa: E731
U64 = lambda v: struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)  # noqa: E731


def name_block_ok(payload, method):
    """Validate the payload's leading name block structurally."""
    if not isinstance(payload, bytes) or len(payload) < 4:
        return False, "not bytes / too short"
    declared = struct.unpack("<H", payload[:2])[0]
    if declared < 3 or 2 + declared > len(payload):
        return False, "declared len %d does not fit" % declared
    name = payload[2:2 + declared]
    want = ("%s::%s*" % (PROTO, method)).encode("ascii") + b"\x00"
    if name != want:
        return False, "name %r != %r" % (name, want)
    return True, ""


def name_block_shape_ok(payload, method, proto=None):
    """Structural check: u16 len incl NUL, name ends with '::<method>*\\0'."""
    if not isinstance(payload, bytes) or len(payload) < 4:
        return False, "not bytes / too short"
    declared = struct.unpack("<H", payload[:2])[0]
    if declared < 3 or 2 + declared > len(payload):
        return False, "declared len %d does not fit" % declared
    name = payload[2:2 + declared]
    tail = ("::%s*\x00" % method).encode("ascii")
    if not name.endswith(tail):
        return False, "name %r does not end with %r" % (name, tail)
    if proto is not None and name != ("%s::%s*" % (proto, method)).encode("ascii") + b"\x00":
        return False, "name %r != proto %r" % (name, proto)
    head = name[:-len(tail)]
    if not head or not all(0x21 <= b <= 0x7E and b not in b":*" for b in head):
        return False, "bad proto part %r" % (head,)
    return True, ""


print("== name block ==")
for m in METHODS:
    builder = B.METHOD_NAMES[m]
    payload = builder()
    ok, why = name_block_ok(payload, m)
    check("name_block %s" % m, ok, why)
    # declared length must include the trailing NUL exactly once
    check(
        "name_len_incl_nul %s" % m,
        payload[2:2 + struct.unpack("<H", payload[:2])[0]][-1:] == b"\x00",
        "%r" % (payload[:6],),
    )

print("== #1 GetVersion_V1 (u32) ==")
eq("get_version(3)", B.get_version(3), nb("GetVersion_V1") + U32(3))
eq("get_version() default", B.get_version(), nb("GetVersion_V1") + U32(1))
eq(
    "get_version(0x01020304) little-endian",
    B.get_version(0x01020304),
    nb("GetVersion_V1") + b"\x04\x03\x02\x01",
)
eq(
    "get_version() not a string",
    B.get_version(),
    nb("GetVersion_V1") + b"\x01\x00\x00\x00",
)

print("== #2 GetProtocolVersions_V1 (u32 count + raw elements) ==")
eq(
    "get_protocol_versions() empty probe",
    B.get_protocol_versions(),
    nb("GetProtocolVersions_V1") + b"\x00\x00\x00\x00",
)
eq(
    "get_protocol_versions((b'\\xde\\xad\\xbe\\xef',))",
    B.get_protocol_versions((b"\xde\xad\xbe\xef",)),
    nb("GetProtocolVersions_V1") + U32(1) + b"\xde\xad\xbe\xef",
)
eq(
    "get_protocol_versions(2 raw blobs, counted)",
    B.get_protocol_versions([b"\x01\x02", b""]),
    nb("GetProtocolVersions_V1") + U32(2) + b"\x01\x02",
)
eq(
    "get_protocol_versions((7,)) int -> u32 blob",
    B.get_protocol_versions((7,)),
    nb("GetProtocolVersions_V1") + U32(1) + U32(7),
)

print("== #3 CheckLoginAllowed_V1 (String msg + u16 kind) ==")
eq(
    "check_login_allowed() clean",
    B.check_login_allowed(),
    nb("CheckLoginAllowed_V1") + b"\x01\x00\x00" + U16(0),
)
eq(
    "check_login_allowed('', 0)",
    B.check_login_allowed("", 0),
    nb("CheckLoginAllowed_V1") + b"\x01\x00\x00" + b"\x00\x00",
)
eq(
    "check_login_allowed('bad sandbox', 5)",
    B.check_login_allowed("bad sandbox", 5),
    nb("CheckLoginAllowed_V1") + qs("bad sandbox") + b"\x05\x00",
)
eq(
    "check_login_allowed string len includes the NUL",
    B.check_login_allowed("AB", 0)[-(3 + 2 + 2):],
    b"\x03\x00AB\x00\x00\x00",
)

print("== #4 IsMaintenanceOngoing_V1 (u8 bool) ==")
eq(
    "is_maintenance_ongoing()",
    B.is_maintenance_ongoing(),
    nb("IsMaintenanceOngoing_V1") + b"\x00",
)
eq(
    "is_maintenance_ongoing(True)",
    B.is_maintenance_ongoing(True),
    nb("IsMaintenanceOngoing_V1") + b"\x01",
)
eq(
    "is_maintenance_ongoing(False) is one byte",
    B.is_maintenance_ongoing(False)[len(nb("IsMaintenanceOngoing_V1")):],
    b"\x00",
)

print("== #5 GetMaintenanceTime_V1 (u64) ==")
eq(
    "get_maintenance_time()",
    B.get_maintenance_time(),
    nb("GetMaintenanceTime_V1") + U64(0),
)
eq(
    "get_maintenance_time(0x1122334455667788) little-endian",
    B.get_maintenance_time(0x1122334455667788),
    nb("GetMaintenanceTime_V1") + b"\x88\x77\x66\x55\x44\x33\x22\x11",
)
check(
    "get_maintenance_time is 8 field bytes",
    len(B.get_maintenance_time(1)) - len(nb("GetMaintenanceTime_V1")) == 8,
    str(len(B.get_maintenance_time(1)) - len(nb("GetMaintenanceTime_V1"))),
)

print("== #6/#7 Schedule & StopMaintenance_V1 (void) ==")
eq("schedule_maintenance()", B.schedule_maintenance(), nb("ScheduleMaintenance_V1"))
eq("stop_maintenance()", B.stop_maintenance(), nb("StopMaintenance_V1"))

print("== #8 GetProfilingUnits_V1 (u32 count + N x (String,u32,u32)) ==")
eq(
    "get_profiling_units() empty",
    B.get_profiling_units(),
    nb("GetProfilingUnits_V1") + b"\x00\x00\x00\x00",
)
eq(
    "get_profiling_units 2 items",
    B.get_profiling_units((("Frame", 1, 2), ("Render", 0x10, 0x20))),
    nb("GetProfilingUnits_V1")
    + U32(2)
    + qs("Frame")
    + U32(1)
    + U32(2)
    + qs("Render")
    + U32(0x10)
    + U32(0x20),
)
eq(
    "get_profiling_units lists work too",
    B.get_profiling_units([["A", 0, 0]]),
    nb("GetProfilingUnits_V1") + U32(1) + qs("A") + U32(0) + U32(0),
)

print("== #9 GetSandboxName_V1 (String) ==")
eq(
    "get_sandbox_name() default",
    B.get_sandbox_name(),
    nb("GetSandboxName_V1") + b"\x0a\x00" + b"127.0.0.1\x00",
)
eq(
    "get_sandbox_name('localhost')",
    B.get_sandbox_name("localhost"),
    nb("GetSandboxName_V1") + b"\x0a\x00" + b"localhost\x00",
)
check(
    "get_sandbox_name ends with the terminator",
    B.get_sandbox_name("abc")[-1:] == b"\x00",
    repr(B.get_sandbox_name("abc")[-4:]),
)

print("== registry ==")
check(
    "METHOD_NAMES keys == the 9 REPLIES.md methods",
    set(B.METHOD_NAMES) == set(METHODS),
    "extra=%s missing=%s"
    % (sorted(set(B.METHOD_NAMES) - set(METHODS)), sorted(set(METHODS) - set(B.METHOD_NAMES))),
)
check(
    "METHOD_NAMES has exactly 9 entries",
    len(B.METHOD_NAMES) == 9,
    str(len(B.METHOD_NAMES)),
)
check(
    "METHOD_NAMES values are callables",
    all(callable(f) for f in B.METHOD_NAMES.values()),
)

for m in METHODS:
    payload = B.build(PROTO, m)
    ok, why = name_block_ok(payload, m)
    check("build(PROTO, %r) payload" % m, ok and payload == B.METHOD_NAMES[m](), why)

eq("build() bare name", B.build(PROTO, "GetVersion_V1", version=9),
   nb("GetVersion_V1") + U32(9))
eq("build() without _V1 suffix", B.build(PROTO, "GetVersion", version=9),
   nb("GetVersion_V1") + U32(9))
eq(
    "build() fully qualified + '*'",
    B.build(PROTO, "BackEndServiceProtocol::GetSandboxName_V1*"),
    nb("GetSandboxName_V1") + qs("127.0.0.1"),
)
eq(
    "build() kwargs reach the builder",
    B.build(PROTO, "CheckLoginAllowed_V1", msg="nope", kind=7),
    nb("CheckLoginAllowed_V1") + qs("nope") + U16(7),
)
eq(
    "build() unknown kwargs ignored",
    B.build(PROTO, "CheckLoginAllowed_V1", msg="x", bogus=1),
    nb("CheckLoginAllowed_V1") + qs("x") + U16(0),
)
check(
    "build() unknown method -> None",
    B.build(PROTO, "NoSuchMethod_V1") is None
    and B.build(PROTO, "") is None
    and B.build(PROTO, None) is None
    and B.build(PROTO, 42) is None,
)
eq(
    "build() custom proto",
    B.build("OtherProtocol", "GetVersion_V1", version=2),
    nb("GetVersion_V1", proto="OtherProtocol") + U32(2),
)
eq(
    "build() junk proto falls back to default",
    B.build(None, "GetVersion_V1", version=2),
    nb("GetVersion_V1") + U32(2),
)
eq(
    "build() control-char proto falls back to default",
    B.build("\x00*\x00", "GetVersion_V1", version=2),
    nb("GetVersion_V1") + U32(2),
)
eq(
    "build() bytes proto accepted",
    B.build(b"OtherProtocol", "GetVersion_V1", version=2),
    nb("GetVersion_V1", proto="OtherProtocol") + U32(2),
)

print("== totality (junk in -> bytes out, never raises) ==")
JUNK = [
    None,
    0,
    1,
    -1,
    2 ** 70,
    -2 ** 70,
    3.5,
    True,
    False,
    "",
    "x",
    "é",
    "\x00*\x00",
    b"",
    b"\x00\xff",
    bytearray(b"ab"),
    memoryview(b"cd"),
    [],
    (),
    {},
    {1: 2},
    [None],
    [None, None, None],
    [(None, None, None)],
    [("a", "b", "c")],
    [b"\x01"],
    object(),
    iter([1, 2]),
    len,
]


def totality(label, func, nargs):
    global CHECKS
    bad = []
    for junk in JUNK:
        args = [junk] * nargs
        try:
            out = func(*args)
        except Exception as exc:  # noqa: BLE001 - that is exactly what we test
            bad.append("arg=%r raised %s: %s" % (junk, type(exc).__name__, exc))
            continue
        if not isinstance(out, bytes):
            bad.append("arg=%r -> %s" % (junk, type(out).__name__))
    check("total: %s" % label, not bad, "; ".join(bad[:3]))


totality("get_version", B.get_version, 1)
totality("get_protocol_versions", B.get_protocol_versions, 1)
totality("check_login_allowed(msg, kind)", B.check_login_allowed, 2)
totality("is_maintenance_ongoing", B.is_maintenance_ongoing, 1)
totality("get_maintenance_time", B.get_maintenance_time, 1)
totality("schedule_maintenance", B.schedule_maintenance, 0)
totality("stop_maintenance", B.stop_maintenance, 0)
totality("get_profiling_units", B.get_profiling_units, 1)
totality("get_sandbox_name", B.get_sandbox_name, 1)

# junk must not break the envelope's expectations either
bad = []
for m in METHODS:
    for junk in JUNK:
        try:
            out = B.build(junk, m)
            if not isinstance(out, bytes) or not name_block_shape_ok(out, m)[0]:
                bad.append("(%r,%s)" % (junk, m))
        except Exception as exc:  # noqa: BLE001
            bad.append("(%r,%s) raised %s" % (junk, m, type(exc).__name__))
check("total: build(junk_proto, method)", not bad, str(bad[:3]))

bad = []
for junk in JUNK:
    try:
        out = B.build(PROTO, junk)
    except Exception as exc:  # noqa: BLE001
        bad.append("%r raised %s" % (junk, type(exc).__name__))
        continue
    if out is not None and not isinstance(out, bytes):
        bad.append("%r -> %s" % (junk, type(out).__name__))
check("total: build(PROTO, junk_method)", not bad, str(bad[:3]))

# clamping: over-long strings must still produce a valid u16 length
long_name = "A" * 70000
payload = B.get_sandbox_name(long_name)
declared = struct.unpack("<H", payload[len(nb("GetSandboxName_V1")):][:2])[0]
check(
    "over-long String clipped to u16",
    declared == 0xFFFF and len(payload) == len(nb("GetSandboxName_V1")) + 2 + 0xFFFF,
    "declared=%d payload=%d" % (declared, len(payload)),
)

print("")
print("-" * 60)
print("checks: %d  failures: %d" % (CHECKS, len(FAILURES)))
if FAILURES:
    print("FAILED:")
    for label in FAILURES:
        print("  - %s" % label)
    print("RESULT: FAIL")
    sys.exit(1)
print("RESULT: PASS (%d/%d)" % (CHECKS, CHECKS))
sys.exit(0)
