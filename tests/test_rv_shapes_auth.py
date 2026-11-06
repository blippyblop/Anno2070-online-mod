#!/usr/bin/env python3
"""Shape tests for ``server/rv_shapes/auth.py`` (Quazal Authentication, 0x0A).

Plain python, no pytest::

    source ~/tools/env.sh && python3 tools/test_rv_shapes_auth.py

Exit code 0 means every check passed.  Every expected byte is re-derived here
with ``struct`` / ``hashlib`` / ``hmac`` directly -- the module's own helpers
are deliberately *not* reused for the expected values, so a bug in
``rv_shapes.name_block`` / ``qstr`` / ``blob`` / ``derive_key`` shows up as a
failure instead of agreeing with itself.

What is checked
---------------
* the module's own *finding* (``PRESENT_IN_ANNO is False``, the protocol table
  has ten entries and none of them is Authentication/0x0A) -- a regression
  guard: if somebody later "fixes" auth.py by inventing an Anno shape, this
  fails;
* the name block of all three methods: ``u16 length INCLUDING the NUL``, then
  ``"AuthenticationProtocol::<M>"`` + ``"*"`` + NUL (the client's ``_mbscmp``
  oracle compares against name+``*``);
* ``Login``/``LoginEx``: ``u32 result | u32 pid | Buffer ticket |
  RVConnectionData(String url | u32 0 | String "") | String tail`` -- field by
  field, offset by offset;
* ``RequestTicket``: ``u32 result | Buffer ticket`` and nothing else;
* a Quazal ``Buffer`` is ``u32 len`` + bytes, contrasted with the Quazal
  ``String`` used right next to it (``u16 len`` EXCLUDING the NUL);
* the ticket: ``RC4(derive_key(pid, "UbiDummyPwd"),
  session_key[16] | u32 server_pid | u32 len | inner) + HMAC-MD5`` -- rebuilt
  from scratch here and compared byte for byte, plus a round-trip decode;
* ``derive_key`` is the reference's ``65000 + pid % 1024`` MD5 iteration chain;
* totality (junk arguments never raise), purity (module constants untouched),
  ``resolve_method`` / ``build`` dispatch and the package-level fallback
  (``auth`` must NOT be registered in ``rv_shapes.build`` yet -- the human
  registers modules).
"""

import hashlib
import hmac
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import rv_shapes  # noqa: E402
from rv_shapes import auth as A  # noqa: E402

VERBOSE = bool(os.environ.get("RV_TEST_VERBOSE"))
PROTO = "AuthenticationProtocol"

CHECKS = 0
FAILURES = []


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if ok:
        if VERBOSE:
            print("  ok   %s" % label)
    else:
        FAILURES.append(label)
        print("  FAIL %s %s" % (label, detail))


# --------------------------------------------------------------------------
# independent re-implementations (the oracle)
# --------------------------------------------------------------------------
def ref_name_block(proto, method):
    nm = (proto + "::" + method + "*").encode("ascii") + b"\x00"
    return struct.pack("<H", len(nm)) + nm


def ref_qstr(text):
    raw = text.encode("ascii") if isinstance(text, str) else bytes(text)
    return struct.pack("<H", len(raw)) + raw


def ref_blob(data):
    return struct.pack("<I", len(data)) + bytes(data)


def ref_rc4(key, data):
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(b ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def ref_derive_key(pid, password="UbiDummyPwd"):
    buf = password.encode("ascii")
    for _ in range(65000 + pid % 1024):
        buf = hashlib.md5(buf).digest()
    return buf


def ref_ticket(pid, server_pid, session_key, inner, password="UbiDummyPwd"):
    key = ref_derive_key(pid, password)
    plain = (session_key + struct.pack("<I", server_pid)
             + struct.pack("<I", len(inner)) + inner)
    enc = ref_rc4(key, plain)
    return enc + hmac.new(key, enc, hashlib.md5).digest()


INNER = bytes.fromhex(
    "76214BA62196D3F39A8C7A270DD9B3FA210EEDAF42639295C1165408EE6E691735782E6E"
)


# --------------------------------------------------------------------------
# 0. the finding itself
# --------------------------------------------------------------------------
print("[0] the 0x0A finding")
check("PRESENT_IN_ANNO is False", A.PRESENT_IN_ANNO is False,
      repr(A.PRESENT_IN_ANNO))
check("protocol table has 10 entries", len(A.PROTOCOL_TABLE) == 10,
      str(len(A.PROTOCOL_TABLE)))
check("no Authentication client class",
      not any("Authentication" in row[0] or "Authentication" in row[1]
              for row in A.PROTOCOL_TABLE))
check("no SecureProtocol client class",
      not any("SecureProtocol" in row[1] for row in A.PROTOCOL_TABLE))
check("LoginProtocol present",
      any(row[1] == "LoginProtocol" for row in A.PROTOCOL_TABLE))
check("every vtable is 8-byte aligned and in .rdata",
      all(row[2] % 8 == 0 and 0x1582000 <= row[2] < 0x1A1D000
          for row in A.PROTOCOL_TABLE))
check("PROVEN_METHODS is empty (nothing provable against Anno)",
      A.PROVEN_METHODS == {}, repr(A.PROVEN_METHODS))
check("METHODS is the reference's three", A.METHODS == ("Login", "LoginEx",
                                                        "RequestTicket"),
      repr(A.METHODS))
check("UNKNOWN_METHODS is a dict", isinstance(A.UNKNOWN_METHODS, dict))

# --------------------------------------------------------------------------
# 1. name blocks
# --------------------------------------------------------------------------
print("[1] name blocks")
for m in A.METHODS:
    payload = A.build(PROTO, m)
    check("%s: build() returns bytes" % m, isinstance(payload, (bytes, bytearray)))
    payload = bytes(payload)
    nb = ref_name_block(PROTO, m)
    nlen = struct.unpack_from("<H", payload, 0)[0]
    check("%s: u16 length INCLUDES the NUL" % m,
          nlen == len(PROTO) + 2 + len(m) + 1 + 1, "%d" % nlen)
    check("%s: name bytes + trailing '*' + NUL" % m, payload[:len(nb)] == nb,
          payload[:len(nb)].hex())
    check("%s: name ends with '*' NUL" % m,
          payload[:len(nb)].endswith(b"*\x00"))
    check("%s: independent re-derivation agrees" % m,
          payload == ref_name_block(PROTO, m) + payload[len(nb):])

# --------------------------------------------------------------------------
# 2. Login / LoginEx layout
# --------------------------------------------------------------------------
print("[2] Login / LoginEx layout")
TICKET = bytes(range(0x30, 0x30 + 40))          # 40 bytes, easy to spot
URL = "prudps:/address=127.0.0.1;port=5005;CID=1;PID=2;sid=1;stream=3;type=2"
for m, tail in (("Login", "build-name"), ("LoginEx", "return-message")):
    payload = bytes(A.build(PROTO, m, pid=0x11223344, ticket=TICKET,
                            url=URL, **({"build_name": tail}
                                        if m == "Login" else {"message": tail})))
    nb = ref_name_block(PROTO, m)
    off = len(nb)
    check("%s: result u32 == 0 at +%d" % (m, off),
          struct.unpack_from("<I", payload, off)[0] == 0)
    off += 4
    check("%s: pid u32 LE at +%d" % (m, off),
          struct.unpack_from("<I", payload, off)[0] == 0x11223344,
          hex(struct.unpack_from("<I", payload, off)[0]))
    off += 4
    check("%s: Buffer length is a u32 at +%d" % (m, off),
          struct.unpack_from("<I", payload, off)[0] == len(TICKET))
    off += 4
    check("%s: ticket bytes verbatim at +%d" % (m, off),
          payload[off:off + len(TICKET)] == TICKET)
    off += len(TICKET)
    check("%s: RVConnectionData url is a String (u16 EXCL NUL)" % m,
          struct.unpack_from("<H", payload, off)[0] == len(URL),
          "%d" % struct.unpack_from("<H", payload, off)[0])
    off += 2
    check("%s: url bytes have no NUL terminator" % m,
          payload[off:off + len(URL)] == URL.encode("ascii"))
    off += len(URL)
    check("%s: special-protocol u32 == 0" % m,
          struct.unpack_from("<I", payload, off)[0] == 0)
    off += 4
    check("%s: empty special-protocol String (u16 0)" % m,
          struct.unpack_from("<H", payload, off)[0] == 0)
    off += 2
    check("%s: tail String length" % m,
          struct.unpack_from("<H", payload, off)[0] == len(tail))
    off += 2
    check("%s: tail bytes" % m, payload[off:off + len(tail)] == tail.encode())
    off += len(tail)
    check("%s: no trailing bytes (total %d)" % (m, off), off == len(payload),
          "%d vs %d" % (off, len(payload)))

# --------------------------------------------------------------------------
# 3. RequestTicket layout
# --------------------------------------------------------------------------
print("[3] RequestTicket layout")
payload = bytes(A.build(PROTO, "RequestTicket", ticket=TICKET))
nb = ref_name_block(PROTO, "RequestTicket")
check("RequestTicket: result u32 == 0", struct.unpack_from("<I", payload, len(nb))[0] == 0)
check("RequestTicket: Buffer length u32",
      struct.unpack_from("<I", payload, len(nb) + 4)[0] == len(TICKET))
check("RequestTicket: ticket verbatim", payload[len(nb) + 8:] == TICKET)
check("RequestTicket: no pid, no URL",
      len(payload) == len(nb) + 8 + len(TICKET))

# --------------------------------------------------------------------------
# 4. the ticket
# --------------------------------------------------------------------------
print("[4] Kerberos ticket")
SESSION = bytes(range(16))
built = A.build_ticket(0x1234, 2, SESSION)
expect = ref_ticket(0x1234, 2, SESSION, INNER)
check("build_ticket matches the independent RC4/HMAC construction", built == expect)
want_len = 16 + 4 + 4 + len(INNER) + 16
check("ticket length = 16 + 4 + 4 + %d + 16 = %d" % (len(INNER), want_len),
      len(built) == want_len, str(len(built)))
check("ticket tail (last 16) == HMAC-MD5", built[-16:] == expect[-16:])
sk, spid, inner = A.open_ticket(built, 0x1234)
check("open_ticket round-trips the session key", sk == SESSION, sk.hex())
check("open_ticket round-trips the server pid", spid == 2, str(spid))
check("open_ticket round-trips the inner blob", inner == INNER)
try:
    A.open_ticket(built[:-1] + bytes([built[-1] ^ 1]), 0x1234)
    check("open_ticket rejects a bad MAC", False)
except ValueError:
    check("open_ticket rejects a bad MAC", True)
def _rejects(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


check("rejects a ticket under the wrong pid",
      _rejects(lambda: A.open_ticket(built, 0x1235)))
check("derive_key is the 65000 + pid%1024 MD5 chain",
      A.derive_key(0x40) == ref_derive_key(0x40))
check("derive_key(0) == 65000 iterations",
      len(A.derive_key(0)) == 16)
check("derive_key is cached (same object)", A.derive_key(7) is A.derive_key(7))
check("build_ticket default session key is 16 zero bytes",
      A.build_ticket(1, 2, b"") == ref_ticket(1, 2, b"\x00" * 16, INNER))
check("DEFAULT_INNER_TICKET untouched",
      A.DEFAULT_INNER_TICKET == INNER, A.DEFAULT_INNER_TICKET.hex())
check("DEFAULT_INNER_TICKET is 36 bytes", len(A.DEFAULT_INNER_TICKET) == 36)

# --------------------------------------------------------------------------
# 5. secure_url / connection_data
# --------------------------------------------------------------------------
print("[5] secure_url / RVConnectionData")
url = A.secure_url("10.1.2.3", 5005, 2)
check("secure_url scheme", url.startswith("prudps:/address=10.1.2.3;port=5005;"),
      url)
check("secure_url carries stream=3 and type=2",
      ";stream=3;type=2" in url, url)
check("secure_url carries CID=1;PID=2", "CID=1;PID=2" in url, url)
check("connection_data = String + u32 0 + empty String",
      A.connection_data("X") == ref_qstr("X") + struct.pack("<I", 0)
      + struct.pack("<H", 0))

# --------------------------------------------------------------------------
# 6. beta checks: totality, purity, dispatch
# --------------------------------------------------------------------------
print("[6] totals, purity, dispatch")
JUNK = [None, object(), 3.5, b"\xff\xfe", -1, 1 << 80, "x" * 70000]
ok_all = True
for m in A.METHODS:
    for j in JUNK:
        try:
            out = A.build(PROTO, m, pid=j, ticket=j, url=j, session_key=j,
                          server_id=j, password=j)
            if not isinstance(out, bytes):
                ok_all = False
        except Exception:
            ok_all = False
check("every builder is total on junk arguments", ok_all)
check("unknown method -> None", A.build(PROTO, "NoSuchMethod_V1") is None)
check("unknown proto still builds (proto is only a name)",
      isinstance(A.build("Whatever", "Login"), bytes))
check("resolve_method accepts bare and prefixed names",
      A.resolve_method("login") == "Login"
      and A.resolve_method("AuthenticationProtocol::RequestTicket*") == "RequestTicket")
check("resolve_method rejects junk", A.resolve_method("") is None
      and A.resolve_method(None) is None)
check("session key is truncated/padded to 16",
      len(A.build_ticket(1, 2, b"AB")) == want_len)
check("blob() = u32 len + bytes",
      A.blob(b"abc") == b"\x03\x00\x00\x00abc")
check("blob(b'') = four zero bytes", A.blob(b"") == b"\x00\x00\x00\x00")

# --------------------------------------------------------------------------
# 7. envelope integration (the frame the client's reader expects)
# --------------------------------------------------------------------------
print("[7] envelope integration")
payload = bytes(A.build(PROTO, "RequestTicket", ticket=TICKET))
call_id = 7
inner = (struct.pack("<H", len(PROTO) + 1) + PROTO.encode() + b"\x00"
         + b"\x00" + b"\x01" + struct.pack("<I", call_id) + payload)
frame = struct.pack("<I", len(inner)) + inner
check("envelope size field", struct.unpack_from("<I", frame, 0)[0] == len(frame) - 4)
check("envelope proto length INCL NUL",
      struct.unpack_from("<H", frame, 4)[0] == len(PROTO) + 1)
_plen = len(PROTO) + 1                      # the u16 INCLUDES the NUL
check("envelope type/status", frame[6 + _plen:6 + _plen + 2] == b"\x00\x01",
      frame[6 + _plen:6 + _plen + 2].hex())
check("envelope payload reproduces the builder output", frame[-(len(payload)):] == payload)

# --------------------------------------------------------------------------
# 8. package registration is the human's job (do not sneak in)
# --------------------------------------------------------------------------
print("[8] package registration")
check("rv_shapes does not route 'AuthenticationProtocol' yet",
      rv_shapes.build("AuthenticationProtocol", "Login") is None)

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
