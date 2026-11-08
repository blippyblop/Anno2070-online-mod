#!/usr/bin/env python3
"""LIVE probe of the running server, over the websocket it actually uses.

Offline unit tests call ``respond_payload()`` directly; the game does not.  This
asks a *running* server the same questions the client asks, with the same frames
(including the 4-byte request prologue), and checks the answers:

  * a World Council vote (id 1001) is counted on the council's bars and not the
    senate's,
  * the vote credited the career map's faction,
  * the world event carries the month-long campaign (0 < progress <= target,
    target in 25,000..125,000) and a packed date that decodes to a real year.

Usage (the server must already be running):

    python server\probe_rmc.py            # port 8443, the default
    python server\probe_rmc.py 18444      # another port

Exit code 0 = every probe passed.  It only VOTES (once) and reads - it never
touches the ARK box or deletes anything.
"""

import base64
import os
import socket
import struct
import sys

HOST, PORT = "127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 18443
PROLOGUE = b"\x00\x00\x00\x00"
failures = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + str(detail)))
    if not cond:
        failures.append(name)


def ws_connect(path="/notifications"):
    s = socket.create_connection((HOST, PORT), timeout=6)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(4096)
    assert b"101" in resp.split(b"\r\n")[0], resp[:120]
    return s


def ws_send(s, payload, opcode=0x2):
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    ln = len(payload)
    head = struct.pack("!BB", 0x80 | opcode, 0x80 | ln) if ln < 126 else \
        struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, ln)
    s.sendall(head + mask + masked)


def ws_recv(s):
    def exact(n):
        buf = b""
        while len(buf) < n:
            c = s.recv(n - len(buf))
            if not c:
                raise ConnectionError("closed")
            buf += c
        return buf
    b1, b2 = exact(2)
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", exact(2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", exact(8))[0]
    return exact(ln)


CALL = [100]


def rpc(proto, method, params=b""):
    """One request; returns the reply PAYLOAD (name block included)."""
    CALL[0] += 1
    pn = proto.encode() + b"\x00"
    mn = ("%s::%s" % (proto, method)).encode() + b"\x00"
    inner = (struct.pack("<H", len(pn)) + pn + b"\x01"
             + struct.pack("<I", CALL[0])
             + struct.pack("<H", len(mn)) + mn
             + PROLOGUE + params)
    frame = struct.pack("<I", len(inner)) + inner
    s = ws_connect()
    ws_send(s, frame)
    out = ws_recv(s)
    s.close()
    size, plen = struct.unpack_from("<IH", out, 0)
    off = 6 + plen
    return out[off + 6:]


def fields(payload):
    if len(payload) < 2:
        return payload
    return payload[2 + int.from_bytes(payload[0:2], "little"):]


def u32map(payload):
    body = fields(payload)
    n = int.from_bytes(body[:4], "little")
    return {struct.unpack_from("<I", body, 4 + 8 * i)[0]:
            struct.unpack_from("<I", body, 8 + 8 * i)[0] for i in range(n)}


def main():
    print("live websocket RMC probe against %s:%d" % (HOST, PORT))

    # 1. World Council vote, with the exact bytes a client sends
    council_id = 1001
    rpc("VotingServiceProtocol", "Vote_V1",
        struct.pack("<II", council_id, 2))
    counts = u32map(rpc("VotingServiceProtocol", "GetVoteCount_V1",
                        struct.pack("<I", council_id)))
    check("a council vote is counted on the council bars",
          counts.get(2, 0) > 0, counts)
    senate = u32map(rpc("VotingServiceProtocol", "GetVoteCount_V1",
                        struct.pack("<I", 0)))
    check("the senate bars are NOT the council bars", senate != counts,
          (senate, counts))

    # 1b. the council's OWN VOTE must come back in GetVotingInfos (the bug
    #     report: "I vote, restart, and the vote can be done again")
    infos = fields(rpc("VotingServiceProtocol", "GetVotingInfos_V1",
                       bytes.fromhex("010000000b00566f74696e67496e666f000100")))
    n = int.from_bytes(infos[:4], "little")
    docs = {}
    for i in range(n):
        rec = infos[4 + i * 49: 4 + (i + 1) * 49]
        docs[int.from_bytes(rec[0:4], "little")] = {
            "vote_id": int.from_bytes(rec[4:8], "little"),
            "phase": rec[12],
            "own": int.from_bytes(rec[21:25], "little", signed=True)}
    check("GetVotingInfos serves the senate and the World Council",
          set(docs) >= {0, 1}, docs)
    check("the council document keeps its Vote() id 1001",
          docs.get(1, {}).get("vote_id") == council_id, docs.get(1))
    check("the vote phase is open (f3 = 1) so the panel draws the vote UI",
          docs.get(1, {}).get("phase") == 1, docs.get(1))
    check("the council reports OUR vote (own vote = the faction just cast) - "
          "this is what makes it survive a restart",
          docs.get(1, {}).get("own") == 2, docs.get(1))

    # 2. the career document answers for all three factions
    career = u32map(rpc("PlayerProfileServiceProtocol", "GetReputationPoints_V1"))
    check("GetReputationPoints serves the three factions",
          set(career) >= {0, 1, 2}, career)
    check("the council vote paid its faction", career.get(2, 0) >= 50, career)

    # 3. the world event carries the month-long campaign
    ev = fields(rpc("WorldEventServiceProtocol", "GetLatestWorldEvent_V1", b"\x01\x00"))
    f1, f2 = struct.unpack_from("<II", ev, 0)
    off = 8
    slen = struct.unpack_from("<H", ev, off)[0]
    off += 2 + slen                       # f3: String (u16 len, bytes, NUL)
    f4 = struct.unpack_from("<Q", ev, off)[0]
    f5 = ev[off + 8]
    f6, f7 = struct.unpack_from("<II", ev, off + 9)
    check("the event names a real chapter",
          f2 in (3054018, 3054105, 3058000, 3060000, 3062000), f2)
    check("the running flag is 0 (0 == running)", f5 == 0, f5)
    check("the bar is the campaign: 0 < progress <= max",
          0 < f6 <= f7, (f6, f7))
    check("the progress max is the 25,000..125,000 target",
          25000 <= f7 <= 125000, f7)
    year = (f4 >> 26) & 0x3FFF
    check("the packed date decodes to a sane year", 2020 <= year <= 2100, (f4, year))

    print("event chapter %s, worldside %d/%d, date year %d" % (f2, f6, f7, year))
    print("ALL LIVE PROBES PASSED" if not failures else "FAILED: %r" % (failures,))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        print("the local server is not answering on %s:%d (%s)"
              % (HOST, PORT, exc.__class__.__name__))
        print("start PLAY.bat first, then run this again")
        sys.exit(1)
