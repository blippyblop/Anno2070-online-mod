#!/usr/bin/env python3
"""Round-trip tests for the ARK item box, driving the REAL server code.

Everything here goes through ``server/rmc_services.py`` with an in-memory store
(``open_store(None)``): the same handlers the PRUDP responder and the websocket
payload seam run, no stubs, no mocks.  What is proven:

  1. ``GetArkItems_V1`` replies with the store's items as ``u32 count`` plus
     16-byte records, in the documented field order
     ``{socket_quality, socket_index, item_id, count}`` and LITTLE-endian
     (byte-level assertions, not only decoded values).
  2. ``SetArkItems_V1`` stores the request list verbatim and changes what
     ``GetArkItems_V1`` returns afterwards; an empty list clears the box.
  3. ``SetArk_V1``'s documented delta semantics: ``key=(w0,w1)``,
     ``value=(w2,w3)``, merged into the current box, with ``w3 == 0`` meaning
     "skip this record".
  4. Every payload starts with the name block
     ``u16 length INCLUDING the NUL | "Proto::Method*" | NUL``.

Run:
    source ~/tools/env.sh && python3 tools/test_ark_roundtrip.py
    python3 tools/test_ark_roundtrip.py --xfail-known   # ignore the known bug

Exit status is non-zero when any check fails.  The SetArk_V1 delta checks are
expected to FAIL today: ``h_set_ark`` replaces the box with the request list
instead of merging it and does not skip ``w3 == 0`` records (see
decomp/_adhoc/ark-verify.md, "Bugs found").  ``--xfail-known`` downgrades those
checks to XFAIL so the rest can be used as a green gate.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_DIR = os.path.join(ROOT, "server")
for path in (SERVER_DIR, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import ark_selftest as V                    # noqa: E402  (fixtures + validator)
import rmc_services as R                    # noqa: E402  (the real server code)
import rv_shapes as RV                      # noqa: E402

PROTO = "PlayerProfileServiceProtocol"
GET = b"PlayerProfileServiceProtocol::GetArkItems_V1"
SET_ITEMS = b"PlayerProfileServiceProtocol::SetArkItems_V1"
SET_ARK = b"PlayerProfileServiceProtocol::SetArk_V1"

RESULTS = []                                # (status, name, detail)


def check(name, cond, detail="", known_bug=False, xfail_known=False):
    """Record one assertion; ``known_bug`` can be downgraded to XFAIL."""
    if cond:
        RESULTS.append(("PASS", name, ""))
        return True
    if known_bug and xfail_known:
        RESULTS.append(("XFAIL", name, detail))
        return True
    RESULTS.append(("FAIL", name, detail))
    return False


# --------------------------------------------------------------------------
# wire helpers
# --------------------------------------------------------------------------
def split_payload(payload):
    """``(plen, name_bytes_incl_nul, body)`` for a reply payload."""
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise AssertionError("payload is empty/None")
    payload = bytes(payload)
    if len(payload) < 2:
        raise AssertionError("payload shorter than its u16 length: %r" % payload)
    plen = struct.unpack_from("<H", payload, 0)[0]
    if 2 + plen > len(payload):
        raise AssertionError("name block overruns the payload (plen=%d, len=%d)"
                             % (plen, len(payload)))
    return plen, payload[2:2 + plen], payload[2 + plen:]


def decode_ark(body):
    """``(count, [(w0,w1,w2,w3), ...])`` from a ``list<ArkItem>`` body."""
    if len(body) < 4:
        raise AssertionError("ARK body shorter than the u32 count: %r" % body)
    count = struct.unpack_from("<I", body, 0)[0]
    need = 4 + 16 * count
    if len(body) != need:
        raise AssertionError("ARK body is %d bytes, count %d needs %d"
                             % (len(body), count, need))
    return count, [struct.unpack_from("<4I", body, 4 + 16 * i)
                   for i in range(count)]


def ark_body(items):
    """Encode ``[(w0,w1,w2,w3), ...]`` as the request/serializer body."""
    return (struct.pack("<I", len(items))
            + b"".join(struct.pack("<4I", *(int(x) for x in it)) for it in items))


def store_items(store, pid="default"):
    """The store's ArkItems as a list of 4-tuples (dict or list tolerant)."""
    out = []
    for raw in store.player(pid).get("arkItems", []):
        fields, problem = V.coerce_record(raw)
        if fields is None:
            raise AssertionError("stored item is not an ArkItem: %r (%s)"
                                 % (raw, problem))
        out.append(fields)
    return out


def as_tuples(items):
    out = []
    for raw in items:
        fields, problem = V.coerce_record(raw)
        assert fields is not None, (raw, problem)
        out.append(fields)
    return out


def get_payload(store, pid="default"):
    return R.respond_payload(PROTO, "GetArkItems_V1", store, b"")


def get_items(store):
    _plen, _name, body = split_payload(get_payload(store))
    return decode_ark(body)[1]


def apply_delta(current, delta):
    """The documented SetArk_V1 semantics, implemented independently.

    ``key=(w0,w1)``, ``value=(w2,w3)``; ``w3 == 0`` means "skip" (it cannot
    express a deletion on this wire).  Existing sockets are updated in place,
    new keys are appended, so the result is deterministic.
    """
    out = [list(r) for r in current]
    index = {(r[0], r[1]): i for i, r in enumerate(out)}
    for w0, w1, w2, w3 in delta:
        if w3 == 0:
            continue
        key = (w0, w1)
        if key in index:
            out[index[key]] = [w0, w1, w2, w3]
        else:
            index[key] = len(out)
            out.append([w0, w1, w2, w3])
    return [tuple(r) for r in out]


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
def starter_box():
    """The table's recommended starter box (validated before use)."""
    table = V.ArkTable()
    items = as_tuples(table.raw["starter_items"])
    rows, ok, _ = V.validate_items(items, table)
    return items, ok, rows


STARTER, STARTER_OK, STARTER_ROWS = starter_box()
ALT = as_tuples(V.ArkTable().raw["starter_items_alt"])


# --------------------------------------------------------------------------
# 1. GetArkItems_V1 encodes the store exactly
# --------------------------------------------------------------------------
def test_get_encodes_store():
    store = R.open_store(None)
    store.player("default")["arkItems"] = [list(t) for t in STARTER]

    payload = get_payload(store)
    plen, name, body = split_payload(payload)

    check("fixtures: the starter box passes ark_selftest.py", STARTER_OK,
          "; ".join(p for r in STARTER_ROWS for p in r["problems"]))

    check("GetArkItems: payload starts with the name block, '*' and NUL included",
          name == GET + b"*\x00" and plen == len(GET) + 2,
          "plen=%r name=%r" % (plen, name))
    check("GetArkItems: the u16 length INCLUDES the NUL",
          plen == len(name) and name.endswith(b"\x00")
          and struct.unpack_from("<H", payload, 0)[0] == 46,
          "plen=%d len(name)=%d" % (plen, len(name)))
    check("GetArkItems: body is u32 count + 16 B x count",
          len(body) == 4 + 16 * len(STARTER) == 100,
          "%d bytes" % len(body))

    count, decoded = decode_ark(body)
    check("GetArkItems: count equals the store's item count",
          count == len(STARTER) == 6, "count=%d" % count)
    check("GetArkItems: decodes to exactly the store's items", decoded == STARTER,
          "got %r" % (decoded,))

    # byte-level: the first record is A0, id 60530 (0xEC72), count 1
    rec0 = body[4:20]
    check("GetArkItems: first record is 16 bytes, fields in documented order, LE",
          rec0 == struct.pack("<4I", 1, 0, 60530, 1)
          == b"\x01\x00\x00\x00" b"\x00\x00\x00\x00"
             b"\x72\xec\x00\x00" b"\x01\x00\x00\x00",
          rec0.hex())
    check("GetArkItems: id 60530 (0xEC72) goes on the wire as 72 ec 00 00",
          body[4 + 8:4 + 12] == b"\x72\xec\x00\x00",
          body[4 + 8:4 + 12].hex())
    check("GetArkItems: ... and not big-endian (which would be 00 00 ec 72)",
          body[4 + 8:4 + 12] != struct.pack(">I", 60530),
          body[4 + 8:4 + 12].hex())

    # the task brief quotes "12 ec 00 00" next to id 60530; that byte pattern is
    # 0xEC12 = 60434, so pin both, with a record whose four words are distinct
    other = R.open_store(None)
    other.player("default")["arkItems"] = [[2, 1, 60434, 7]]
    _p, _n, other_body = split_payload(get_payload(other))
    check("GetArkItems: id 60434 (0xEC12) is 12 ec 00 00 (the quoted pattern)",
          other_body[4 + 8:4 + 12] == b"\x12\xec\x00\x00",
          other_body[4 + 8:4 + 12].hex())
    check("GetArkItems: all four words distinct prove order + width",
          other_body[4:20] == b"\x02\x00\x00\x00" b"\x01\x00\x00\x00"
                               b"\x12\xec\x00\x00" b"\x07\x00\x00\x00",
          other_body[4:20].hex())

    # state-aware: not the stateless rv_shapes default, and identical to what
    # the PRUDP responder's dispatch returns for the same store
    stateless = RV.build(PROTO, "GetArkItems_V1")
    _p, _n, stateless_body = split_payload(stateless)
    check("GetArkItems: the seam is state-aware (differs from the stateless shape)",
          stateless_body != body and stateless_body == b"\x00\x00\x00\x00",
          "stateless=%r" % (stateless_body,))
    responder = R.RmcResponder(store, lambda *a, **k: None)
    same = responder.dispatch(PROTO + "::GetArkItems_V1", b"", {"pid": "default"})
    check("GetArkItems: seam bytes == RmcResponder.dispatch bytes",
          same == payload, "%r vs %r" % (same and len(same), len(payload)))


# --------------------------------------------------------------------------
# 2. SetArkItems_V1 round trip
# --------------------------------------------------------------------------
def test_set_ark_items_roundtrip():
    store = R.open_store(None)
    store.player("default")["arkItems"] = [list(t) for t in STARTER]

    new = ALT + [(3, 2, 40000088, 1)]          # 6 alt items + one addon id = 7
    body_in = ark_body(new)
    reply = R.respond_payload(PROTO, "SetArkItems_V1", store, body_in)
    plen, name, reply_body = split_payload(reply)

    check("SetArkItems: reply is the name block only (void payload)",
          name == SET_ITEMS + b"*\x00" and plen == len(SET_ITEMS) + 2
          and reply_body == b"", "%r" % (reply,))

    stored = store_items(store)
    check("SetArkItems: the request list is stored verbatim (7 records)",
          stored == new, "stored %r" % (stored,))
    check("SetArkItems: stored as the server's a/b/c/d dicts",
          store.player("default")["arkItems"][0]
          == {"a": new[0][0], "b": new[0][1], "c": new[0][2], "d": new[0][3]},
          repr(store.player("default")["arkItems"][0]))

    _p, gname, got_body = split_payload(get_payload(store))
    check("SetArkItems -> GetArkItems: the box changed to the new list",
          gname == GET + b"*\x00" and got_body == body_in,
          "got %r want %r" % (got_body[:24], body_in[:24]))
    check("SetArkItems -> GetArkItems: decodes to exactly the new items",
          decode_ark(got_body)[1] == new, "got %r" % (decode_ark(got_body)[1],))

    # an explicit empty list clears the box
    reply = R.respond_payload(PROTO, "SetArkItems_V1", store,
                              struct.pack("<I", 0))
    _p, name, empty_body = split_payload(reply)
    check("SetArkItems []: reply is still the name block",
          name == SET_ITEMS + b"*\x00" and empty_body == b"", repr(name))
    check("SetArkItems []: the box is empty afterwards", store_items(store) == [],
          repr(store_items(store)))
    _p, _n, got_body = split_payload(get_payload(store))
    check("SetArkItems [] -> GetArkItems: count 0 and no records",
          got_body == b"\x00\x00\x00\x00", got_body.hex())

    # an EMPTY request body also clears (documented in respond_payload)
    R.respond_payload(PROTO, "SetArkItems_V1", store, body_in)
    check("SetArkItems: box refilled before the empty-body case",
          store_items(store) == new, repr(store_items(store)))
    R.respond_payload(PROTO, "SetArkItems_V1", store, b"")
    check("SetArkItems with an empty body: clears the box (documented write)",
          store_items(store) == [], repr(store_items(store)))

    # and the responder path behaves like the seam
    resp = R.RmcResponder(store, lambda *a, **k: None)
    resp.dispatch(PROTO + "::SetArkItems_V1", body_in, {"pid": "default"})
    check("SetArkItems via RmcResponder: same stored box",
          store_items(store) == new, repr(store_items(store)))


# --------------------------------------------------------------------------
# 3. SetArk_V1 delta semantics (documented)
# --------------------------------------------------------------------------
def test_set_ark_delta(xfail_known=False):
    # (a) merge: one changed socket, the other five survive
    store = R.open_store(None)
    store.player("default")["arkItems"] = [list(t) for t in STARTER]
    delta = [(1, 0, 60136, 1), (3, 2, 60133, 1)]
    reply = R.respond_payload(PROTO, "SetArk_V1", store, ark_body(delta))
    _p, name, reply_body = split_payload(reply)
    check("SetArk_V1: reply is the name block only (void payload)",
          name == SET_ARK + b"*\x00" and reply_body == b"", repr(reply))

    want = apply_delta(STARTER, delta)
    got = store_items(store)
    check("SetArk_V1 merges by (w0,w1): the five untouched sockets survive",
          got == want,
          "got %r\n  want %r\n  (h_set_ark replaces the list instead of "
          "merging -- see decomp/_adhoc/ark-verify.md)" % (got, want),
          known_bug=True, xfail_known=xfail_known)
    check("SetArk_V1: GetArkItems_V1 returns the merged box",
          get_items(store) == want,
          "got %r want %r" % (get_items(store), want),
          known_bug=True, xfail_known=xfail_known)

    # (b) w3 == 0 means "skip", so the record must not change anything
    store = R.open_store(None)
    store.player("default")["arkItems"] = [list(t) for t in STARTER]
    R.respond_payload(PROTO, "SetArk_V1", store, ark_body([(2, 1, 0, 0)]))
    got = store_items(store)
    check("SetArk_V1 with w3 == 0: record skipped, box unchanged",
          got == STARTER,
          "got %r\n  want %r\n  (h_set_ark stores the zero record -- see "
          "decomp/_adhoc/ark-verify.md)" % (got, STARTER),
          known_bug=True, xfail_known=xfail_known)

    # (c) a skip next to a real change: only the real change lands
    store = R.open_store(None)
    store.player("default")["arkItems"] = [list(t) for t in STARTER]
    delta = [(2, 1, 0, 0), (1, 0, 60136, 1)]
    R.respond_payload(PROTO, "SetArk_V1", store, ark_body(delta))
    want = apply_delta(STARTER, delta)
    got = store_items(store)
    check("SetArk_V1: w3 == 0 skips, the w3 == 1 record still applies",
          got == want,
          "got %r\n  want %r" % (got, want),
          known_bug=True, xfail_known=xfail_known)

    # (d) an empty delta is a no-op (this already matches the code)
    store = R.open_store(None)
    store.player("default")["arkItems"] = [list(t) for t in STARTER]
    R.respond_payload(PROTO, "SetArk_V1", store, struct.pack("<I", 0))
    check("SetArk_V1 with no records: leaves the box untouched",
          store_items(store) == STARTER, repr(store_items(store)))

    # (e) a full-box delta equals a snapshot (the case the client exercises)
    store = R.open_store(None)
    store.player("default")["arkItems"] = []
    R.respond_payload(PROTO, "SetArk_V1", store, ark_body(ALT))
    check("SetArk_V1 on an empty box: the records become the box",
          store_items(store) == ALT, repr(store_items(store)))


# --------------------------------------------------------------------------
# 4. name-block invariants (all three methods)
# --------------------------------------------------------------------------
def test_name_blocks():
    store = R.open_store(None)
    bodies = {
        "GetArkItems_V1": b"",
        "SetArkItems_V1": ark_body(ALT),
        "SetArk_V1": ark_body([(1, 0, 60136, 1)]),
    }
    for method, body in bodies.items():
        payload = R.respond_payload(PROTO, method, store, body)
        expected = ("%s::%s*" % (PROTO, method)).encode()
        plen, name, tail = split_payload(payload)
        check("%s: name block has the trailing '*' and a NUL-terminated name"
              % method,
              name == expected + b"\x00" and name.endswith(b"*\x00"),
              repr(name))
        check("%s: u16 length field INCLUDES the NUL" % method,
              plen == len(expected) + 1 == len(name),
              "plen=%d expected=%d" % (plen, len(expected) + 1))
        check("%s: payload begins at offset 0 with that name block" % method,
              payload[:2] == struct.pack("<H", len(expected) + 1)
              and payload[2:2 + plen] == expected + b"\x00",
              payload[:4].hex())
        # trailing '*' can never be part of a request/response field body
        check("%s: no stray name copy in the body" % method,
              expected not in tail, repr(tail[:24]))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--xfail-known", action="store_true",
                   help="downgrade the known SetArk_V1 delta bugs to XFAIL")
    args = p.parse_args(argv)

    print("ARK round-trip tests (real rmc_services handlers, in-memory store)")
    print("  table: %s" % V.DEFAULT_TABLE)

    test_get_encodes_store()
    test_set_ark_items_roundtrip()
    test_set_ark_delta(xfail_known=args.xfail_known)
    test_name_blocks()

    counts = {"PASS": 0, "FAIL": 0, "XFAIL": 0}
    print("\n-- results " + "-" * 60)
    for status, name, detail in RESULTS:
        counts[status] = counts.get(status, 0) + 1
        print("%-5s %s" % (status, name))
        if detail and status != "PASS":
            for line in str(detail).splitlines():
                print("      %s" % line)

    total = sum(counts.values())
    print("\n%d checks: %d PASS, %d FAIL, %d XFAIL"
          % (total, counts["PASS"], counts["FAIL"], counts["XFAIL"]))
    if counts["FAIL"]:
        print("RESULT: FAIL")
        if counts["XFAIL"] == 0:
            print("  (the SetArk_V1 delta failures are a real server bug; "
                  "run with --xfail-known to gate the rest)")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
