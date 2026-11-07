"""Wire-shape builders for the RendezVous (RV/Storm) session layer.

Every builder returns the *payload* of a reply (the bytes after the call id),
already including the method name with its trailing '*'. The envelope itself
(u32 size | u16 proto_len "Proto\0" | u8 type=0 | u8 status=1 | u32 call_id |
payload) is added by mimic_server.rv_rmc_reply.

Rules decoded from the client (see decomp/batch/*):
  * the reply's method name MUST end with '*'  (FUN_141370440 compares
    decodedName + "*" with _mbscmp)
  * the name is length-prefixed with a u16 that INCLUDES the NUL
  * payload integers are LITTLE-endian (the client memcpy's them; its own
    request envelope proves LE on this wire)
  * a Quazal String is u16 length EXCLUDING the NUL, then the bytes
"""
import struct


def name_block(proto, method):
    """u16 len (incl NUL) + 'Proto::Method*' + NUL."""
    nm = f"{proto}::{method}*".encode("ascii") + b"\x00"
    return struct.pack("<H", len(nm)) + nm


def qstr(text):
    """Quazal String: u16 length INCLUDING the NUL, then the bytes + NUL.

    The engine's own writer (FUN_141370f90) emits ``u16 (strlen + 1)`` and then
    ``strlen + 1`` bytes, and its reader (FUN_141371620) sizes the object to
    ``len + 1`` while copying ``len`` bytes -- so byte ``len - 1`` must be the
    0x00 terminator.  Writing ``len`` without it left the client's last byte as
    uninitialised heap memory, which is the string that rendered as
    "Player" followed by a few boxes.
    """
    b = text.encode("ascii") if isinstance(text, str) else bytes(text)
    b = b[:0xFFFE]                       # the count must stay a u16
    return struct.pack("<H", len(b) + 1) + b + b"\x00"


def u8(v):
    return struct.pack("<B", v & 0xFF)


def u16(v):
    return struct.pack("<H", v & 0xFFFF)


def u32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


def u64(v):
    return struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)


# --- package-level dispatch (PN 138) ---------------------------------------
# Import the per-protocol modules lazily so a broken one cannot take the rest
# down, and expose them as attributes plus one `build()` entry point.
_MODULES = {}


def _mod(name):
    if name not in _MODULES:
        try:
            _MODULES[name] = __import__(f"rv_shapes.{name}", fromlist=[name])
        except Exception as exc:          # pragma: no cover - defensive
            _MODULES[name] = exc
    got = _MODULES[name]
    if isinstance(got, Exception):
        raise got
    return got


_PROTO_MODULE = {
    "backendserviceprotocol": "backend",
    "loginprotocol": "login",
    "playerprofileserviceprotocol": "profile",
    "votingserviceprotocol": "voting",
    "worldeventserviceprotocol": "voting",
    "mailserviceprotocol": "mail",
    # A5 lobby / matchmaking (PN 184).  Every one of the 30 methods the
    # client's reply dispatcher (RVA 0xE04110) tests by name is answered;
    # the 17 pure setters reply with the name block only.
    "matchmakingserviceprotocol": "matchmaking",
    # Registered, but deliberately NOT on the reply path: no Anno 2070 build
    # speaks protocol 0x0A (``rv_shapes.auth.PRESENT_IN_ANNO is False``), so
    # ``build()`` below returns None for it and the caller falls back to a
    # name-block-only reply.  Call ``rv_shapes.auth.build(...)`` directly to
    # exercise the reference shapes.
    "authenticationprotocol": "auth",
}


def build(proto, method, **kw):
    """Full reply payload for (proto, method), or None if we do not know it."""
    modname = _PROTO_MODULE.get(str(proto).strip("*").lower())
    if not modname:
        return None
    try:
        mod = _mod(modname)
    except Exception:
        return None
    if getattr(mod, "PRESENT_IN_ANNO", True) is False:
        # the module declares that this client never sends its protocol:
        # keep it off the default reply path (no invented names on the wire)
        return None
    fn = getattr(mod, "build", None)
    if fn is None:
        return None
    try:
        return fn(proto, method, **kw)
    except TypeError:
        try:
            return fn(proto, method)
        except Exception:
            return None
    except Exception:
        return None


def available():
    """Which shape modules imported cleanly (for /status and the log)."""
    out = {}
    for name in ("backend", "profile", "voting", "login", "auth", "matchmaking",
                 "mail"):
        try:
            _mod(name)
            out[name] = "ok"
        except Exception as exc:
            out[name] = f"error: {exc!r}"
    return out


# eager import so `rv_shapes.backend` etc. are attributes (cheap, pure modules)
for _n in ("backend", "profile", "voting", "login", "auth", "matchmaking", "mail"):
    try:
        _mod(_n)
        globals()[_n] = _MODULES[_n]
    except Exception:      # pragma: no cover
        pass
