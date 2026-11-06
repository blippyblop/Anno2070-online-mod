"""Wire-shape builders for ``LoginProtocol`` replies.

This is the last protocol that used to be handled by the hand-written ``elif``
chain in ``mimic_server.rv_rmc_reply`` (``RV_REPLY_MODE=rvlogin``).  Every
builder here returns the reply *payload* only -- the method name block followed
by the method's fields.  The envelope (``u32 size | u16 proto_len "Proto\\0" |
u8 type=0 | u8 status=1 | u32 call_id``) is added by the caller, exactly as for
:mod:`rv_shapes.backend` / :mod:`rv_shapes.profile`; see
``decomp/_adhoc/rmcsuccess.md`` §1 for that grammar.

Source of the field list
------------------------
``decomp/_adhoc/loginpayload.md`` §1/§2 -- decompilation + disassembly of the
client's login reply reader ``FUN_141370440`` (RVA 0x1370440, vtable 0x1649cd0
slot +0xb8; loginpayload.md header calls this out as the real reader, not the
no-op 0x8c710):

    #   method                      reply payload (after the name block)
    1   LoginWithToken_V1           16-byte GUID + String + u8 bool
    2   LoginWithS2SToken_V1        identical to #1 (same reader, same branch)
    3   Register_V1                 u32 + String
    4   RedirectLogin_V1            <void>  (asm: JNZ straight to the exit)
    5   RenewWithTicket_V1          <void>  (asm: JNZ straight to the exit)

Conventions (same file, §1/§3, plus ``decomp/batch/step1_backend/REPLIES.md``
§0 which documents the same reader helpers):

  * the method name is whatever :func:`rv_shapes.name_block` produces: a u16
    length that **INCLUDES** the trailing NUL, the literal ``"Proto::Method"``,
    then ``"*"`` and the NUL.  The asterisk is mandatory: ``FUN_141370440``
    builds ``decodedName + "*"`` (``String::operator+`` at 0x14137052a) and
    compares with ``_mbscmp`` (0x141199240) -- exact equality, no wildcard;
  * the GUID is the 4/2/2/8 Windows GUID layout (``FUN_1413714c0``): u32 Data1,
    u16 Data2, u16 Data3, u8 Data4[8], all little-endian except that Data4 is a
    plain byte array.  ``FUN_141353360`` parses GUID *text* into the same
    struct, which is why a dashed string is accepted here too;
  * a Quazal ``String`` is a u16 length that **EXCLUDES** the NUL, then the raw
    bytes, with no NUL on the wire (``FUN_141371620``).  Note the two different
    length conventions in one payload -- that is deliberate, not a bug;
  * a bool is one byte, ``!= 0`` means true (``FUN_14135e2a0``);
  * all integers are little-endian and unpadded (the client ``memcpy``s them:
    ``FUN_14135e5f0`` -> ``FUN_141367760``).

Defaults reproduce the working ``rvlogin`` fallback byte-for-byte
(``mimic_server.rv_rmc_reply``): GUID ``00000000-0000-0000-0000-0000000000aa``,
player name ``"Player"``, bool 1, and Register's u32 ``1``.  That GUID is all
zeroes except the last byte 0xAA, a deliberately non-zero marker so a log or a
hexdump shows that the field was actually filled in.

Every function is *total*: no I/O, no globals mutated, no exceptions.  Junk
arguments fall back to the documented default rather than raising, because the
client silently accepts truncated payloads (``FUN_14135e600`` copies whatever
is left and the return code is ignored -- loginpayload.md §2), so a defaulted
reply is strictly better than a broken one.
"""

import inspect
import struct

try:  # normal package import
    from . import name_block as _name_block
    from . import qstr as _qstr
except ImportError:  # pragma: no cover - flat sys.path (server/ on sys.path)
    from rv_shapes import name_block as _name_block
    from rv_shapes import qstr as _qstr

__all__ = [
    "DEFAULT_PROTO",
    "DEFAULT_GUID",
    "DEFAULT_GUID_BYTES",
    "DEFAULT_NAME",
    "DEFAULT_OK",
    "DEFAULT_PLAYER_ID",
    "METHOD_NAMES",
    "METHODS",
    "encode_guid",
    "decode_guid",
    "login_with_token",
    "login_with_s2s_token",
    "register",
    "redirect_login",
    "renew_with_ticket",
    "resolve_method",
    "build",
]

#: Only this protocol's client reader exists (vtable 0x1649cd0).
DEFAULT_PROTO = "LoginProtocol"

#: Fallback GUID as canonical text (8-4-4-4-12) and as the 16 wire bytes.
DEFAULT_GUID = "00000000-0000-0000-0000-0000000000aa"
DEFAULT_GUID_BYTES = bytes.fromhex("000000000000000000000000000000aa")

#: Fallback player name / bool (LoginWithToken, LoginWithS2SToken).
DEFAULT_NAME = "Player"
DEFAULT_OK = True

#: Fallback u32 for Register_V1.
DEFAULT_PLAYER_ID = 1

GUID_SIZE = 16
_U32_MAX = 0xFFFFFFFF
_STR_MAX = 0xFFFF          # Quazal String length is a u16
_HEX_DIGITS = set("0123456789abcdefABCDEF")


# --------------------------------------------------------------------------
# coercion helpers -- total on any input, never raise
# --------------------------------------------------------------------------
def _proto_name(proto):
    """Return a usable protocol name (falls back to the documented default)."""
    if isinstance(proto, (bytes, bytearray, memoryview)):
        try:
            proto = bytes(proto).decode("ascii")
        except Exception:
            return DEFAULT_PROTO
    if not isinstance(proto, str):
        return DEFAULT_PROTO
    text = proto.replace("\x00", "").strip().strip("*").strip()
    if "::" in text:                 # tolerate "LoginProtocol::Method"
        text = text.split("::")[0].strip()
    return text or DEFAULT_PROTO


def _name_bytes(name):
    """Player name as ASCII bytes; falls back to DEFAULT_NAME on junk."""
    if isinstance(name, (bytes, bytearray, memoryview)):
        raw = bytes(name)
    elif isinstance(name, str):
        try:
            raw = name.encode("ascii")
        except Exception:
            raw = name.encode("ascii", "replace")
    elif name is None:
        return DEFAULT_NAME.encode("ascii")
    else:
        try:
            raw = str(name).encode("ascii", "replace")
        except Exception:            # pragma: no cover - defensive
            return DEFAULT_NAME.encode("ascii")
    return raw[:_STR_MAX]


def _bool_byte(ok):
    """One byte: 0x01 for true, 0x00 for false (``FUN_14135e2a0``).

    Plain truthiness, except that ``None`` means "not specified" and therefore
    falls back to :data:`DEFAULT_OK` -- consistent with how ``guid=None`` /
    ``name=None`` / ``player_id=None`` select their defaults.
    """
    if ok is None:
        ok = DEFAULT_OK
    return b"\x01" if ok else b"\x00"


def _u32_value(value, default):
    """Coerce to an unsigned 32-bit integer, masking like the client's u32."""
    if value is None:
        return default & _U32_MAX
    try:
        return int(value) & _U32_MAX
    except Exception:
        return default & _U32_MAX


def _fit16(raw):
    """Exactly 16 bytes: zero-padded when short, truncated when long."""
    return raw[:GUID_SIZE].ljust(GUID_SIZE, b"\x00")


# --------------------------------------------------------------------------
# GUID: 4/2/2/8 Windows layout (u32 Data1, u16 Data2, u16 Data3, u8 Data4[8])
# --------------------------------------------------------------------------
def _pack_guid(data1, data2, data3, data4):
    """u32 Data1 | u16 Data2 | u16 Data3 | 8 x u8, all little-endian fields.

    This is the layout ``FUN_1413714c0`` reads (loginpayload.md §2: reads at
    +0x38(4), +0x34(2), +0x30(2), then four 2-byte reads into Data4) and the
    one ``FUN_141353340`` stores.
    """
    return struct.pack(
        "<IHH8s",
        data1 & _U32_MAX,
        data2 & 0xFFFF,
        data3 & 0xFFFF,
        bytes(data4)[:8].ljust(8, b"\x00"),
    )


def _guid_from_text(text):
    """Canonical GUID text -> 16 wire bytes, or None if it is not a GUID.

    ``FUN_141353360`` parses GUID text with the charset
    ``"0123456789abcdfeABCDEF-"`` into the same 4/2/2/8 struct, i.e. the first
    group is the *value* of Data1 (0x01020304), which then goes on the wire
    little-endian as ``04 03 02 01``.  Braces and spaces are tolerated, and the
    dashes may be omitted because only the hex digits matter.
    """
    body = text.strip().strip("{}").replace("-", "").replace("_", "")
    body = "".join(body.split())
    if body[:2].lower() == "0x":
        body = body[2:]
    if len(body) != 32 or any(c not in _HEX_DIGITS for c in body):
        return None
    try:
        return _pack_guid(
            int(body[0:8], 16),
            int(body[8:12], 16),
            int(body[12:16], 16),
            bytes.fromhex(body[16:32]),
        )
    except Exception:                 # pragma: no cover - defensive
        return None


def encode_guid(guid=None):
    """Return the 16 wire bytes for a GUID.

    Accepts, in order of preference:

      * ``None`` / junk        -> ``DEFAULT_GUID_BYTES``
      * 16-byte blob           -> used verbatim (a truncated blob is zero-padded
                                 and an over-long one truncated: the client
                                 reads a fixed 16 bytes, never a length)
      * sequence of 16 ints    -> one byte each
      * ``"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"`` (canonical text, braces and
        missing dashes tolerated) -> 4/2/2/8 struct packing, see
        :func:`_guid_from_text`.
    """
    if guid is None:
        return DEFAULT_GUID_BYTES
    if isinstance(guid, (bytes, bytearray, memoryview)):
        return _fit16(bytes(guid))
    if isinstance(guid, (list, tuple)):
        try:
            return bytes(bytearray(int(v) & 0xFF for v in guid))[:GUID_SIZE].ljust(
                GUID_SIZE, b"\x00"
            )
        except Exception:
            return DEFAULT_GUID_BYTES
    if isinstance(guid, str):
        parsed = _guid_from_text(guid)
        return parsed if parsed is not None else DEFAULT_GUID_BYTES
    return DEFAULT_GUID_BYTES


def decode_guid(blob=None):
    """16 wire bytes -> canonical GUID text (inverse of the string form)."""
    data1, data2, data3, data4 = struct.unpack("<IHH8s", encode_guid(blob))
    tail = data4.hex()
    return "%08x-%04x-%04x-%s-%s" % (data1, data2, data3, tail[:4], tail[4:])


# --------------------------------------------------------------------------
# builders -- payload = name block + fields (no envelope, no call id)
# --------------------------------------------------------------------------
def login_with_token(guid=None, name=DEFAULT_NAME, ok=DEFAULT_OK, proto=DEFAULT_PROTO):
    """``LoginWithToken_V1``: 16-byte GUID + String + u8 bool.

    Reader: ``FUN_141370440`` matches the name and falls into 0x141370695,
    which reads GUID (``FUN_1413714c0``), String (``FUN_1413715c0`` ->
    ``FUN_141371620``) and bool (``FUN_14135e2a0``) in that order, nothing
    else (loginpayload.md §2).
    """
    return (
        _name_block(_proto_name(proto), "LoginWithToken_V1")
        + encode_guid(guid)
        + _qstr(_name_bytes(name))
        + _bool_byte(ok)
    )


def login_with_s2s_token(
    guid=None, name=DEFAULT_NAME, ok=DEFAULT_OK, proto=DEFAULT_PROTO
):
    """``LoginWithS2SToken_V1``: same field set as :func:`login_with_token`.

    loginpayload.md §1: the second decoded name jumps to the *same* field
    reader (0x141370695), so the bytes are identical apart from the name block.
    """
    return (
        _name_block(_proto_name(proto), "LoginWithS2SToken_V1")
        + encode_guid(guid)
        + _qstr(_name_bytes(name))
        + _bool_byte(ok)
    )


def register(player_id=DEFAULT_PLAYER_ID, name=DEFAULT_NAME, proto=DEFAULT_PROTO):
    """``Register_V1``: u32 + String (loginpayload.md §1, reader 0x141370965)."""
    return (
        _name_block(_proto_name(proto), "Register_V1")
        + struct.pack("<I", _u32_value(player_id, DEFAULT_PLAYER_ID))
        + _qstr(_name_bytes(name))
    )


def redirect_login(proto=DEFAULT_PROTO):
    """``RedirectLogin_V1``: no fields -- the name block is the whole payload.

    loginpayload.md §1: the match jumps straight to 0x141370a96 (the exit).
    """
    return _name_block(_proto_name(proto), "RedirectLogin_V1")


def renew_with_ticket(proto=DEFAULT_PROTO):
    """``RenewWithTicket_V1``: no fields (reader ``FUN_14136f570``, same exit)."""
    return _name_block(_proto_name(proto), "RenewWithTicket_V1")


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
#: method -> human-readable reply layout (also documents the reader branch).
METHOD_NAMES = {
    "LoginWithToken_V1": "GUID(16) + String + u8 bool",
    "LoginWithS2SToken_V1": "GUID(16) + String + u8 bool",
    "Register_V1": "u32 + String",
    "RedirectLogin_V1": "void",
    "RenewWithTicket_V1": "void",
}

#: The five methods, in the client's own dispatch order (loginpayload.md §1).
METHODS = tuple(METHOD_NAMES)

_BUILDERS = {
    "LoginWithToken_V1": login_with_token,
    "LoginWithS2SToken_V1": login_with_s2s_token,
    "Register_V1": register,
    "RedirectLogin_V1": redirect_login,
    "RenewWithTicket_V1": renew_with_ticket,
}

_RESOLVE = {k.lower(): k for k in METHOD_NAMES}


def resolve_method(method):
    """Map a loosely-specified method name to a key of :data:`METHOD_NAMES`.

    Accepts ``"Register_V1"``, ``"Register"``, ``"LoginProtocol::Register_V1"``
    and the on-wire ``"..._V1*"`` form; matching is case-insensitive because
    this is an operator-facing helper, while the bytes on the wire keep the
    client's exact spelling (``_mbscmp`` is case-sensitive).
    """
    if isinstance(method, (bytes, bytearray, memoryview)):
        try:
            method = bytes(method).decode("ascii")
        except Exception:
            return None
    if not isinstance(method, str):
        return None
    text = method.replace("\x00", "").strip().strip("*").strip()
    if "::" in text:
        text = text.split("::")[-1].strip()
    key = _RESOLVE.get(text.lower())
    if key is not None:
        return key
    if not text.lower().endswith("_v1"):
        return _RESOLVE.get((text + "_V1").lower())
    return None


def build(proto, method, **kw):
    """Complete payload (name block + fields) for ``proto::method``.

    Returns ``None`` when the method is not one of the five handled here, so
    ``rv_shapes.build`` can fall through to "name block only" for everything
    else.  Unknown keyword arguments are ignored rather than raising; junk
    values are coerced by the individual builders.
    """
    key = resolve_method(method)
    if key is None:
        return None
    func = _BUILDERS[key]
    proto_name = _proto_name(proto)
    try:
        allowed = set(inspect.signature(func).parameters) - {"proto"}
        body = func(proto=proto_name, **{k: v for k, v in kw.items() if k in allowed})
    except Exception:                 # pragma: no cover - defensive
        return None
    if not isinstance(body, (bytes, bytearray)):
        return None
    return bytes(body)
