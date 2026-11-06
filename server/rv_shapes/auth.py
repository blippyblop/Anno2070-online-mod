"""Wire-shape builders for the Quazal **Authentication** service (protocol 0x0A).

Read this first
---------------
**Anno 2070's client does not speak protocol 0x0A, and neither does anything
else in Anno5.exe.**  The evidence is in `decomp/_adhoc/auth-protocol.md`; the
short version, all measured on ``Anno 2070/Anno5.exe`` 3.0.8051 (image base
``0x140000000``, RVAs below):

* The RTTI table contains **no** ``AuthenticationProtocolClient``,
  ``AuthenticationService`` or ``SecureProtocolClient`` class -- the only
  ``ProtocolClient`` classes in the image are the ten listed in
  :data:`PROTOCOL_TABLE` (BackEnd, PlayerProfile, Voting, WorldEvent, Chat,
  Mail, Friends, MatchMaking, LoadTest, Login).
* The strings ``AuthenticationProtocol``, ``AuthenticationService``,
  ``RequestTicket``, ``LoginEx``, ``Kerberos`` and ``UbiAuthenticationLoginCustomData``
  do **not** occur anywhere in the file (the Quazal method names in this build
  are XOR-obfuscated, but the *class* names of RTTI are not, and the ubiservices
  "Authentication" strings that do exist are all HTTP/SDK ones).
* RV 3.38 in this build never puts a numeric protocol id on the wire at all:
  a request is built by ``FUN_14136f9d0`` (0x136f9d0), which writes the
  protocol *name* through ``FUN_141371ef0`` (0x1371ef0) -> ``FUN_141346b80``
  (0x1346b80) -> ``FUN_14135e510`` (0x135e510): ``u16 (strlen+1)``, then the
  name and its NUL, then a ``u8`` type byte (1 = call).  Replies are routed
  back by name too (``FUN_14136ebd0`` 0x136ebd0 -> ``FUN_1413471f0``
  0x13471f0 ``GetProtocol(name)`` -> ``FUN_1413718e0``).  There is no
  id -> class table to answer, only a name -> class one.
* The secure PRUDP stream (URL ``type=3``/``stream=3``) is **not** the classic
  Kerberos/RC4 secure stream: ``FUN_141355dc0`` (0x1355dc0, a global byte)
  switches "RVSec" on, and the CONNECT then carries **ECDH P-256** material
  (``FUN_1413687a0`` 0x13687a0: ``BCryptOpenAlgorithmProvider("ECDH_P256")`` +
  ``BCryptGenerateKeyPair`` + ``BCryptExportKey("ECCPUBLICBLOB")``;
  ``FUN_1413688d0`` 0x13688d0: ``BCryptImportKeyPair`` +
  ``BCryptSecretAgreement`` + ``BCryptDeriveKey`` -> a 16-byte session key).
  RVSec is switched on iff the *rendezvous configuration* we serve carries a
  non-empty ``public_key`` member (``FUN_14134b44f`` 0x134b44f, key string read
  at 0x134b32a, ``FUN_141355eb0`` 0x1355eb0 at 0x134b471).

So the four ``Login``/``LoginEx``/``RequestTicket`` builders below are **not**
proven against Anno: they are the acb-rdv reference shapes for a client that
*does* speak 0x0A (a different Ubisoft title on the same engine).  They are
kept here, byte-faithful to ``decomp/ref/acb-rdv/quazal_protocols_authentication.py``
and ``quazal_kerberos.py``, because (a) they are the only known 0x0A shapes for
this engine and (b) a future Anno build without RVSec would need exactly them.
Everything that is *not* in that reference -- and every 0x0A method name that
would have to be invented -- is listed in :data:`UNKNOWN_METHODS` instead of
being guessed.

Conventions (same package contract as :mod:`rv_shapes.login` / ``profile``)
---------------------------------------------------------------------------
Every builder returns the reply **payload** (after the envelope's ``u32 size |
u16 proto_len "Proto\\0" | u8 type=0 | u8 status=1 | u32 call_id``): the method
name block, then the method's fields.  The name block is produced by
:func:`rv_shapes.name_block` -- ``u16 length INCLUDING the NUL``,
``"Proto::Method"``, ``"*"``, NUL; the trailing ``*`` is mandatory because the
client builds ``decodedName + "*"`` and ``_mbscmp``s it.  A Quazal ``String``
is ``u16 length EXCLUDING the NUL`` + bytes (:func:`rv_shapes.qstr`).  A Quazal
``Buffer`` -- used for the ticket -- is ``u32 length`` + bytes (:func:`blob`).
All integers are little-endian.
"""

import hashlib
import hmac
import struct

try:  # normal package import
    from . import name_block as _name_block
    from . import qstr as _qstr
    from . import u16 as _u16
    from . import u32 as _u32
    from . import u8 as _u8
except ImportError:  # pragma: no cover - flat sys.path (server/ on sys.path)
    from rv_shapes import name_block as _name_block
    from rv_shapes import qstr as _qstr
    from rv_shapes import u16 as _u16
    from rv_shapes import u32 as _u32
    from rv_shapes import u8 as _u8

__all__ = [
    "DEFAULT_PROTO",
    "PRESENT_IN_ANNO",
    "PROTOCOL_TABLE",
    "RESULT_SUCCESS",
    "DEFAULT_PASSWORD",
    "DEFAULT_INNER_TICKET",
    "DEFAULT_SERVER_ID",
    "DEFAULT_SESSION_KEY",
    "SESSION_KEY_SIZE",
    "METHOD_NAMES",
    "METHODS",
    "UNKNOWN_METHODS",
    "PROVEN_METHODS",
    "blob",
    "rc4",
    "derive_key",
    "hmac_md5",
    "build_ticket",
    "open_ticket",
    "secure_url",
    "connection_data",
    "login",
    "login_ex",
    "request_ticket",
    "resolve_method",
    "build",
]

#: The name the reference implementation answers under.  Anno never sends it.
DEFAULT_PROTO = "AuthenticationProtocol"

#: ``False`` = there is no 0x0A peer in Anno5.exe (see the module docstring).
#: A dispatcher may use this to keep the reference builders disabled.
PRESENT_IN_ANNO = False

#: Every ``*ProtocolClient`` class Anno5.exe instantiates, as
#: ``(class name, wire protocol name, vtable RVA)``.  The wire names were
#: decoded by running each class's ``GetProtocolName`` (vtable slot +0x98)
#: under a small x86-64 emulator -- they are XOR-obfuscated in .text, so they
#: do not appear in a string dump.  **There is no 0x0A entry.**
PROTOCOL_TABLE = (
    ("LoginProtocolClient", "LoginProtocol", 0x1649CD0),
    ("BackEndServiceProtocolClient", "BackEndServiceProtocol", 0x1724CC8),
    ("PlayerProfileServiceProtocolClient", "PlayerProfileServiceProtocol", 0x1724DE0),
    ("VotingServiceProtocolClient", "VotingServiceProtocol", 0x1725110),
    ("WorldEventServiceProtocolClient", "WorldEventServiceProtocol", 0x17254B8),
    ("ChatServiceProtocolClient", "ChatServiceProtocol", 0x1725258),
    ("MailServiceProtocolClient", "MailServiceProtocol", 0x1725380),
    ("FriendsServiceProtocolClient", "FriendsServiceProtocol", 0x1725690),
    ("MatchMakingServiceProtocolClient", "MatchMakingServiceProtocol", 0x1725828),
    ("LoadTestServiceProtocolClient", "LoadTestServiceProtocol", 0x1725A78),
)

#: ``Quazal::RESULT_SUCCESS`` -- the leading u32 of every LoggedIn reply.
RESULT_SUCCESS = 0

#: The reference's dummy Kerberos password (the ticket key is derived from it).
DEFAULT_PASSWORD = "UbiDummyPwd"

#: Inner ticket dumped from the original service (acb-rdv
#: ``quazal_kerberos.DEFAULT_INNER_TICKET``, 34 bytes).  The server never
#: opens it, the client only round-trips it, so a fixed blob is fine.
DEFAULT_INNER_TICKET = bytes.fromhex(
    "76214BA62196D3F39A8C7A270DD9B3FA210EEDAF42639295C1165408EE6E691735782E6E"
)

#: Server id the reference's ``Login`` advertises (``RVConnectionData``).
DEFAULT_SERVER_ID = 2

#: A ticket is ``RC4(key, 16-byte session key | u32 pid | u32 len | inner)``
#: followed by a 16-byte HMAC-MD5, so the session key is exactly 16 bytes.
SESSION_KEY_SIZE = 16
DEFAULT_SESSION_KEY = b"\x00" * SESSION_KEY_SIZE

#: ``RVConnectionData`` as the reference writes it: ``String url | u32 0 |
#: String ""`` -- the u32 is the (empty) special-protocol table.
_CONNECTION_DATA_TAIL = _u32(0) + _qstr("")

_STR_MAX = 0xFFFF
_U32_MAX = 0xFFFFFFFF


# --------------------------------------------------------------------------
# coercion helpers -- total on any input, never raise
# --------------------------------------------------------------------------
def _proto_name(proto):
    """A usable protocol name; falls back to :data:`DEFAULT_PROTO`."""
    if isinstance(proto, (bytes, bytearray, memoryview)):
        try:
            proto = bytes(proto).decode("ascii")
        except Exception:
            return DEFAULT_PROTO
    if not isinstance(proto, str):
        return DEFAULT_PROTO
    text = proto.replace("\x00", "").strip().strip("*").strip()
    if "::" in text:
        text = text.split("::")[0].strip()
    return text or DEFAULT_PROTO


def _as_bytes(value, default=b""):
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return value.encode("ascii")
        except Exception:
            return value.encode("ascii", "replace")
    return default


def _qstr_safe(text):
    """``qstr`` that never raises: the u16 length caps at 0xFFFF bytes."""
    raw = _as_bytes(text)
    if len(raw) > _STR_MAX:
        raw = raw[:_STR_MAX]
    return _qstr(raw)


def _u32v(value, default=0):
    try:
        return int(value) & _U32_MAX if value is not None else default & _U32_MAX
    except Exception:
        return default & _U32_MAX


def _session_key(value):
    """Exactly :data:`SESSION_KEY_SIZE` bytes (zero-padded / truncated)."""
    raw = _as_bytes(value, DEFAULT_SESSION_KEY)
    return raw[:SESSION_KEY_SIZE].ljust(SESSION_KEY_SIZE, b"\x00")


# --------------------------------------------------------------------------
# wire primitives this protocol needs beyond the package helpers
# --------------------------------------------------------------------------
def blob(data):
    """Quazal ``Buffer``: ``u32 length`` + the raw bytes (no NUL, no padding).

    The ticket travels in one of these: the reference's
    ``Writer.buffer(ticket)``.  Contrast :func:`rv_shapes.qstr`, whose length
    prefix is a *u16 that excludes* the NUL.
    """
    raw = _as_bytes(data)
    return _u32(len(raw)) + raw


def secure_url(address, port, server_pid, cid=1):
    """``prudps:/address=...;port=...;CID=..;PID=..;sid=1;stream=3;type=2``.

    Exactly the reference's ``secure_url()`` -- the ``s`` in ``prudps`` plus
    ``stream=3``/``type=2`` is what selects the secure stream on the client.
    """
    addr = _as_bytes(address, b"127.0.0.1").decode("ascii", "replace")
    port = _u32v(port, 0) & 0xFFFF
    pid = _u32v(server_pid, DEFAULT_SERVER_ID) & 0xFFFF
    cid = _u32v(cid, 1) & 0xFFFF
    return (
        f"prudps:/address={addr};port={port};CID={cid};PID={pid}"
        ";sid=1;stream=3;type=2"
    )


def connection_data(url):
    """``RVConnectionData``: ``String url | u32 0 | String ""`` (reference)."""
    return _qstr_safe(url) + _CONNECTION_DATA_TAIL


# --------------------------------------------------------------------------
# Kerberos ticket (reference ``quazal_kerberos.py``, quoted in the docstring)
# --------------------------------------------------------------------------
def rc4(key, data):
    """RC4 (encrypt == decrypt); same schedule as the reference's ``rc4()``."""
    key = _as_bytes(key)
    data = _as_bytes(data)
    if not key:
        return data
    s = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + s[i] + key[i % klen]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = b ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


_KEY_CACHE = {}


def derive_key(pid, password=DEFAULT_PASSWORD):
    """Kerberos key: MD5 iterated ``65000 + pid % 1024`` times over the password.

    Straight from the reference (``Helper.cs``).  Cached, because 65k hashes
    per call would otherwise show up in the server's latency.
    """
    pid = _u32v(pid, 0)
    if isinstance(password, (bytes, bytearray)):
        try:
            password = bytes(password).decode("ascii")
        except Exception:
            password = DEFAULT_PASSWORD
    if not isinstance(password, str):
        password = DEFAULT_PASSWORD
    ck = (pid, password)
    got = _KEY_CACHE.get(ck)
    if got is not None:
        return got
    buf = password.encode("ascii")
    for _ in range(65000 + pid % 1024):
        buf = hashlib.md5(buf).digest()
    _KEY_CACHE[ck] = buf
    return buf


def hmac_md5(key, data):
    return hmac.new(_as_bytes(key), _as_bytes(data), hashlib.md5).digest()


def build_ticket(user_pid, server_pid, session_key, inner=None,
                 password=DEFAULT_PASSWORD):
    """``RC4(key, session[16] | u32 server_pid | u32 len | inner) + HMAC-MD5``.

    ``key = derive_key(user_pid, password)``; the result is what the reference
    puts in the ``Buffer`` of a ``Login``/``LoginEx``/``RequestTicket`` reply
    and what the client echoes back in the CONNECT payload.
    """
    key = derive_key(user_pid, password)
    inner = DEFAULT_INNER_TICKET if inner is None else _as_bytes(inner)
    plain = (
        _session_key(session_key)
        + _u32(_u32v(server_pid, DEFAULT_SERVER_ID))
        + _u32(len(inner))
        + inner
    )
    enc = rc4(key, plain)
    return enc + hmac_md5(key, enc)


def open_ticket(ticket, user_pid, password=DEFAULT_PASSWORD):
    """Inverse of :func:`build_ticket` -> ``(session_key, server_pid, inner)``.

    Raises ``ValueError`` on a MAC mismatch.  Only used by the tests and by an
    operator debugging a capture; the *server* never has to open a ticket the
    client sends (it re-derives what it needs from its own state).
    """
    raw = _as_bytes(ticket)
    if len(raw) < 24 + 16:
        raise ValueError("ticket too short (%d bytes)" % len(raw))
    key = derive_key(user_pid, password)
    enc, mac = raw[:-16], raw[-16:]
    if hmac_md5(key, enc) != mac:
        raise ValueError("ticket HMAC mismatch")
    plain = rc4(key, enc)
    (server_pid, size) = struct.unpack_from("<II", plain, SESSION_KEY_SIZE)
    if 24 + size > len(plain):
        raise ValueError("inner ticket length %d exceeds payload" % size)
    return plain[:SESSION_KEY_SIZE], server_pid, plain[24:24 + size]


# --------------------------------------------------------------------------
# builders -- payload = name block + fields (no envelope, no call id)
# --------------------------------------------------------------------------
def login(pid=DEFAULT_SERVER_ID, ticket=b"", url="", build_name="",
          proto=DEFAULT_PROTO, password=DEFAULT_PASSWORD,
          session_key=DEFAULT_SESSION_KEY, server_id=DEFAULT_SERVER_ID):
    """``Login``: ``u32 result | u32 pid | Buffer ticket | RVConnectionData |
    String server_build_name``.

    Reference ``AuthenticationProtocol.login`` (``@method(1, "Login")``)::

        w.u32(RESULT_SUCCESS).u32(TRACKING_USER.pid).buffer(TRACKING_LOGIN_TICKET)
        self._connection_data(w, secure_url(...))
        w.string("")  # server build name

    ``ticket`` defaults to one built from ``session_key``/``server_id`` (pass
    ``ticket=...`` to supply the original service's dumped bytes verbatim).
    """
    body = _as_bytes(ticket)
    if not body:
        body = build_ticket(pid, server_id, session_key, password=password)
    return (
        _name_block(_proto_name(proto), "Login")
        + _u32(RESULT_SUCCESS)
        + _u32(_u32v(pid, DEFAULT_SERVER_ID))
        + blob(body)
        + connection_data(url)
        + _qstr_safe(build_name)
    )


def login_ex(pid=DEFAULT_SERVER_ID, ticket=b"", url="", message="",
             proto=DEFAULT_PROTO, password=DEFAULT_PASSWORD,
             session_key=DEFAULT_SESSION_KEY, server_id=DEFAULT_SERVER_ID):
    """``LoginEx``: same reply as :func:`login`; the last String is the
    "return message" instead of the build name (reference ``login_ex``::

        w.u32(RESULT_SUCCESS).u32(user.pid).buffer(ticket)
        self._connection_data(w, secure_url(...))
        w.string("")  # return message

    The *request* carries a ``UbiAuthenticationLoginCustomData`` AnyDataHolder
    (username, online key, password) -- we never have to build that, only
    parse it, and Anno never sends it.
    """
    body = _as_bytes(ticket)
    if not body:
        body = build_ticket(pid, server_id, session_key, password=password)
    return (
        _name_block(_proto_name(proto), "LoginEx")
        + _u32(RESULT_SUCCESS)
        + _u32(_u32v(pid, DEFAULT_SERVER_ID))
        + blob(body)
        + connection_data(url)
        + _qstr_safe(message)
    )


def request_ticket(ticket=b"", proto=DEFAULT_PROTO, pid=DEFAULT_SERVER_ID,
                   server_id=DEFAULT_SERVER_ID, password=DEFAULT_PASSWORD,
                   session_key=DEFAULT_SESSION_KEY):
    """``RequestTicket``: ``u32 result | Buffer ticket`` (nothing else).

    Reference ``AuthenticationProtocol.request_ticket``::

        return self.ctx.writer().u32(RESULT_SUCCESS).buffer(ticket)
    """
    body = _as_bytes(ticket)
    if not body:
        body = build_ticket(pid, server_id, session_key, password=password)
    return _name_block(_proto_name(proto), "RequestTicket") + _u32(RESULT_SUCCESS) + blob(body)


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
#: method -> human-readable reply layout (and the reference handler it mirrors)
METHOD_NAMES = {
    "Login": (
        "u32 result | u32 pid | Buffer ticket | String secure_url | u32 0 | "
        "String \"\" | String build_name   [acb-rdv reference shape]"
    ),
    "LoginEx": (
        "u32 result | u32 pid | Buffer ticket | String secure_url | u32 0 | "
        "String \"\" | String return_message   [acb-rdv reference shape]"
    ),
    "RequestTicket": "u32 result | Buffer ticket   [acb-rdv reference shape]",
}

#: The three reference methods, in the reference's own id order (1/2/3).
METHODS = tuple(METHOD_NAMES)

#: Methods with **no** provable shape at all -- neither in Anno (which has no
#: 0x0A) nor in the reference.  Nothing is emitted for these; a caller must
#: fall back to a name-block-only reply.  Kept as a dict (``method -> reason``)
#: to match ``rv_shapes.profile.UNKNOWN_METHODS``.
UNKNOWN_METHODS = {
    # The Quazal Authentication service also defines LoginWithToken /
    # LoginWithUbiToken / RequestTicket variants in *some* 3.x builds (the
    # client's own LoginProtocol has LoginWithToken_V1, which is a different
    # protocol).  None of their names or payloads can be recovered from
    # Anno5.exe, and the reference does not implement them, so they stay
    # unguessed.
}

#: Methods proven byte-for-byte against Anno's client -- **empty by
#: construction**: Anno has no Authentication protocol reader to prove against.
PROVEN_METHODS = {}

_BUILDERS = {
    "Login": login,
    "LoginEx": login_ex,
    "RequestTicket": request_ticket,
}

_RESOLVE = {k.lower(): k for k in METHOD_NAMES}


def resolve_method(method):
    """Map a loosely-specified method name to a key of :data:`METHOD_NAMES`."""
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
    return _RESOLVE.get(text.lower())


def build(proto, method, **kw):
    """Complete payload (name block + fields) for ``proto::method`` or ``None``.

    ``None`` = "no shape known" (unknown method), so the caller can fall back
    to a name-block-only reply.  Unknown keyword arguments are ignored; junk
    values are coerced by the individual builders, which never raise.
    """
    key = resolve_method(method)
    if key is None:
        return None
    func = _BUILDERS[key]
    try:
        import inspect

        allowed = set(inspect.signature(func).parameters) - {"proto"}
    except Exception:  # pragma: no cover - defensive
        allowed = {
            "pid", "ticket", "url", "build_name", "message", "password",
            "session_key", "server_id",
        }
    args = {k: v for k, v in kw.items() if k in allowed}
    try:
        return func(proto=_proto_name(proto), **args)
    except Exception:  # pragma: no cover - defensive
        return None
