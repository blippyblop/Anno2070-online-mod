#!/usr/bin/env python3
"""Tests for ``server/content_events.py`` (world-event CONTENT).

Plain python, no pytest.  Run:

    source ~/tools/env.sh && python3 tools/test_content_events.py

Exit 0 = every check passed, non-zero = at least one failure (and the failing
labels are printed).

What is proven here
-------------------
1. **Packed Date layout, byte level.**  Known-good literals from
   ``decomp/_adhoc/voting-events-content.md`` SS2, the 64-bit little-endian
   image of the value, every bit field extracted by hand (second/min/hour/day/
   month/year), flags in bits 40..63, and that no bit above 63 is set.
2. **Round-trip**, including leap day (2012, 2000), a year rollover
   (1999-12-31 23:59:59 -> 2000-01-01), a post-2038 year, a non-UTC input
   (must normalise to the same instant) and the rejected epoch-seconds trap.
3. **Default event semantics**: ``f5 == 0`` (running, inverted!), the window
   covers "now", progress max is non-zero, ``visited`` is False, the instance
   id is stable and non-zero.
4. **Wire placement.**  The document is encoded *and* decoded by an
   independent re-implementation of the ``WorldEvent`` layout written from the
   ``rv_shapes/voting.py`` docstring (never by calling its helpers), so every
   value provably lands in the slot the client reads.  As a cross-check the
   real builder ``rv_shapes.voting.get_latest_world_event()`` is also required
   to produce byte-identical output for the same dict -- that is what makes
   this module a drop-in.
5. **Missions**: ``complete_mission`` returns the u32 the client stores,
   records once, is idempotent per (mission, player), appends for a different
   player / different mission, and the helpers stay consistent (record count ==
   stats counter == per-player sum).
6. **Hygiene**: standard library only, no import-time side effects, no import
   of ``rv_shapes`` / ``content_voting`` / ``mimic_server``, deterministic
   output, junk input never raises.
"""

import inspect
import io
import json
import os
import struct
import sys
import tokenize
import types
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import content_events as ce  # noqa: E402

FAILURES = []
CHECKS = 0
PASSED = 0


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def check(cond, label):
    global CHECKS, PASSED
    CHECKS += 1
    if cond:
        PASSED += 1
    else:
        FAILURES.append(label)
        print("  FAIL %s" % label)


def eq(actual, expected, label):
    global CHECKS, PASSED
    CHECKS += 1
    if actual == expected:
        PASSED += 1
    else:
        FAILURES.append(label)
        print("  FAIL %s" % label)
        print("    expected %r" % (expected,))
        print("    actual   %r" % (actual,))


def raises(exc, fn, label):
    try:
        fn()
    except exc:
        check(True, label)
        return
    except Exception as other:               # noqa: BLE001 - report the type
        check(False, "%s (raised %r instead of %s)" % (label, other, exc))
        return
    check(False, "%s (did not raise)" % label)


# --------------------------------------------------------------------------
# INDEPENDENT re-implementation of the two encodings
#
# Written from the docstrings/field tables, NOT from the modules under test:
#   * packed Anno Date: voting-events-content.md SS2 (masks, bit order)
#   * WorldEvent wire: rv_shapes/voting.py's world_event() docstring:
#       u32, u32, String, u64, bool, u32, u32, bool
#     Quazal String = u16 length EXCLUDING the NUL, then the bytes.
# --------------------------------------------------------------------------

def ref_pack_date(y, mo, d, h=0, mi=0, s=0, flags=0):
    v = y & 0x3FFF
    v = (v << 4) | (mo & 0xF)
    v = (v << 5) | (d & 0x1F)
    v = (v << 5) | (h & 0x1F)
    v = (v << 6) | (mi & 0x3F)
    v = (v << 6) | (s & 0x3F)
    return v | (flags << 40)


def ref_unpack_date(value):
    sec = value & 0x3F
    minute = (value >> 6) & 0x3F
    hour = (value >> 12) & 0x1F
    day = (value >> 17) & 0x1F
    month = (value >> 22) & 0xF
    year = (value >> 26) & 0x3FFF
    return datetime(year, month, day, hour, minute, sec, tzinfo=timezone.utc)


def ref_encode_world_event(doc):
    """The WorldEvent record, field by field (no rv_shapes involved)."""
    name = doc["name"].encode("ascii")
    return b"".join([
        struct.pack("<I", int(doc["a"])),                     # f1
        struct.pack("<I", int(doc["b"])),                     # f2
        struct.pack("<H", len(name) + 1), name, b"\x00",      # f3 String (incl NUL)
        struct.pack("<Q", int(doc["start"])),                 # f4 packed date
        b"\x01" if doc["running"] else b"\x00",               # f5 bool, 1 byte
        struct.pack("<I", int(doc["f"])),                     # f6
        struct.pack("<I", int(doc["g"])),                     # f7
        b"\x01" if doc["visited"] else b"\x00",               # f8 bool, 1 byte
    ])


def ref_decode_world_event(buf):
    """Cursor-based decode; asserts the record ends exactly at len(buf)."""
    pos = 0

    def take(n):
        nonlocal pos
        if pos + n > len(buf):
            raise AssertionError("short read: wanted %d at %d of %d"
                                 % (n, pos, len(buf)))
        chunk = buf[pos:pos + n]
        pos += n
        return chunk

    def u32():
        return struct.unpack_from("<I", take(4))[0]

    def u64():
        return struct.unpack_from("<Q", take(8))[0]

    def string():
        # engine String: the u16 count INCLUDES the terminating NUL
        n = struct.unpack_from("<H", take(2))[0]
        raw = take(n)
        return raw[:-1].rstrip(b"\x00").decode("ascii")

    def boolean():
        raw = take(1)
        if raw not in (b"\x00", b"\x01"):
            raise AssertionError("bool byte is not 0/1: %r" % (raw,))
        return raw == b"\x01"

    out = {
        "f1": u32(), "f2": u32(), "f3": string(), "f4": u64(),
        "f5": boolean(), "f6": u32(), "f7": u32(), "f8": boolean(),
    }
    if pos != len(buf):
        raise AssertionError("%d trailing byte(s) after the record"
                             % (len(buf) - pos,))
    return out


def ref_name_block(proto, method):
    nm = ("%s::%s*" % (proto, method)).encode("ascii") + b"\x00"
    return struct.pack("<H", len(nm)) + nm


def _code_only(src):
    """Source with comments and string literals removed (docstrings included).

    The hygiene checks must look at *code*: mentioning ``rv_shapes`` in prose
    to say "we must not import it" is the opposite of a violation.
    """
    pieces = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        pieces.append(tok.string)
    return " ".join(pieces)


# --------------------------------------------------------------------------
# 1 + 2: the packed date
# --------------------------------------------------------------------------

def test_packed_date_layout():
    print("[1] packed Anno Date: bit layout / byte image / known literals")

    # Literals straight out of the report (SS2 "Sanity values"), which were
    # round-tripped through the client's own getters.
    eq(ce.packed_date(datetime(2012, 6, 15, 12, 30, 0)), 0x0000001F719EC780,
       "2012-06-15 12:30:00 == 0x0000001F719EC780")
    eq(ce.packed_date(datetime(2026, 11, 5, 18, 0, 0)), 0x0000001FAACB2000,
       "2026-11-05 18:00:00 == 0x0000001FAACB2000")
    eq(ce.packed_date(datetime(2030, 1, 1, 0, 0, 0)), 0x0000001FB8420000,
       "2030-01-01 00:00:00 == 0x0000001FB8420000")

    # Independent reference implementation agrees for a spread of values
    # (the 3rd is deliberately awkward: 23:59:59, single-digit fields).
    for y, mo, d, h, mi, s in ((2012, 6, 15, 12, 30, 0), (2026, 11, 5, 18, 0, 0),
                               (2001, 2, 3, 23, 59, 59), (1999, 12, 31, 0, 0, 1),
                               (3000, 1, 1, 0, 0, 0), (1601, 1, 1, 0, 0, 0)):
        eq(ce.packed_date(datetime(y, mo, d, h, mi, s)),
           ref_pack_date(y, mo, d, h, mi, s),
           "reference encoder agrees for %04d-%02d-%02d %02d:%02d:%02d"
           % (y, mo, d, h, mi, s))

    value = ce.packed_date(datetime(2012, 6, 15, 12, 30, 47))
    raw = struct.pack("<Q", value)
    eq(len(raw), 8, "packed date is 8 bytes on the wire (u64)")
    eq(raw, struct.pack("<Q", 0x0000001F719EC7AF),
       "byte image of 2012-06-15 12:30:47 is little-endian 0x...C7AF")

    # Field-by-field extraction from the 64-bit image.
    eq(value & 0x3F, 47, "bits 0-5  = second")
    eq((value >> 6) & 0x3F, 30, "bits 6-11 = minute")
    eq((value >> 12) & 0x1F, 12, "bits 12-16 = hour")
    eq((value >> 17) & 0x1F, 15, "bits 17-21 = day")
    eq((value >> 22) & 0x0F, 6, "bits 22-25 = month")
    eq((value >> 26) & 0x3FFF, 2012, "bits 26-39 = year (4-digit, not years-since-1900)")
    eq(value >> 40, 0, "bits 40-63 = 0 flags by default")
    check(value <= 0xFFFFFFFFFFFFFFFF, "value fits in u64")
    eq(value >> 40, (value & 0xFFFFFF0000000000) >> 40,
       "flags region is exactly bits 40..63")

    # Flags are preserved and do not disturb the date fields.
    flagged = ce.packed_date(datetime(2012, 6, 15, 12, 30, 47), flags=0xABCDEF)
    eq(flagged >> 40, 0xABCDEF, "flags land in bits 40..63")
    eq(flagged & 0xFFFFFFFFFF, value & 0xFFFFFFFFFF,
       "flags do not change bits 0..39")
    eq(ce.unpack_date(flagged), ce.unpack_date(value),
       "unpack ignores the flags region (like _mkgmtime64)")

    # The trap: epoch seconds must not masquerade as a date.
    raises(TypeError, lambda: ce.packed_date(1762363200),
           "epoch seconds are rejected (the SS2 trap)")
    # Field extraction only -- this value is not a legal date at all, which is
    # exactly the point: epoch seconds decode to year 26 / month 4 / day 5 /
    # hour 24 (the client normalises that into a 1926-ish or blank date).
    bogus = 1762363200
    eq((bogus >> 26) & 0x3FFF, 26, "epoch 1762363200 'decodes' as year 26")
    eq((bogus >> 22) & 0x0F, 4, "epoch 1762363200 'decodes' as month 4")
    eq((bogus >> 17) & 0x1F, 5, "epoch 1762363200 'decodes' as day 5")
    eq((bogus >> 12) & 0x1F, 24, "epoch 1762363200 'decodes' as hour 24 (illegal)")
    check(((bogus >> 26) & 0x3FFF) != 2026,
          "epoch seconds are nowhere near the intended year (the SS2 trap)")


def test_packed_date_roundtrip():
    print("[2] packed Anno Date: round-trip (leap day, rollover, >2038)")

    cases = [
        datetime(2012, 2, 29, 0, 0, 0),        # leap day
        datetime(2012, 2, 29, 23, 59, 59),     # leap day, last second
        datetime(2000, 2, 29, 12, 0, 0),       # 2000 is a leap year (400 rule)
        datetime(1999, 12, 31, 23, 59, 59),    # year rollover, before
        datetime(2000, 1, 1, 0, 0, 0),         # year rollover, after
        datetime(2026, 11, 5, 19, 33, 0),      # the current test-day stamp
        datetime(2038, 1, 19, 3, 14, 8),       # past the 32-bit epoch limit
        datetime(2100, 12, 31, 23, 59, 59),    # NOT a leap year, far future
        datetime(1601, 1, 1, 0, 0, 0),         # MSVC _mkgmtime64 lower bound
    ]
    for dt in cases:
        value = ce.packed_date(dt)
        eq(ce.unpack_date(value), dt.replace(tzinfo=timezone.utc),
           "round-trip %s" % dt.isoformat())
        eq(ref_unpack_date(value), ce.unpack_date(value),
           "independent decoder agrees on %s" % dt.isoformat())

    # Leap day is actually distinguishable from 1 March.
    check(ce.packed_date(datetime(2012, 2, 29)) != ce.packed_date(datetime(2012, 3, 1)),
          "2012-02-29 and 2012-03-01 encode differently")
    # ...and a rollover lands on day 1 of month 1 of the next year.
    over = ref_unpack_date(ce.packed_date(datetime(1999, 12, 31, 23, 59, 59)))
    eq((over.year, over.month, over.day), (1999, 12, 31),
       "rollover: the last second still decodes as 1999-12-31")
    over_next = ref_unpack_date(
        ce.packed_date(datetime(1999, 12, 31, 23, 59, 59) + timedelta(seconds=1)))
    eq((over_next.year, over_next.month, over_next.day), (2000, 1, 1),
       "rollover: the next second decodes as 2000-01-01")

    # Accepted input forms all mean the same instant (UTC by default).
    eq(ce.packed_date(datetime(2026, 11, 5, 19, 0, 0)),
       ce.packed_date(datetime(2026, 11, 5, 20, 0, 0,
                               tzinfo=timezone(timedelta(hours=1)))),
       "naive input is UTC, aware input is converted")
    eq(ce.packed_date(datetime(2026, 11, 5)),
       ce.packed_date(datetime(2026, 11, 5, 0, 0, 0)),
       "date input means midnight UTC")
    eq(ce.packed_date((2026, 11, 5, 19, 33, 0)),
       ce.packed_date(datetime(2026, 11, 5, 19, 33, 0)),
       "tuple input is accepted")
    eq(ce.packed_date("2026-11-05T19:33:00Z"),
       ce.packed_date(datetime(2026, 11, 5, 19, 33, 0)),
       "ISO-8601 string input is accepted")

    # Values the client's masks would silently mangle are refused.
    raises(ValueError, lambda: ce.packed_date(datetime(2026, 13, 1)),
           "month 13 rejected")
    raises(ValueError, lambda: ce.packed_date(datetime(2026, 1, 32)),
           "day 32 rejected")
    raises(ValueError, lambda: ce.packed_date((2026, 1, 1, 24)),
           "hour 24 rejected")
    raises(ValueError, lambda: ce.packed_date((2026, 1, 1, 0, 0, 60)),
           "second 60 rejected")
    raises(ValueError, lambda: ce.packed_date(datetime(1500, 1, 1)),
           "year 1500 rejected (outside mkgmtime64's range)")
    raises(ValueError, lambda: ce.packed_date(datetime(2026, 1, 1), flags=1 << 24),
           "flags wider than 24 bits rejected")


# --------------------------------------------------------------------------
# 3: the default event document
# --------------------------------------------------------------------------

NOW = datetime(2026, 11, 5, 19, 33, 0, tzinfo=timezone.utc)
WIRE_KEYS = ["a", "b", "name", "start", "running", "f", "g", "visited"]


def test_default_event():
    print("[3] default event: keys, inverted f5, window covers now, progress")

    doc = ce.latest_event(now=NOW)

    eq(list(doc)[:8], WIRE_KEYS,
       "dict key order is the wire field order the builders read")
    check(isinstance(doc, dict), "latest_event returns a plain dict")
    check(json.loads(json.dumps(doc)) == doc,
          "document survives a JSON round-trip unchanged (persistable)")

    eq(doc["running"], False, "f5 == False  => 0 on the wire => event RUNNING")
    check(doc["a"] != 0, "f1 instance id is non-zero")
    eq(doc["a"], ce.latest_event(now=NOW + timedelta(days=3))["a"],
       "instance id is stable across calls / windows")
    eq(doc["b"], 0, "f2 mission-set index is 0 (only justified value, see note)")
    check(isinstance(doc["name"], str) and doc["name"],
          "f3 name is a non-empty string (dropped by the client, harmless)")
    eq(doc["f"], 0, "f6 progress value starts at 0")
    check(doc["g"] > 0, "f7 progress max is non-zero (no divide-by-zero bar)")
    check(doc["f"] <= doc["g"], "progress value <= max (bar not past 100%)")
    eq(doc["visited"], False, "f8 visited is False (badge shown once)")

    # The single wire date is a *packed* date inside a window covering now.
    wire = ref_unpack_date(doc["start"])
    window_start = ref_unpack_date(doc["windowStart"])
    window_end = ref_unpack_date(doc["windowEnd"])
    check(window_start <= NOW <= window_end,
          "window covers now (%s <= %s <= %s)"
          % (window_start.isoformat(), NOW.isoformat(), window_end.isoformat()))
    eq(wire, window_end if ce.F4_IS_EVENT_END else window_start,
       "wire f4 is the documented bound of the window (F4_IS_EVENT_END=%r)"
       % (ce.F4_IS_EVENT_END,))
    check(wire.year == 2026,
          "decoded f4 is a sane year, not 1926/1970 (packed, not epoch)")
    check(wire > NOW, "the displayed date is in the future relative to now")

    # Same ``now`` -> identical document (deterministic, no hidden clock use).
    eq(ce.latest_event(now=NOW), doc, "latest_event is deterministic for a fixed now")

    # Default ``now`` is the real clock: window still covers it.
    live = ce.latest_event()
    check(ref_unpack_date(live["windowStart"]) <= datetime.now(timezone.utc)
          <= ref_unpack_date(live["windowEnd"]),
          "with now=None the window covers the wall clock")


def test_event_overrides():
    print("[3b] override handling (store plug-in path) is total")

    doc = ce.latest_event(now=NOW, event={"visited": True, "b": 3})
    eq(doc["visited"], True, "event override: visited")
    eq(doc["b"], 3, "event override: mission-set index")
    eq(doc["a"], ce.EVENT_INSTANCE_ID, "untouched fields keep their default")

    # SetVisited on the store dict is what the handler would call.
    state = {}
    eq(ce.set_visited(state), True, "set_visited returns the new flag")
    eq(ce.latest_event(now=NOW, event=state)["visited"], True,
       "a store dict with visited=True shows up as f8")
    ce.set_visited(state, False)
    eq(ce.latest_event(now=NOW, event=state)["visited"], False,
       "set_visited(False) clears f8")

    # Junk must not raise -- a corrupted store cannot kill the session.
    for junk in (None, 42, "x", [1, 2, 3], {"a": "nope", "b": None,
                                           "name": object(), "start": "??",
                                           "f": [], "g": {}, "visited": "yes"}):
        try:
            out = ce.latest_event(now=NOW, event=junk)
            ok = isinstance(out, dict) and len(out) >= 8
        except Exception as exc:                     # noqa: BLE001
            ok = False
            print("    raised for %r: %r" % (junk, exc))
        check(ok, "latest_event tolerates event=%r" % (junk,))

    # A datetime override for the packed field is encoded, an int is taken as
    # already packed.
    when = datetime(2030, 1, 1, tzinfo=timezone.utc)
    eq(ce.latest_event(now=NOW, event={"start": when})["start"],
       ce.packed_date(when), "datetime override for f4 is encoded")
    eq(ce.latest_event(now=NOW, event={"start": 12345})["start"], 12345,
       "int override for f4 is passed through as packed")

    # `now` may be epoch seconds (a clock value) ...
    eq(ce.latest_event(now=int(NOW.timestamp())), ce.latest_event(now=NOW),
       "now may be epoch seconds as well as a datetime")
    # ... but the legacy store key `active` (True == up) must be inverted.
    eq(ce.latest_event(now=NOW, event={"active": True})["running"], False,
       "legacy active=True -> wire f5 False -> running")
    eq(ce.latest_event(now=NOW, event={"active": False})["running"], True,
       "legacy active=False -> wire f5 True -> 'no event running'")
    eq(ce.latest_event(now=NOW, event={"active": True, "running": True})["running"],
       True, "explicit 'running' wins over the legacy 'active' key")


# --------------------------------------------------------------------------
# 4: the values really land where the client reads them
# --------------------------------------------------------------------------

def test_wire_placement():
    print("[4] independent encode + decode: values land in the client's slots")

    doc = ce.latest_event(now=NOW)
    blob = ref_encode_world_event(doc)
    eq(len(blob), 4 + 4 + 2 + len(doc["name"]) + 1 + 8 + 1 + 4 + 4 + 1,
       "record length = 4+4+(2+len+1)+8+1+4+4+1 (no padding, bools are 1 byte)")

    got = ref_decode_world_event(blob)
    eq(got["f1"], doc["a"], "f1 = instance id")
    eq(got["f1"], ce.EVENT_INSTANCE_ID, "f1 == EVENT_INSTANCE_ID")
    eq(got["f2"], 0, "f2 = 0 -> mission set index 0")
    eq(got["f3"], doc["name"], "f3 String round-trips")
    eq(got["f4"], doc["start"], "f4 = the packed date, unshifted (u64 slot)")
    eq(got["f5"], False, "f5 decodes False -> client IsWorldEventRunning() True")
    eq(got["f6"], 0, "f6 = progress value 0")
    eq(got["f7"], 100, "f7 = progress max 100")
    eq(got["f8"], False, "f8 = visited False")
    # And the packed date in that slot still decodes to a real date.
    check(ref_unpack_date(got["f4"]).year == 2026,
          "f4 as the client decodes it is 2026, not a 1926 epoch artefact")


def test_dropin_matches_rv_shapes():
    print("[4b] drop-in: the real builder emits the same bytes for our dict")

    try:
        from rv_shapes import voting as rv_voting
    except Exception as exc:                          # noqa: BLE001
        check(False, "rv_shapes.voting importable (%r)" % (exc,))
        return

    doc = ce.latest_event(now=NOW)
    expected = (ref_name_block("WorldEventServiceProtocol",
                               "GetLatestWorldEvent_V1")
                + ref_encode_world_event(doc))
    eq(rv_voting.get_latest_world_event(doc), expected,
       "rv_shapes get_latest_world_event(latest_event()) is byte-identical")
    eq(rv_voting.world_event(doc), ref_encode_world_event(doc),
       "rv_shapes world_event(doc) matches the independent encoder")

    # The same must hold for a store-shaped override dict, not just defaults.
    state = {}
    ce.complete_mission(state, 4, "Goose_1762363980", now=NOW)
    ce.set_visited(state)
    overridden = ce.latest_event(now=NOW, event=state)
    eq(rv_voting.world_event(overridden), ref_encode_world_event(overridden),
       "override dict also encodes identically")

    # Every key the builder reads is present with the expected type.
    for key in WIRE_KEYS:
        check(key in doc, "key %r present for the builder" % key)
    check(not isinstance(doc["running"], int) or isinstance(doc["running"], bool),
          "f5 is a bool (the builder writes its truthiness into 1 byte)")


# --------------------------------------------------------------------------
# 5: missions and stats
# --------------------------------------------------------------------------

def test_complete_mission():
    print("[5] complete_mission: reply u32, one record, idempotent per pair")

    state = {}
    reply = ce.complete_mission(state, 4, "Goose_1762363980", now=NOW)
    eq(reply, 0, "reply is the u32 the client stores (0)")
    check(isinstance(reply, int) and not isinstance(reply, bool),
          "reply is a plain int (encoded with rv_shapes.u32)")
    eq(reply, ce.COMPLETE_MISSION_REPLY, "reply == COMPLETE_MISSION_REPLY")

    records = state["completedMissions"]
    eq(len(records), 1, "one completion recorded")
    eq(records[0]["a"], "Goose", "player parsed out of '<Name>_<unixtime>'")
    eq(records[0]["id"], 4, "mission index stored")
    eq(records[0]["b"], "Goose_1762363980",
       "instance name kept verbatim for the wire String")
    eq(records[0]["ts"], int(NOW.timestamp()), "timestamp recorded")

    # Idempotent per (mission, player) even with a different instance string.
    eq(ce.complete_mission(state, 4, "Goose_1762399999", now=NOW), 0,
       "duplicate call still replies 0")
    eq(len(state["completedMissions"]), 1,
       "duplicate (mission, player) does NOT append")

    # ...but a different mission or a different player does.
    ce.complete_mission(state, 5, "Goose_1762399999", now=NOW)
    eq(len(state["completedMissions"]), 2, "different mission appends")
    ce.complete_mission(state, 4, "Ada_1762363980", now=NOW)
    eq(len(state["completedMissions"]), 3, "different player appends")

    # The records are exactly the three wire fields (plus bookkeeping ts).
    wire = ce.completed_missions(state)
    eq(wire[0], {"a": "Goose", "id": 4, "b": "Goose_1762363980"},
       "completed_missions() emits {a, id, b} only")
    eq([r["id"] for r in wire], [4, 5, 4], "order preserved (oldest first)")
    eq([r["a"] for r in ce.completed_missions(state, player="Goose")],
       ["Goose", "Goose"], "player filter (GetOwn...) works")
    eq(ce.completed_missions(state, player="Nobody"), [],
       "unknown player -> empty list (count 0, not an error)")

    # The completed-mission record also encodes the documented way.
    entry = wire[0]
    raw = (struct.pack("<H", len(entry["a"])) + entry["a"].encode("ascii")
           + struct.pack("<I", entry["id"])
           + struct.pack("<H", len(entry["b"])) + entry["b"].encode("ascii"))
    eq(len(raw), 2 + 5 + 4 + 2 + 16,
       "WorldEventCompletedMission wire size = (2+name)+4+(2+instance)")

    # Totality: junk state / junk ids must not raise.
    for junk_state in (None, 5, "state", [1, 2]):
        try:
            ok = ce.complete_mission(junk_state, 1, "x_1762363980") == 0
        except Exception as exc:                     # noqa: BLE001
            ok = False
            print("    raised for state=%r: %r" % (junk_state, exc))
        check(ok, "complete_mission tolerates state=%r" % (junk_state,))
    eq(ce.complete_mission({}, "not-a-number", None), 0,
       "junk mission id / name -> still replies 0")
    eq(ce.completed_missions(None), [], "completed_missions(None) -> []")

    # No duplicate-record growth across a JSON persist/load cycle.
    state2 = json.loads(json.dumps(state))
    ce.complete_mission(state2, 4, "Goose_1762363980", now=NOW)
    eq(len(state2["completedMissions"]), 3,
       "reload from JSON then re-complete does not duplicate")


def test_event_stats():
    print("[5b] event_stats: consistent with the completion records")

    state = {}
    ce.complete_mission(state, 4, "Goose_1762363980", now=NOW)
    ce.complete_mission(state, 5, "Goose_1762363980", now=NOW)
    ce.complete_mission(state, 4, "Ada_1762363980", now=NOW)
    state["reputation"] = {"Goose": 12, "Ada": 3}
    state["registered"] = [111]

    stats = ce.event_stats(state)
    eq(stats["c1"], 3, "c1 == number of completion records")
    eq(stats["c1"], len(ce.completed_missions(state)),
       "stat counter agrees with completed_missions()")
    eq(sum(e["a"] for e in stats["listA"]), stats["c1"],
       "listA counts sum to the completion counter")
    eq([e["name"] for e in stats["listA"]], ["Goose", "Ada"],
       "listA is one entry per player, best points first (stable reply bytes)")
    eq([e["b"] for e in stats["listA"]], [2, 1], "listA holds per-player counts")
    eq([(e["name"], e["a"]) for e in stats["listB"]],
       [("Goose", 12), ("Ada", 3)], "listB mirrors the reputation map, best first")
    eq(stats["c0"], sum(e["a"] for e in stats["listA"]),
       "c0 is the points total (documented guess over the three counters)")
    eq(stats["c2"], len(state.get("registered") or []),
       "c2 is the number of registered sessions")
    check(all(set(e) == {"name", "a", "b", "c"} for e in
              stats["listA"] + stats["listB"]),
          "every stats entry has exactly the four wire fields")
    check(json.loads(json.dumps(stats)) == stats, "stats document is persistable")

    empty = ce.event_stats({})
    eq(empty["listA"], [], "empty store -> empty list A")
    eq(len(empty["listB"]), 0, "empty store -> empty list B")
    eq((empty["c0"], empty["c1"], empty["c2"]), (0, 0, 0),
       "empty store -> zero counters")
    eq(ce.event_stats(None), empty, "None state behaves like an empty store")

    # And the pieces really encode: two lists, then three u32.
    try:
        from rv_shapes import voting as rv_voting
        payload = rv_voting.get_stats(stats)
        body = payload[len(ref_name_block("WorldEventServiceProtocol",
                                          "GetStats_V1")):]
        expect = (struct.pack("<I", 2)
                  + b"".join(struct.pack("<H", len(e["name"]) + 1)
                             + e["name"].encode("ascii") + b"\x00"
                             + struct.pack("<III", e["a"], e["b"], e["c"])
                             for e in stats["listA"])
                  + struct.pack("<I", 2)
                  + b"".join(struct.pack("<H", len(e["name"]) + 1)
                             + e["name"].encode("ascii") + b"\x00"
                             + struct.pack("<III", e["a"], e["b"], e["c"])
                             for e in stats["listB"])
                  + struct.pack("<III", stats["c0"], stats["c1"], stats["c2"]))
        eq(body, expect, "event_stats() encodes as list,list,u32,u32,u32")
    except Exception as exc:                          # noqa: BLE001
        check(False, "GetStats_V1 encoding check ran (%r)" % (exc,))


# --------------------------------------------------------------------------
# 6: catalogue note + hygiene
# --------------------------------------------------------------------------

def test_catalogue_note():
    print("[6] mission-set index: documented, not invented")

    eq(ce.MISSION_SET_INDEX, 0, "exactly one event, index 0")
    eq(ce.latest_event(now=NOW)["b"], 0, "the document uses that index")
    note = ce.MISSION_CATALOGUE_INDEX_NOTE
    check(isinstance(note, str) and len(note) > 100, "the note exists and is substantial")
    for needle in ("DAT_141ba3660", ".rda", "No mission GUID found",
                   "f2", "enumera"):
        check(needle in note, "note mentions %r" % needle)
    check("1, 2, 3" not in note and "0, 1, 2" not in note,
          "the note does not fabricate an index list")


def test_hygiene():
    print("[6b] hygiene: stdlib only, no side effects, no cross-imports")

    path = os.path.join(ROOT, "server", "content_events.py")
    with open(path, "r", encoding="utf-8") as handle:
        src = handle.read()

    imports = [ln.strip() for ln in src.splitlines()
               if ln.strip().startswith(("import ", "from "))]
    code = _code_only(src)
    eq(sorted(imports), sorted([
        "from datetime import date as _date",
        "from datetime import datetime, timedelta, timezone",
        "from collections.abc import Mapping",
    ]), "only stdlib imports: %r" % (imports,))
    for banned in ("rv_shapes", "content_voting", "mimic_server", "socket",
                   "open(", "import json", "requests"):
        check(banned not in code, "no %r in the module's code" % banned)
    check("rv_shapes" in src and "content_voting" in src,
          "the docstring does name the modules we must not import (context)")

    # Import-time side effects: the module must not have executed anything that
    # touched a file/socket.  Re-import in a fresh namespace and compare the
    # public constants to prove determinism, then check that no module-level
    # call result was cached from the clock.
    spec = __import__("importlib.util", fromlist=["util"]).spec_from_file_location(
        "content_events_reimport", path)
    fresh = __import__("importlib.util", fromlist=["util"]).module_from_spec(spec)
    spec.loader.exec_module(fresh)
    eq(fresh.EVENT_INSTANCE_ID, ce.EVENT_INSTANCE_ID,
       "re-import yields identical constants")
    eq(fresh.latest_event(now=NOW), ce.latest_event(now=NOW),
       "re-import produces the same document (no cached state)")
    check(fresh.complete_mission({}, 1, "a_1762363980") == 0,
          "mission state lives in the caller's dict, not in the module")
    leftovers = [(k, type(v).__name__) for k, v in vars(ce).items()
                 if not k.startswith("__")
                 and not isinstance(v, (types.FunctionType, types.ModuleType,
                                        str, int, bool, float, tuple,
                                        frozenset, timedelta))
                 and not inspect.isclass(v)]
    eq(leftovers, [], "module namespace holds only constants/callables "
                      "(no mutable module-level store)")

    # Calling twice with the same inputs must be byte-stable.
    eq(ref_encode_world_event(ce.latest_event(now=NOW)),
       ref_encode_world_event(ce.latest_event(now=NOW)),
       "repeat calls produce identical bytes")


# --------------------------------------------------------------------------

def main():
    for test in (test_packed_date_layout, test_packed_date_roundtrip,
                 test_default_event, test_event_overrides,
                 test_wire_placement, test_dropin_matches_rv_shapes,
                 test_complete_mission, test_event_stats,
                 test_catalogue_note, test_hygiene):
        test()

    print("\nPASS %d   FAIL %d   (%d checks)" % (PASSED, len(FAILURES), CHECKS))
    if FAILURES:
        print("FAILED:")
        for label in FAILURES:
            print("  - %s" % label)
        return 1
    print("ALL WORLD-EVENT CONTENT CHECKS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
