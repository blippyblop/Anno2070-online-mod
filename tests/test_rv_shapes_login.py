#!/usr/bin/env python3
"""Shape tests for ``server/rv_shapes/login.py`` (LoginProtocol).

Plain python, no pytest:

    source ~/tools/env.sh && python3 tools/test_rv_shapes_login.py

Exit code 0 means every check passed.  Every expected byte below is re-derived
here from ``decomp/_adhoc/loginpayload.md`` and
``decomp/_adhoc/rmcsuccess.md`` with ``struct`` directly -- the module's own
helpers are deliberately *not* reused, so a bug in ``rv_shapes.name_block`` /
``qstr`` shows up as a failure instead of agreeing with itself.

Checks, per method (all five: LoginWithToken_V1, LoginWithS2SToken_V1,
Register_V1, RedirectLogin_V1, RenewWithTicket_V1):
  * the name block: u16 length INCLUDING the NUL, the literal
    "LoginProtocol::<M>*" + NUL (the trailing '*' is what FUN_141370440's
    _mbscmp compares against), byte-by-byte;
  * every field's offset / width / endianness;
  * the Quazal String convention: u16 length EXCLUDING the NUL, no NUL on the
    wire (FUN_141371620) -- contrasted with the name's convention above;
  * the GUID both as a 16-byte blob and as "xxxxxxxx-..." text (4/2/2/8
    Windows layout, FUN_1413714c0 / FUN_141353360);
  * byte-for-byte equality with the live ``rvlogin`` fallback in
    ``mimic_server.rv_rmc_reply`` (re-implemented here as an oracle);
  * totality (junk arguments never raise), purity (no globals mutated),
    build()/resolve_method() dispatch and package registration.
"""

import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import rv_shapes  # noqa: E402
from rv_shapes import login as L  # noqa: E402

VERBOSE = bool(os.environ.get("RV_TEST_VERBOSE"))
PROTO = "LoginProtocol"

# client dispatch order, decomp/_adhoc/loginpayload.md §1
METHODS = [
    "LoginWithToken_V1",
    "LoginWithS2SToken_V1",
    "Register_V1",
    "RedirectLogin_V1",
    "RenewWithTicket_V1",
]

CHECKS = 0
FAILURES = []


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if ok:
        if VERBOSE:
            print("PASS %s" % label)
    else:
        FAILURES.append(label)
        print("FAIL %s%s" % (label, ("  <- " + detail) if detail else ""))


def check_bytes(label, got, want):
    """Length check + one assertion per byte (so offsets are pinned down)."""
    got = bytes(got)
    want = bytes(want)
    check("%s length" % label, len(got) == len(want),
          "got %d B, want %d B" % (len(got), len(want)))
    for i in range(min(len(got), len(want))):
        check("%s byte %d" % (label, i), got[i] == want[i],
              "got 0x%02x, want 0x%02x" % (got[i], want[i]))


# --------------------------------------------------------------------------
# independent re-derivation of the wire shapes (no rv_shapes helpers used)
# --------------------------------------------------------------------------
def nb(proto, method):
    """u16 len INCL NUL | "Proto::Method*" | NUL  (FUN_141346db0/FUN_14135e680)."""
    nm = (proto + "::" + method + "*").encode("ascii") + b"\x00"
    return struct.pack("<H", len(nm)) + nm


def qs(text):
    """Quazal String: u16 len INCLUDING the NUL, then bytes + NUL.

    The engine's writer FUN_141370f90 emits (strlen + 1) and a terminator; the
    reader sizes the object to len + 1 and copies len bytes, so the last byte
    must be the 0."""
    b = text.encode("ascii")
    return struct.pack("<H", len(b) + 1) + b + b"\x00"


def guid_bytes(data1, data2, data3, data4):
    """4/2/2/8 Windows GUID: u32, u16, u16 (LE) + 8 raw bytes."""
    return struct.pack("<IHH8s", data1, data2, data3, data4)


GUID_DEF = bytes.fromhex("000000000000000000000000000000aa")
GUID_TEXT = "00000000-0000-0000-0000-0000000000aa"

EXPECTED = {
    "LoginWithToken_V1": nb(PROTO, "LoginWithToken_V1") + GUID_DEF + qs("Player") + b"\x01",
    "LoginWithS2SToken_V1": nb(PROTO, "LoginWithS2SToken_V1") + GUID_DEF + qs("Player") + b"\x01",
    "Register_V1": nb(PROTO, "Register_V1") + struct.pack("<I", 1) + qs("Player"),
    "RedirectLogin_V1": nb(PROTO, "RedirectLogin_V1"),
    "RenewWithTicket_V1": nb(PROTO, "RenewWithTicket_V1"),
}

NAME_LEN = {                      # u16 inside the name block = len(literal)+1
    "LoginWithToken_V1": 0x22,    # loginpayload.md §4: "22 00 ... 2a 00"
    "LoginWithS2SToken_V1": 0x25,
    "Register_V1": 0x1C,
    "RedirectLogin_V1": 0x21,
    "RenewWithTicket_V1": 0x23,
}

BUILDERS = {
    "LoginWithToken_V1": L.login_with_token,
    "LoginWithS2SToken_V1": L.login_with_s2s_token,
    "Register_V1": L.register,
    "RedirectLogin_V1": L.redirect_login,
    "RenewWithTicket_V1": L.renew_with_ticket,
}


def body_off(method):
    """Offset of the first field = length of the name block."""
    return len(nb(PROTO, method))


print("== defaults / constants ==")
check("DEFAULT_PROTO", L.DEFAULT_PROTO == PROTO, repr(L.DEFAULT_PROTO))
check("DEFAULT_GUID text", L.DEFAULT_GUID == GUID_TEXT, repr(L.DEFAULT_GUID))
check("DEFAULT_GUID_BYTES", L.DEFAULT_GUID_BYTES == GUID_DEF, L.DEFAULT_GUID_BYTES.hex())
check("DEFAULT_GUID_BYTES is 16 B", len(L.DEFAULT_GUID_BYTES) == 16)
check("DEFAULT_GUID_BYTES ends 0xaa", L.DEFAULT_GUID_BYTES[-1] == 0xAA)
check("DEFAULT_NAME", L.DEFAULT_NAME == "Player", repr(L.DEFAULT_NAME))
check("DEFAULT_OK is True", L.DEFAULT_OK is True)
check("DEFAULT_PLAYER_ID", L.DEFAULT_PLAYER_ID == 1, repr(L.DEFAULT_PLAYER_ID))
check("METHODS order", tuple(L.METHODS) == tuple(METHODS), repr(L.METHODS))
check("METHOD_NAMES keys", set(L.METHOD_NAMES) == set(METHODS), repr(set(L.METHOD_NAMES)))

print("== name blocks (u16 INCL NUL, literal + '*' + NUL) ==")
for m in METHODS:
    literal = "%s::%s*" % (PROTO, m)
    payload = BUILDERS[m]()
    n = body_off(m)
    check("%s name length prefix" % m,
          struct.unpack_from("<H", payload, 0)[0] == len(literal) + 1,
          "declared %d, want %d" % (struct.unpack_from("<H", payload, 0)[0],
                                    len(literal) + 1))
    check("%s name length constant" % m,
          struct.unpack_from("<H", payload, 0)[0] == NAME_LEN[m],
          "0x%02x" % struct.unpack_from("<H", payload, 0)[0])
    check_bytes("%s name block" % m, payload[:n], nb(PROTO, m))
    check("%s name ends '*\\0'" % m,
          payload[n - 2:n] == b"*\x00", payload[n - 2:n].hex())
    check("%s name has no NUL before '*' (u16 incl NUL only)" % m,
          payload[2:n - 1] == literal.encode("ascii")
          and b"\x00" not in payload[2:n - 1],
          payload[2:n].hex())

print("== per-method payload bytes (builder + package build) ==")
for m in METHODS:
    want = EXPECTED[m]
    check_bytes("builder %s payload" % m, BUILDERS[m](), want)
    check_bytes("rv_shapes.build %s payload" % m,
                rv_shapes.build(PROTO, m) or b"", want)
    check("builder %s == package build %s" % (m, m),
          BUILDERS[m]() == rv_shapes.build(PROTO, m))
    check("%s name block fits inside the payload" % m,
          struct.unpack_from("<H", BUILDERS[m](), 0)[0] + 2 <= len(want),
          "%d + 2 vs %d" % (struct.unpack_from("<H", BUILDERS[m](), 0)[0], len(want)))

print("== field offsets / widths / endianness ==")
n = body_off("LoginWithToken_V1")
p = L.login_with_token()
check("LoginWithToken total length", len(p) == n + 16 + 2 + 7 + 1,
      "%d B" % len(p))
check_bytes("LoginWithToken GUID at +%d" % n, p[n:n + 16], GUID_DEF)
check("LoginWithToken String length at +%d == 7 (INCL NUL)" % (n + 16),
      struct.unpack_from("<H", p, n + 16)[0] == 7,
      "%d" % struct.unpack_from("<H", p, n + 16)[0])
check_bytes("LoginWithToken name field at +%d" % (n + 18), p[n + 18:n + 24], b"Player")
check("LoginWithToken name is NUL-terminated", p[n + 24] == 0x00, "0x%02x" % p[n + 24])
check("LoginWithToken bool at +%d == 1" % (n + 25), p[n + 25] == 0x01,
      "0x%02x" % p[n + 25])
check("LoginWithToken ends with the terminator then the bool", p[-2:] == b"\x00\x01",
      p[-3:].hex())

n2 = body_off("LoginWithS2SToken_V1")
p2 = L.login_with_s2s_token()
check("LoginWithS2SToken total length", len(p2) == n2 + 16 + 2 + 7 + 1)
check_bytes("LoginWithS2SToken GUID at +%d" % n2, p2[n2:n2 + 16], GUID_DEF)
check("LoginWithS2SToken String length == 7", struct.unpack_from("<H", p2, n2 + 16)[0] == 7)
check("LoginWithS2SToken bool == 1", p2[-1] == 0x01, "0x%02x" % p2[-1])
check("LoginWithS2SToken body == LoginWithToken body",
      p2[n2:] == p[n:], "%r vs %r" % (p2[n2:], p[n:]))
check("LoginWithS2SToken name differs from LoginWithToken name",
      p2[:n2] != p[:n])

nr = body_off("Register_V1")
pr = L.register()
check("Register total length", len(pr) == nr + 4 + 2 + 7, "%d B" % len(pr))
check_bytes("Register u32 at +%d" % nr, pr[nr:nr + 4], b"\x01\x00\x00\x00")
check("Register u32 == 1 (LE)", struct.unpack_from("<I", pr, nr)[0] == 1)
check("Register String length at +%d == 7 (INCL NUL)" % (nr + 4),
      struct.unpack_from("<H", pr, nr + 4)[0] == 7)
check_bytes("Register name field at +%d" % (nr + 6), pr[nr + 6:nr + 12], b"Player")
check("Register name is NUL-terminated", pr[nr + 12:nr + 13] == b"\x00",
      pr[nr + 12:nr + 13].hex())
check("Register has no GUID", b"\xaa" not in pr)

for void_method in ("RedirectLogin_V1", "RenewWithTicket_V1"):
    nv = body_off(void_method)
    pv = BUILDERS[void_method]()
    check("%s is name block only" % void_method, len(pv) == nv, "%d B" % len(pv))
    check("%s has no trailing fields" % void_method, pv[nv:] == b"", pv[nv:].hex())
    check("%s builder == build" % void_method,
          pv == L.build(PROTO, void_method))

print("== GUID: blob and text forms ==")
# default: blob, dashed text and every tolerated spelling must agree
check("encode_guid(None) == default blob", L.encode_guid(None) == GUID_DEF)
check("encode_guid(default text) == default blob", L.encode_guid(GUID_TEXT) == GUID_DEF)
check("encode_guid(upper text)", L.encode_guid(GUID_TEXT.upper()) == GUID_DEF)
check("encode_guid(braces)", L.encode_guid("{%s}" % GUID_TEXT) == GUID_DEF)
check("encode_guid(spaces)", L.encode_guid(" %s " % GUID_TEXT) == GUID_DEF)
check("encode_guid(no dashes)",
      L.encode_guid("000000000000000000000000000000aa") == GUID_DEF)
check("encode_guid(blob) is identity",
      L.encode_guid(bytes(range(16))) == bytes(range(16)))
check("encode_guid(bytearray)",
      L.encode_guid(bytearray(GUID_DEF)) == GUID_DEF)
check("encode_guid(memoryview)",
      L.encode_guid(memoryview(GUID_DEF)) == GUID_DEF)
# 16-int sequence
check("encode_guid(list of 16 ints)",
      L.encode_guid(list(GUID_DEF)) == GUID_DEF)
# 4/2/2/8 layout: Data1/Data2/Data3 are VALUES, so they go out little-endian
DISTINCT_TEXT = "01020304-0506-0708-090a-0b0c0d0e0f10"
DISTINCT_WIRE = bytes.fromhex("0403020106050807090a0b0c0d0e0f10")
check_bytes("encode_guid(distinct text) 4/2/2/8 LE",
            L.encode_guid(DISTINCT_TEXT), DISTINCT_WIRE)
check("distinct text Data1 little-endian",
      L.encode_guid(DISTINCT_TEXT)[:4] == b"\x04\x03\x02\x01",
      L.encode_guid(DISTINCT_TEXT)[:4].hex())
check("distinct text Data4 copied verbatim",
      L.encode_guid(DISTINCT_TEXT)[8:] == bytes.fromhex("090a0b0c0d0e0f10"))
check("distinct text without dashes",
      L.encode_guid(DISTINCT_TEXT.replace("-", "")) == DISTINCT_WIRE)
check("distinct text uppercase",
      L.encode_guid(DISTINCT_TEXT.upper()) == DISTINCT_WIRE)
# the same 16 wire bytes given as a blob must be unchanged
check("blob form of distinct guid == text form",
      L.encode_guid(DISTINCT_WIRE) == L.encode_guid(DISTINCT_TEXT))
# padding / truncation
check("short blob zero-padded",
      L.encode_guid(b"\x01\x02") == b"\x01\x02" + bytes(14),
      L.encode_guid(b"\x01\x02").hex())
check("long blob truncated",
      L.encode_guid(bytes(range(20))) == bytes(range(16)),
      L.encode_guid(bytes(range(20))).hex())
check("empty blob -> 16 zero bytes", L.encode_guid(b"") == bytes(16))
# junk / invalid text falls back to the documented default
for junk in (object(), 3.5, -1, "not-a-guid", "zzzzzzzz-0000-0000-0000-000000000000",
             "01020304-0506-0708-090a", b"\x00" * 32, {"a": 1}, []):
    try:
        got = L.encode_guid(junk)
    except Exception as exc:  # noqa: BLE001
        got = "raised %s" % type(exc).__name__
    check("encode_guid(%r) -> 16 B default" % (junk,),
          isinstance(got, bytes) and len(got) == 16,
          repr(got) if not isinstance(got, bytes) else got.hex())
# decode_guid is the inverse of the text form
check("decode_guid(default blob)", L.decode_guid(GUID_DEF) == GUID_TEXT,
      L.decode_guid(GUID_DEF))
check("decode_guid(distinct blob)", L.decode_guid(DISTINCT_WIRE) == DISTINCT_TEXT,
      L.decode_guid(DISTINCT_WIRE))
check("decode_guid(no arg)", L.decode_guid() == GUID_TEXT)
check("decode->encode round trip",
      L.encode_guid(L.decode_guid(DISTINCT_WIRE)) == DISTINCT_WIRE)
# and the guid reaches the payload at the right offset, unmodified
pn = L.login_with_token(guid=DISTINCT_WIRE)
check_bytes("LoginWithToken custom GUID at +%d" % n, pn[n:n + 16], DISTINCT_WIRE)
check("LoginWithToken custom GUID text == blob",
      L.login_with_token(guid=DISTINCT_TEXT) == pn)
check("LoginWithS2SToken custom GUID",
      L.login_with_s2s_token(guid=DISTINCT_TEXT)[n2:n2 + 16] == DISTINCT_WIRE)

print("== parameters: name / ok / player_id ==")
pname = L.login_with_token(name="mimic-session")
check("custom name String length == 14 (INCL NUL)",
      struct.unpack_from("<H", pname, n + 16)[0] == 14,
      "%d" % struct.unpack_from("<H", pname, n + 16)[0])
check_bytes("custom name bytes", pname[n + 18:n + 31], b"mimic-session")
check("custom name total length", len(pname) == n + 16 + 2 + 14 + 1)
check("custom name then bool", pname[-1] == 0x01)
check("name as bytes", L.login_with_token(name=b"Player") == EXPECTED["LoginWithToken_V1"])
check("name=None -> default", L.login_with_token(name=None) == EXPECTED["LoginWithToken_V1"])
check("name='' -> empty String", L.login_with_token(name="")[n + 16:n + 19] == b"\x01\x00\x00")
check("empty name payload shorter",
      len(L.login_with_token(name="")) == n + 16 + 2 + 1 + 1)
for ok_val, want in ((True, b"\x01"), (1, b"\x01"), ("yes", b"\x01"),
                     (False, b"\x00"), (0, b"\x00"), ("", b"\x00"), (None, b"\x01")):
    check("ok=%r -> %s" % (ok_val, want.hex()),
          L.login_with_token(ok=ok_val)[-1:] == want,
          L.login_with_token(ok=ok_val)[-1:].hex())
check("ok affects S2S too", L.login_with_s2s_token(ok=False)[-1:] == b"\x00")
for pid, want in ((0, b"\x00\x00\x00\x00"),
                  (0x11223344, b"\x44\x33\x22\x11"),
                  (2 ** 32 + 7, b"\x07\x00\x00\x00"),
                  (-1, b"\xff\xff\xff\xff"),
                  (None, b"\x01\x00\x00\x00"),
                  ("5", b"\x05\x00\x00\x00"),
                  (object(), b"\x01\x00\x00\x00")):
    got = L.register(player_id=pid)[nr:nr + 4]
    check("player_id=%r -> %s" % (pid, want.hex()), got == want, got.hex())
check("register custom name",
      L.register(name="Bob")[nr + 4:] == qs("Bob"),
      L.register(name="Bob")[nr + 4:].hex())

print("== totality / purity ==")
for m in METHODS:
    fn = BUILDERS[m]
    junk_calls = [
        (),
        (object(),),
        ((object(),),),
        ({},),
    ]
    for args in junk_calls:
        try:
            out = fn(*args)
        except Exception as exc:  # noqa: BLE001
            out = "raised %s: %s" % (type(exc).__name__, exc)
        check("%s total on args %r" % (m, args),
              isinstance(out, bytes) and out.startswith(nb(PROTO, m)[:2]),
              repr(out)[:80])
try:
    long_out = L.login_with_token(name="A" * 70000)
    declared = struct.unpack_from("<H", long_out, n + 16)[0]
    check("over-long name clipped to u16",
          declared == 0xFFFF and len(long_out) == n + 16 + 2 + 0xFFFF + 1,
          "declared=%d len=%d" % (declared, len(long_out)))
except Exception as exc:  # noqa: BLE001
    check("over-long name clipped to u16", False, "raised %r" % (exc,))
try:
    uni = L.login_with_token(name="P\u00e4yer")
    check("non-ASCII name does not raise", isinstance(uni, bytes) and len(uni) > n)
except Exception as exc:  # noqa: BLE001
    check("non-ASCII name does not raise", False, "raised %r" % (exc,))
# purity: repeated calls are identical and no module constant is mutated
check("builder is deterministic (token)",
      L.login_with_token() == L.login_with_token())
check("builder is deterministic (register)", L.register() == L.register())
check("DEFAULT_GUID_BYTES unchanged after calls",
      L.DEFAULT_GUID_BYTES.hex() == GUID_DEF.hex(), L.DEFAULT_GUID_BYTES.hex())
check("METHOD_NAMES unchanged after calls", len(L.METHOD_NAMES) == 5)
check("METHODS unchanged after calls", tuple(L.METHODS) == tuple(METHODS))
check("DEFAULT_GUID text unchanged after calls", L.DEFAULT_GUID == GUID_TEXT)
check("a mutable input blob is not retained by the module",
      (lambda: (lambda b: (L.encode_guid(b), b.__setitem__(0, 0xFF),
                           L.DEFAULT_GUID_BYTES)[2])(bytearray(GUID_DEF)))() == GUID_DEF)

print("== dispatch: resolve_method / build ==")
for m in METHODS:
    bare = m[:-3]                       # strip "_V1"
    for text in (m, bare, "%s::%s" % (PROTO, m), "%s::%s*" % (PROTO, m),
                 "%s::%s*\x00" % (PROTO, m), m.lower(), m.upper()):
        check("resolve_method(%r) -> %s" % (text, m), L.resolve_method(text) == m,
              repr(L.resolve_method(text)))
    check("build via bare name %s" % bare, L.build(PROTO, bare) == EXPECTED[m])
    check("build via qualified name %s" % m,
          L.build(PROTO, "%s::%s*" % (PROTO, m)) == EXPECTED[m])
    check("build via bytes name %s" % m,
          L.build(PROTO, m.encode("ascii")) == EXPECTED[m])
    check("build ignores junk kwargs %s" % m,
          L.build(PROTO, m, junk=1, proto2="x") == EXPECTED[m])
for junk in (None, 123, "", "Nope_V1", b"\x00\x01", object(), ["x"],
             "LoginProtocol::Unknown_V1*"):
    try:
        out = L.build(PROTO, junk)
    except Exception as exc:  # noqa: BLE001
        out = "raised %s" % type(exc).__name__
    check("build(%r) -> None" % (junk,), out is None, repr(out))

print("== package registration ==")
check("rv_shapes.login is a module", getattr(rv_shapes, "login", None) is L,
      type(getattr(rv_shapes, "login", None)).__name__)
check("_PROTO_MODULE['loginprotocol'] == 'login'",
      rv_shapes._PROTO_MODULE.get("loginprotocol") == "login",
      repr(rv_shapes._PROTO_MODULE.get("loginprotocol")))
check("rv_shapes.available() still imports backend/profile/voting",
      all(rv_shapes.available().get(k) == "ok" for k in ("backend", "profile", "voting")),
      repr(rv_shapes.available()))
check("package build(LoginProtocol, Register_V1)",
      rv_shapes.build(PROTO, "Register_V1") == EXPECTED["Register_V1"])
check("package build is case-insensitive on the proto key",
      rv_shapes.build("loginprotocol", "Register_V1") is not None)
check("package build unknown method -> None",
      rv_shapes.build(PROTO, "GetVersion_V1") is None)
check("package build unknown proto -> None",
      rv_shapes.build("NopeProtocol", "Register_V1") is None)
check("no cross-talk with the profile protocol",
      rv_shapes.build("PlayerProfileServiceProtocol", "LoginWithToken_V1") is None)
check("build defaults to the documented proto name",
      L.build("", "Register_V1") == EXPECTED["Register_V1"],
      (L.build("", "Register_V1") or b"").hex())
check("build normalises a 'Proto::Method' proto argument",
      L.build("LoginProtocol::Register_V1", "Register_V1") == EXPECTED["Register_V1"])

print("== equality with the live rvlogin fallback (mimic_server.rv_rmc_reply) ==")


def fallback_payload(short):
    """Verbatim re-implementation of the ``elif mode == "rvlogin"`` chain.

    Copied from server/mimic_server.py (the branch reached when
    ``rv_shapes.build`` returns None) so this test still passes if the module
    is removed: it is the behaviour we must reproduce byte-for-byte.
    """
    nmb = (PROTO + "::" + short + "*").encode("ascii") + b"\x00"
    out = struct.pack("<H", len(nmb)) + nmb
    if short in ("LoginWithToken_V1", "LoginWithS2SToken_V1"):
        out += bytes.fromhex("000000000000000000000000000000aa")   # GUID
        txt = b"Player"
        out += struct.pack("<H", len(txt) + 1) + txt + b"\x00"      # String (incl NUL)
        out += b"\x01"                                             # bool
    elif short == "Register_V1":
        out += struct.pack("<I", 1)                                 # u32
        txt = b"Player"
        out += struct.pack("<H", len(txt) + 1) + txt + b"\x00"      # String (incl NUL)
    elif short in ("RedirectLogin_V1", "RenewWithTicket_V1"):
        pass                                                        # no fields
    return out


for m in METHODS:
    fb = fallback_payload(m)
    check("fallback fixture %s == expected" % m, fb == EXPECTED[m], fb.hex())
    check("fallback fixture %s == builder" % m, fb == BUILDERS[m](), fb.hex())
    check("fallback fixture %s == build()" % m, fb == L.build(PROTO, m))
    check("fallback fixture %s == rv_shapes.build()" % m,
          fb == rv_shapes.build(PROTO, m))
    check("fallback fixture %s length" % m, len(fb) == len(BUILDERS[m]()))

# The documented LoginWithToken payload prefix (loginpayload.md §4, last line):
DOC_TOKEN_PREFIX = (
    "22004c6f67696e50726f746f636f6c3a3a4c6f67696e57697468546f6b656e5f56312a00"
)
check("LoginWithToken payload starts with the documented 36-B name block",
      L.login_with_token().hex().startswith(DOC_TOKEN_PREFIX),
      L.login_with_token()[:36].hex())
check_bytes("documented name block", L.login_with_token()[:36],
            bytes.fromhex(DOC_TOKEN_PREFIX))
check("documented 0x22 length byte",
      L.login_with_token()[0] == 0x22, "0x%02x" % L.login_with_token()[0])
check("documented '*' at offset 34",
      L.login_with_token()[34] == 0x2A, "0x%02x" % L.login_with_token()[34])

print("== envelope composition (rmcsuccess.md §1: status 1, then the payload) ==")
for m in METHODS:
    payload = L.build(PROTO, m)
    proto = PROTO.encode("ascii") + b"\x00"
    call_id = 4
    inner = (struct.pack("<H", len(proto)) + proto
             + bytes([0])                      # type 0 = response
             + bytes([1])                      # status 1 = clean, no message
             + struct.pack("<I", call_id)
             + payload)
    frame = struct.pack("<I", len(inner)) + inner
    check("%s envelope size field" % m,
          struct.unpack_from("<I", frame, 0)[0] == len(frame) - 4,
          "%d vs %d" % (struct.unpack_from("<I", frame, 0)[0], len(frame) - 4))
    check("%s envelope proto length INCL NUL" % m,
          struct.unpack_from("<H", frame, 4)[0] == 14,
          "%d" % struct.unpack_from("<H", frame, 4)[0])
    check("%s envelope type/status" % m, frame[20:22] == b"\x00\x01", frame[20:22].hex())
    check("%s envelope call id" % m,
          struct.unpack_from("<I", frame, 22)[0] == call_id)
    check("%s envelope payload offset" % m, frame[26:] == payload)
    check("%s frame length" % m, len(frame) == 26 + len(payload))

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
