#!/usr/bin/env python3
"""prudp_transport.py -- Quazal PRUDP transport for the Anno 2070 mimic server.

Frame layout (verified against the 2015 netz1.log narrative and the reference
implementation in refs/5th-echelon/quazal/src/prudp/packet.rs, i.e. Ubisoft's
own PRUDP client, which Splinter Cell Blacklist uses the same way Anno does):

    u8   source       = (stream_type << 4) | port
    u8   destination  = (stream_type << 4) | port
    u8   type_flag    = (flags << 3) | packet_type
    u8   session_id
    u32  signature                       (LE)
    u16  sequence                        (LE)
    u32  conn_signature                  (SYN/CONNECT only)
    u8   fragment_id                     (DATA only)
    u16  payload_size                    (only when flag HAS_SIZE is set)
    ...  payload
    u32  checksum                    (LE, PN 131: covers the prefix)

    packet_type: SYN 0, CONNECT 1, DATA 2, DISCONNECT 3, PING 4, USER 5, ROUTE 6, RAW 7
    flags:       ACK 1, RELIABLE 2, NEED_ACK 4, HAS_SIZE 8
    stream types: DO 1, RV 2, RVSEC 3, SBMGMT 4, NAT 5, ...

RMC (the game-service layer) rides inside DATA payloads.  Two dialects are
understood here:

  * the NAMED frame -- the one the client's own parsers use (proven, see
    decomp/_adhoc/rmcsuccess.md):

        u32 size | u16 proto_len (INCLUDING the NUL) | "Proto\\0"
                 | u8 type (1 = call, 0 = reply)
                 | u8 status (replies only; 1 = clean result)
                 | u32 call_id
                 | payload

    A reply payload must START with the method name block
    (u16 len incl NUL, "Proto::Method*", NUL) -- every byte after the call id
    is produced by rv_shapes.

  * the CLASSIC Quazal numeric form (what the old builder emitted):
    u8 protocol_id|0x80 (or 0xff + u16), u32 call_id, u32 method_id, params,
    responses adding u32 return_value + u32 error_code + body.  It is still
    parsed for compatibility, but the *response* path no longer emits it.

``build_rmc_response`` therefore returns the named envelope: status 1 (clean),
the request's call id and the request's protocol name.  All integers are
little-endian (the client's own frames prove LE on this wire).

The class below is a *server*: it answers CONNECT/PING, acknowledges DATA, and
hands RMC requests to a callback (the radar of the mimic server's RMC services).
Everything it does is logged so a real client capture can be diffed against it.
"""
import os
import socket
import struct
import threading
import time
import traceback

SYN, CONNECT, DATA, DISCONNECT, PING, USER, ROUTE, RAW = range(8)

# the connection signature handed out in the SYN/SYN-ACK exchange (PN 113)
CONN_SIGNATURE = 0x12345678
FLAG_ACK, FLAG_RELIABLE, FLAG_NEED_ACK, FLAG_HAS_SIZE = 1, 2, 4, 8

STREAM_DO, STREAM_RV, STREAM_RVSEC, STREAM_SBMGMT, STREAM_NAT = 1, 2, 3, 4, 5

TYPE_NAMES = {SYN: "SYN", CONNECT: "CONNECT", DATA: "DATA", DISCONNECT: "DISCONNECT",
              PING: "PING", USER: "USER", ROUTE: "ROUTE", RAW: "RAW"}


def checksum(seed, body):
    """PN 131 - the real algorithm (disassembled at FUN_141367690):

        expected = seed + sum(u32_le(words of body[0:n])) + int_le(tail)

    where n = len(body) rounded down to 4 and tail = body[n:] zero-padded to 4
    bytes, read little-endian. The trailer on the wire is a **4-byte u32**, not
    the single byte we used before - that made every frame we sent 3 bytes short
    and is what the client's parser kept rejecting.
    """
    n = len(body) - (len(body) % 4)
    acc = seed & 0xFFFFFFFF
    for i in range(0, n, 4):
        acc = (acc + struct.unpack_from("<I", body, i)[0]) & 0xFFFFFFFF
    tail = body[n:] + b"\0" * (3 - len(body[n:]))
    return (acc + int.from_bytes(tail, "little")) & 0xFFFFFFFF


def derive_seed(body, trailer):
    """The seed a peer used: trailer - (u32 sum + weighted tail). Exact, no guessing."""
    return (trailer - checksum(0, body)) & 0xFFFFFFFF


class Packet:
    __slots__ = ("source", "destination", "ptype", "flags", "session_id", "signature",
                 "sequence", "conn_signature", "fragment_id", "payload", "checksum",
                 "checksum_ok", "raw_size", "seed")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def src_type(self):
        return (self.source or 0) >> 4

    def src_port(self):
        return (self.source or 0) & 0xF

    def __str__(self):
        return (f"{TYPE_NAMES.get(self.ptype, self.ptype)} "
                f"src={self.src_type()}:{self.src_port()} dst={self.destination:#04x} "
                f"flags={self.flags:#04x} sid={self.session_id} seq={self.sequence} "
                f"sig={self.signature:#010x} "
                f"{'connsig=' + hex(self.conn_signature) + ' ' if self.conn_signature is not None else ''}"
                f"payload={len(self.payload or b'')}B csum={'ok' if self.checksum_ok else 'BAD'}")


def parse_packet(data, key=0):
    if len(data) < 11:
        raise ValueError(f"short packet ({len(data)}B)")
    p = Packet()
    p.source = data[0]
    p.destination = data[1]
    type_flag = data[2]
    p.ptype = type_flag & 0x7
    p.flags = type_flag >> 3
    p.session_id = data[3]
    p.signature = struct.unpack_from("<I", data, 4)[0]
    p.sequence = struct.unpack_from("<H", data, 8)[0]
    off = 10
    p.conn_signature = None
    p.fragment_id = None
    if p.ptype in (SYN, CONNECT):
        p.conn_signature = struct.unpack_from("<I", data, off)[0]
        off += 4
    if p.ptype == DATA:
        p.fragment_id = data[off]
        off += 1
    if p.flags & FLAG_HAS_SIZE:
        size = struct.unpack_from("<H", data, off)[0]
        off += 2
    else:
        size = len(data) - off - 4          # PN 131: 4-byte trailer
    p.payload = bytes(data[off:off + size])
    off += size
    p.checksum = struct.unpack_from("<I", data, len(data) - 4)[0] if len(data) >= 4 else None
    # PN 115: the checksum covers the packet bytes BEFORE the checksum byte and
    # is keyed by the DESTINATION stream type (RVSec -> sum of the access-key
    # bytes). The old check hashed data[off:] - i.e. the checksum byte itself -
    # so it always "passed" and we never noticed our own replies were keyed wrong.
    p.seed = None
    if p.checksum is None:
        p.checksum_ok = False
    else:
        body = bytes(data[:len(data) - 4])
        p.seed = derive_seed(body, p.checksum)
        # RVSec peers use a keyed seed; everything else must derive to 0
        want_zero = ((p.destination or 0) >> 4) != 3
        p.checksum_ok = (p.seed == 0) if want_zero else True
    p.raw_size = len(data)
    return p


def build_packet(ptype, source, destination, session_id=0, signature=0, sequence=0,
                 flags=0, conn_signature=None, fragment_id=None, payload=b"", key=0):
    out = bytearray()
    out += bytes([source & 0xFF, destination & 0xFF, ((flags & 0x1F) << 3) | (ptype & 0x7),
                  session_id & 0xFF])
    out += struct.pack("<I", signature & 0xFFFFFFFF)
    out += struct.pack("<H", sequence & 0xFFFF)
    if ptype in (SYN, CONNECT, ROUTE):
        # PN 147: the client verifies wire4..7 of EVERY non-SYN/USER packet against
        # a MAC over (peer address, hash(stream+0x120)) whose value is exactly the
        # wire10..13 it sent in its own CONNECT. Anything else is dropped silently,
        # so every reply we send must echo it. Type 6 (the client's SPEC) also
        # carries the u32 - its ACK needs 0x80000000 to flip the stream to state 2.
        out += struct.pack("<I", (conn_signature or 0) & 0xFFFFFFFF)
    if ptype == DATA:
        out += bytes([fragment_id or 0])
    if flags & FLAG_HAS_SIZE:
        out += struct.pack("<H", len(payload))
    out += payload
    # PN 131: the trailer is a 4-byte u32 = seed + u32-sum(prefix) + weighted tail
    out += struct.pack("<I", checksum(key, bytes(out)))
    return bytes(out)


# --------------------------------------------------------------------------
# RMC framing (named = what the client speaks; numeric = classic Quazal)
# --------------------------------------------------------------------------

#: numeric protocol id -> protocol name.  EMPTY on purpose: no capture has
#: pinned a single (id -> name) pair yet (the corpus only carries RTTI names).
#: Fill it in when one is measured; until then a frame echoes the *name* it was
#: given, and an id-only request is answered under ``"Protocol<id>"`` so the
#: pair still round-trips through :func:`parse_rmc_request_ex`.
PROTOCOL_NAMES = {}

#: method name -> numeric method id (same situation as PROTOCOL_NAMES).
METHOD_IDS = {}


_PLACEHOLDER = "Protocol"


def protocol_name(protocol_id):
    """Name a reply carries for ``protocol_id`` (or the id's own name)."""
    if isinstance(protocol_id, str) and protocol_id:
        return protocol_id if _ascii_ident(protocol_id) else "Core"
    try:
        pid = int(protocol_id)
    except Exception:                                        # pragma: no cover
        return "Core"
    name = PROTOCOL_NAMES.get(pid)
    if name:
        return name
    if pid < 0:
        return "Core"
    return _PLACEHOLDER + str(pid)


def method_name(method_id):
    """Name for a numeric method id (``None`` while METHOD_IDS is empty)."""
    for name, mid in METHOD_IDS.items():
        if mid == method_id:
            return name
    return None


def protocol_id_for_name(name):
    """Numeric id for a protocol name (``"Protocol11"`` -> 11); 0 if unknown."""
    if not name:
        return 0
    for pid, known in PROTOCOL_NAMES.items():
        if known == name:
            return pid
    if name.startswith(_PLACEHOLDER) and name[len(_PLACEHOLDER):].isdigit():
        return int(name[len(_PLACEHOLDER):])
    return 0


def _ascii_ident(text):
    """True when ``text`` is a plain printable ASCII protocol name."""
    if not isinstance(text, str) or not text:
        return False
    return all(0x21 <= ord(c) <= 0x7E and c not in ":*" for c in text)


def parse_named_frame(payload):
    """``{"proto", "call_id", "kind", "payload_off"}`` or None.

    ``kind`` is 1 for a call and 0 for a reply.  The grammar is the one the
    client's own frame uses (decomp/_adhoc/rmcsuccess.md)::

        u32 size | u16 plen (INCL NUL) "Proto\\0" | u8 type
                 | (type 0: u8 status) | u32 call_id | payload
    """
    try:
        payload = bytes(payload or b"")
    except Exception:                                        # pragma: no cover
        return None
    if len(payload) < 12:
        return None
    declared = struct.unpack_from("<I", payload, 0)[0]
    if declared < 8 or declared > len(payload) + 8:          # tolerate slop
        return None
    plen = struct.unpack_from("<H", payload, 4)[0]
    if plen < 2 or plen > 128:
        return None
    end = 6 + plen
    if end > len(payload) or payload[end - 1:end] != b"\x00":
        return None
    try:
        proto = payload[6:end - 1].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not _ascii_ident(proto):
        return None
    off = end
    if off >= len(payload):
        return None
    kind = payload[off]
    off += 1
    if kind == 1:                                            # a call
        if off + 4 > len(payload):
            return None
        call_id = struct.unpack_from("<I", payload, off)[0]
        off += 4
    elif kind == 0:                                          # a reply
        if off + 1 > len(payload) or payload[off] != 1:
            return None
        off += 1
        if off + 4 > len(payload):
            return None
        call_id = struct.unpack_from("<I", payload, off)[0]
        off += 4
    else:
        return None
    return {"proto": proto, "call_id": call_id, "kind": kind, "payload_off": off}


def split_name_block(payload):
    """``(method_without_star, rest)`` from a reply payload; ("", payload) if none.

    The name block is ``u16 length INCLUDING the NUL``, then the name, then the
    NUL.  A reply's name carries the trailing ``'*'`` the client appends before
    comparing; it is stripped here because it is not part of the method name.
    """
    try:
        payload = bytes(payload or b"")
    except Exception:                                        # pragma: no cover
        return "", b""
    if len(payload) < 4:
        return "", payload
    mlen = struct.unpack_from("<H", payload, 0)[0]
    if mlen < 2 or mlen > 256 or 2 + mlen > len(payload):
        return "", payload
    raw = payload[2:2 + mlen]
    if raw[-1:] != b"\x00":
        return "", payload
    try:
        name = raw[:-1].decode("ascii")
    except UnicodeDecodeError:
        return "", payload
    if name.endswith("*"):
        name = name[:-1]
    return name, payload[2 + mlen:]


def parse_rmc_request_ex(payload):
    """Full parse of one RMC payload, for either dialect; None when neither.

    Returns ``{"dialect", "proto", "protocol_id", "call_id", "kind",
    "method", "full_method", "method_id", "params"}``.  ``method`` is the BARE
    method name (``"GetArkItems_V1"``) -- that is the unit a name-based
    dispatcher is keyed by, mirroring the ws path -- and ``full_method`` is the
    name exactly as the payload carried it (``"Proto::Method"``).  For the
    numeric dialect ``proto`` is ``""`` and both names are ``None`` (the wire
    carries numbers only).
    """
    named = parse_named_frame(payload)
    if named is not None:
        body = bytes(payload)[named["payload_off"]:]
        full, params = split_name_block(body)
        if not full:                         # a bare payload: nothing to split
            params = body
        bare = full.split("::")[-1] if full else None
        return {
            "dialect": "named",
            "proto": named["proto"],
            "protocol_id": protocol_id_for_name(named["proto"]),
            "call_id": named["call_id"],
            "kind": named["kind"],
            "method": bare,
            "full_method": full or None,
            "method_id": METHOD_IDS.get(full, METHOD_IDS.get(bare, 0)) if full else 0,
            "params": params,
        }
    try:
        payload = bytes(payload or b"")
    except Exception:                                        # pragma: no cover
        return None
    if len(payload) < 13:
        return None
    size = struct.unpack_from("<I", payload, 0)[0]
    if size > len(payload) - 4 + 8:            # tolerate sloppy sizing
        return None
    off = 4
    pid_raw = payload[off]
    off += 1
    if pid_raw == 0xFF:
        pid = struct.unpack_from("<H", payload, off)[0]
        off += 2
    else:
        pid = pid_raw & 0x7F
    call_id, method_id = struct.unpack_from("<II", payload, off)
    off += 8
    return {
        "dialect": "numeric",
        "proto": PROTOCOL_NAMES.get(pid, ""),
        "protocol_id": pid,
        "call_id": call_id,
        "kind": 1,
        "method": method_name(method_id),   # bare ("GetArkItems_V1")
        "full_method": None,
        "method_id": method_id,
        "params": payload[off:],
    }


def parse_rmc_request(payload):
    """Returns (protocol_id, call_id, method_id, params) or None.

    Backwards-compatible tuple view of :func:`parse_rmc_request_ex`; use the
    dict form when the protocol *name* or method *name* matter (i.e. for the
    named frame the client actually speaks).
    """
    req = parse_rmc_request_ex(payload)
    if not req:
        return None
    return req["protocol_id"], req["call_id"], req["method_id"], req["params"]


def build_rmc_response(protocol_id, call_id, method_id, return_value=0, error_code=0,
                       body=b"", proto=None):
    """The RMC reply FRAME (envelope + payload), little-endian.

    Layout (decomp/_adhoc/rmcsuccess.md; the client's own frames are the
    reference):

        u32 size | u16 proto_len (INCL NUL) | "Proto\\0" | u8 0 | u8 1
                 | u32 call_id | payload

    ``size`` is the number of bytes AFTER the u32 size field.  ``type`` is 0
    (a response) and ``status`` is 1: a clean result, no message.  Watch out
    for the trap -- the *envelope's* status 1 means clean, while an embedded
    payload ``Result`` means clean with ``kind 0``.

    ``protocol_id`` is resolved to the protocol *name* the frame carries
    (:func:`protocol_name`; pass ``proto=`` to override it explicitly).
    ``method_id`` and ``return_value`` have no slot in this envelope and are
    accepted only for source compatibility with the old numeric builder --
    the method name inside ``body`` is what identifies the call.  A non-zero
    ``error_code`` switches the envelope to the message form (status 0) and
    reports the code as the result kind.
    """
    proto = protocol_name(proto if proto else protocol_id)
    try:
        call_id = int(call_id) & 0xFFFFFFFF
    except Exception:                                        # pragma: no cover
        call_id = 0
    try:
        payload = bytes(body or b"")
    except Exception:                                        # pragma: no cover
        payload = b""
    try:
        error_code = int(error_code or 0)
    except Exception:                                        # pragma: no cover
        error_code = 0
    if error_code:
        msg = ("RMC error %d" % error_code).encode("ascii") + b"\x00"
        head = bytes([0, 0]) + struct.pack("<H", len(msg)) + msg + struct.pack(
            "<H", error_code & 0xFFFF)      # type 0, status 0 = message follows
    else:
        head = bytes([0, 1])                                 # type 0, status 1
    inner = (struct.pack("<H", len(proto) + 1) + proto.encode("ascii") + b"\x00"
             + head + struct.pack("<I", call_id) + payload)
    return struct.pack("<I", len(inner)) + inner



class PrudpServer(threading.Thread):
    """UDP PRUDP responder. One instance per port; daemon thread."""

    def __init__(self, port, log=print, rmc_dispatch=None, bind="127.0.0.1", key=0,
                 rmc_name_dispatch=None):
        super().__init__(daemon=True)
        self.port = port
        self.bind = bind
        self.log = log
        self.rmc_dispatch = rmc_dispatch          # fn(protocol_id, method_id, params) -> bytes
        self.rmc_name_dispatch = rmc_name_dispatch  # fn(proto, method, params) -> bytes
        self.key = key
        self.sock = None
        self.stop_flag = False
        self.sessions = {}                        # (peer, session_id) -> state dict
        self.syn_variants = {}                    # (peer, sid) -> retry count (PN 114)
        self.sec_seed = 0                        # RVSec checksum seed, derived from a peer frame (PN 131)
        self.counters = {"CONNECT": 0, "DATA": 0, "PING": 0, "DISCONNECT": 0, "bad": 0}
        # PN 147: the peer's connection signature, remembered per (peer, session)
        # so every reply can echo it (see build_packet's note).
        self.connsig = {}

    # ---- lifecycle -------------------------------------------------------
    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.bind, self.port))
        s.settimeout(0.5)
        self.sock = s
        self.log(f"PRUDP listening on {self.bind}:{self.port}/udp (transport responder)")
        return s

    def stop(self):
        self.stop_flag = True

    def run(self):
        try:
            if self.sock is None:
                self.open()
        except OSError as exc:
            self.log(f"PRUDP bind failed on {self.port}: {exc}")
            return
        while not self.stop_flag:
            try:
                data, peer = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.handle(data, peer)
            except Exception:                                   # noqa: BLE001
                self.counters["bad"] += 1
                self.log("PRUDP handler error:\n" + traceback.format_exc())

    # ---- packet handling -------------------------------------------------
    def handle(self, data, peer):
        try:
            p = parse_packet(data, self.key)
        except ValueError as exc:
            self.counters["bad"] += 1
            self.log(f"PRUDP unparsable from {peer[0]}:{peer[1]}: {exc} hex={data[:48].hex()}")
            return
        name = TYPE_NAMES.get(p.ptype, str(p.ptype))
        self.counters[name] = self.counters.get(name, 0) + 1
        if getattr(p, "seed", None) is not None and ((p.destination or 0) >> 4) == 3:
            self.sec_seed = p.seed                # exact, recovered from the peer
        if getattr(p, "conn_signature", None):
            self.connsig[(peer, p.session_id)] = p.conn_signature
        self.log(f"PRUDP <- {name} from {peer[0]}:{peer[1]} {p} seed={getattr(p, 'seed', None)} "
                 f"payload={p.payload[:48].hex()}")

        if p.ptype == SYN:
            self._on_syn(p, peer)
        elif p.ptype == CONNECT:
            self._on_connect(p, peer)
        elif p.ptype == PING:
            self._reply(p, peer, PING, payload=p.payload)
        elif p.ptype == ROUTE:
            # PN 147: the client's SPEC. Its ACK is the ONLY writer of stream
            # state 2 (established), and it must carry ACK + the 0x10 flag (byte2
            # 0x8e) and 0x80000000 in wire10..13.
            self._reply(p, peer, ROUTE, flags=FLAG_ACK | 0x10,
                        conn_signature=0x80000000)
        elif p.ptype == DATA:
            self._on_data(p, peer)
        elif p.ptype == DISCONNECT:
            self._reply(p, peer, DISCONNECT)
        else:
            self.log(f"PRUDP ignoring {name}")

    def _reply(self, p, peer, ptype, payload=b"", flags=None, extra=None,
               conn_signature=None, signature=0, session_id=None):
        if flags is None:
            flags = FLAG_ACK | (FLAG_RELIABLE if (p.flags & FLAG_NEED_ACK) else 0)
        if conn_signature is None:
            conn_signature = (getattr(p, "conn_signature", 0)
                              or self.connsig.get((peer, p.session_id), 0))
        pkt = build_packet(ptype,
                           source=p.destination,     # swap the vports
                           destination=p.source,
                           session_id=(p.session_id if session_id is None else session_id),
                           signature=signature,
                           sequence=p.sequence,
                           flags=flags,
                           conn_signature=conn_signature,
                           fragment_id=(0 if ptype == DATA else None),
                           payload=payload,
                           key=(self.sec_seed
                                if ((p.source or 0) >> 4) == 3 else 0))
        self.sock.sendto(pkt, peer)
        self.log(f"PRUDP -> {TYPE_NAMES.get(ptype, ptype)} to {peer[0]}:{peer[1]} "
                 f"flags={flags:#04x} seq={p.sequence} payload={len(payload)}B "
                 f"hex={pkt[:56].hex()}")

    def _on_syn(self, p, peer):
        """PN 114: the client ignores a reference-shaped SYN-ACK, so answer each
        *retry* with a different variant and let the client's own next packet tell
        us which one it accepts (it retries with 0.25..16 s backoff, so one run
        tests the whole list). RV_SYN_VARIANT=<n> pins a single variant."""
        key = (peer, p.session_id)
        n = self.syn_variants.get(key, 0)
        self.syn_variants[key] = n + 1
        pin = os.environ.get("RV_SYN_VARIANT", "").strip()
        if pin:
            n = int(pin)
        else:
            # PN 147: the sweep is pointless - the ACKed-SYN path never looks at
            # the conn-sig, the sid, wire 8..9 or wire 10..13 (only type != 0
            # packets do), so variants 0..6 were seven runs of one experiment.
            # variant 0 is the proven shape; pin RV_SYN_VARIANT to explore.
            n = 0
        # PN 116: one retry per variant, so the client's own next packet tells us
        # which shape it accepts. Payload LENGTH is the last structural variable:
        # the SYN carries 3 bytes, so the answer may have to carry a token too.
        # PN 120: delivery is PROVEN (the client receives our bytes), so the
        # remaining variable is the header content. sid is the one field we never
        # varied: a server normally ASSIGNS the session id in the SYN-ACK.
        variants = [
            # PN 124: plausibly the u32 the answer carries at packet+0x34 is the
            # wire's conn_signature - sweep plausible "connection id" values.
            ("ack", b"", 1,        0, None),
            ("ack", b"", 2,        0, None),
            ("ack", b"", 0x3f,     0, None),
            ("ack", b"", 0x31,     0, None),
            ("ack", b"", 0x1388,   0, None),
            ("ack", b"", 0x10001,  0, None),
            ("ack", b"", 0x84,     0, None),
        ]
        v = variants[n % len(variants)]
        kind, payload, cs = v[0], v[1], v[2]
        sig = v[3] if len(v) > 3 else 0
        sid = v[4] if len(v) > 4 else None
        if kind == "mirror":
            payload = p.payload or b""
            cs = p.conn_signature or 0
            sig = p.signature or 0
            sid = p.session_id
            flags = p.flags
        ptype = SYN if kind in ("ack", "req", "mirror") else CONNECT
        if kind == "mirror":
            pass
        else:
            flags = FLAG_ACK if kind == "ack" else FLAG_NEED_ACK
        self.log(f"PRUDP SYN variant[{n}] = {kind} payload={len(payload)}B "
                 f"connsig={cs:#010x} sig={sig:#010x} sid={sid}")
        self._reply(p, peer, ptype, conn_signature=cs, flags=flags, payload=payload,
                    signature=sig, session_id=sid)

    def _on_syn_ref(self, p, peer):
        """PN 113: the REAL client opens with SYN, our test client started at
        CONNECT. Answer SYN with SYN+ACK carrying our connection signature (the
        reference implementation uses 0x12345678); the CONNECT that follows
        echoes it. Without this every SYN is dropped and the client retries
        with 0.25/0.5/1/2/4/8/16 s backoff and then gives up."""
        self.sessions[(peer, p.session_id)] = {
            "connected_at": time.time(), "peer": peer, "sid": p.session_id,
            "conn_sig": CONN_SIGNATURE, "syn_payload": p.payload}
        # byte-exact with the reference implementation's SYN-ACK
        # (packet.rs test_syn): flags = ACK only, NO payload, connsig = 0x12345678
        # -> "31 3f 08 00 00000000 0000 78563412 3c"
        self._reply(p, peer, SYN, conn_signature=CONN_SIGNATURE, flags=FLAG_ACK)

    def _on_connect(self, p, peer):
        self.sessions[(peer, p.session_id)] = {"connected_at": time.time(),
                                               "peer": peer, "sid": p.session_id,
                                               "conn_sig": p.conn_signature}
        self._reply(p, peer, CONNECT)

    def _on_data(self, p, peer):
        # Always acknowledge first: PRUDP is reliable, the peer will re-send
        # otherwise and every retry is a chance for the client to give up.
        self._reply(p, peer, DATA)
        req = parse_rmc_request_ex(p.payload)
        if not req:
            return
        pid, call_id, method_id = req["protocol_id"], req["call_id"], req["method_id"]
        params, proto, method = req["params"], req["proto"], req["method"]
        self.log(f"PRUDP RMC request ({req['dialect']}): protocol={pid} "
                 f"proto={proto!r} call={call_id} method={method_id} "
                 f"method_name={req['full_method'] or method!r} params={len(params)}B")
        body = b""
        try:
            if method and self.rmc_name_dispatch:
                # the named frame carries the method NAME, which is what the
                # service layer is keyed by
                body = self.rmc_name_dispatch(proto, method, params) or b""
            elif self.rmc_dispatch:
                body = self.rmc_dispatch(pid, method_id, params) or b""
        except Exception:                                       # noqa: BLE001
            self.log("PRUDP RMC dispatch error:\n" + traceback.format_exc())
            body = b""
        if not body:
            # A clean (status 1) envelope makes the client read a payload: it
            # MUST start with the method name block, or the call is graded an
            # error.  Say so loudly instead of emitting a silent name-less frame.
            self.log("PRUDP RMC: dispatcher returned no payload for "
                     f"protocol={pid} method={method_id} ({method!r}); the client's "
                     "protocol reader needs the rv_shapes name block here")
        resp = build_rmc_response(pid, call_id, method_id, 0, 0, body, proto=proto or None)
        self._reply(p, peer, DATA, payload=resp, flags=FLAG_ACK | FLAG_RELIABLE)
        self.log(f"PRUDP RMC response: protocol={pid} method={method_id} {len(resp)}B")
