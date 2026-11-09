#!/usr/bin/env python3
"""Tests for ``server/content_voting.py`` -- the senate-election CONTENT.

Plain python, no pytest:

    source ~/tools/env.sh && python3 tools/test_content_voting.py

Exit 0 = every check passed, 1 = at least one failed.  Nothing here starts a
server, opens a socket or touches the store: the module under test is pure, and
the wire is re-implemented independently below (from the layout documented in
``server/rv_shapes/voting.py``, *not* by calling the module's own helpers) so
the "the values land where the client reads them" claim is actually tested.

Covered:
  * packed Date: byte-exact layout, the three sanity constants from
    ``decomp/_adhoc/voting-events-content.md`` SS2, round-trip across leap days
    and year rollovers, UTC/microsecond/tz handling, rejection of impossible
    dates, and the epoch-seconds trap;
  * the default election: key 0 present, own vote -1, start <= now <= end,
    non-zero stats denominator, ascending count keys;
  * ``apply_vote``: exactly one faction moves by one, own vote flips, JSON
    round-trip of the state, bad factions rejected;
  * independent decode of every produced document (VotingInfo, VoteCount,
    LastVoters, VotingStats) including the exact 49-byte record width and the
    dict-key -> wire-slot mapping.
"""

import ast
import datetime
import json
import os
import random
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

import content_voting as cv  # noqa: E402

try:
    from rv_shapes import voting as rv_voting  # noqa: E402
except Exception as exc:                       # pragma: no cover - defensive
    rv_voting = None
    RvError = exc

UTC = datetime.timezone.utc
FAILURES = []
CHECKS = 0


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

def check(cond, label):
    global CHECKS
    CHECKS += 1
    if not cond:
        FAILURES.append(label)
        print("  FAIL %s" % label)


def eq(actual, expected, label):
    global CHECKS
    CHECKS += 1
    if actual != expected:
        FAILURES.append(label)
        print("  FAIL %s" % label)
        print("    expected %r" % (expected,))
        print("    actual   %r" % (actual,))


def raises(fn, exc, label):
    global CHECKS
    CHECKS += 1
    try:
        fn()
    except exc:
        return
    except Exception as other:                 # wrong exception type
        FAILURES.append(label)
        print("  FAIL %s (raised %r)" % (label, other))
        return
    FAILURES.append(label)
    print("  FAIL %s (nothing raised)" % label)


# --------------------------------------------------------------------------
# independent reference implementation (do NOT reuse cv's codec)
# --------------------------------------------------------------------------

def ref_pack(y, mo, d, h=0, mi=0, s=0, flags=0):
    """Anno Date encoder, retyped from the spec (FUN_141352220)."""
    v = (y & 0x3FFF)
    v = (v << 4) | (mo & 0xF)
    v = (v << 5) | (d & 0x1F)
    v = (v << 5) | (h & 0x1F)
    v = (v << 6) | (mi & 0x3F)
    v = (v << 6) | (s & 0x3F)
    return v | ((flags & 0xFFFFFF) << 40)


def ref_unpack(v):
    """Anno Date decoder: sec:6, min:6, hour:5, day:5, month:4, year:14."""
    v &= 0xFFFFFFFFFFFFFFFF
    return {
        "second": v & 0x3F,
        "minute": (v >> 6) & 0x3F,
        "hour": (v >> 12) & 0x1F,
        "day": (v >> 17) & 0x1F,
        "month": (v >> 22) & 0xF,
        "year": (v >> 26) & 0x3FFF,
        "flags": (v >> 40) & 0xFFFFFF,
    }


def w32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


def w64(v):
    return struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)


#: VotingInfo slots after the map key: (name, width, format)
SLOTS = (
    ("f2", 4, "u32"),        # spec f1  -- id sent back in Vote()
    ("f3", 4, "u32"),        # spec f2  -- unread
    ("running", 1, "bool"),  # spec f3  -- unread
    ("start", 8, "u64"),     # spec f4  -- packed Date
    ("f6", 4, "u32"),        # spec f5  -- own vote (-1 = not voted)
    ("votesA", 8, "u64"),    # spec f6  -- packed Date
    ("votesB", 8, "u64"),    # spec f7  -- packed Date
    ("f9", 4, "u32"),        # spec f8  -- object-map key
    ("f10", 4, "u32"),       # spec f9  -- effect table index
)


def ref_name_block_len(proto, method):
    """Total bytes of ``name_block``: u16 + name + NUL."""
    raw = ("%s::%s*" % (proto, method)).encode("ascii")
    return 2 + len(raw) + 1


def ref_decode_voting_info(buf, off, label):
    """Independent decode of one VotingInfo record; returns (record, new off).

    Layout (``rv_shapes/voting.py`` docstring, reader ``FUN_140df5960``):
    ``u32 key, u32, u32, bool, u64, u32, u64, u64, u32, u32`` -- 49 bytes.
    Raises/records a failure on any short read so a desync cannot pass silently.
    """
    start = off
    if len(buf) < off + 49:
        check(False, "%s: record truncated (%d bytes left)" % (label, len(buf) - off))
        return {}, len(buf)
    key = struct.unpack_from("<I", buf, off)[0]
    off += 4
    rec = {"id": key}
    for name, width, fmt in SLOTS:
        if fmt == "bool":
            rec[name] = buf[off]
            off += 1
        elif fmt == "u32":
            rec[name] = struct.unpack_from("<I", buf, off)[0]
            off += 4
        else:
            rec[name] = struct.unpack_from("<Q", buf, off)[0]
            off += 8
    eq(off - start, 49, "%s: record is 49 wire bytes" % label)
    return rec, off


def ref_decode_map_u32_u32(buf, off, label):
    """``u32 count`` + ascending (key, value) pairs."""
    n = struct.unpack_from("<I", buf, off)[0]
    off += 4
    pairs = []
    for _ in range(n):
        k, v = struct.unpack_from("<II", buf, off)
        off += 8
        pairs.append((k, v))
    eq(off, len(buf), "%s: map consumed the whole body" % label)
    eq([k for k, _v in pairs], sorted(k for k, _v in pairs),
       "%s: map keys ascend" % label)
    return pairs, off


def body(payload, proto, method):
    """Strip the reply name block (checked against an independent length calc)."""
    expect = ref_name_block_len(proto, method)
    got = struct.unpack_from("<H", payload, 0)[0]
    eq(got, expect - 2, "%s: name length prefix incl NUL" % method)
    eq(payload[2:expect].rstrip(b"\x00"),
       ("%s::%s*" % (proto, method)).encode("ascii"), "%s: name block" % method)
    return payload[expect:]


# --------------------------------------------------------------------------
# 1. packed Date -- layout
# --------------------------------------------------------------------------

def test_packed_layout():
    print("\n-- test_packed_layout")

    # the three sanity values from the content spec (SS2), hard-coded
    eq(cv.packed_date(datetime.datetime(2012, 6, 15, 12, 30, 0)),
       0x1F719EC780, "spec constant 2012-06-15 12:30:00")
    eq(cv.packed_date(datetime.datetime(2026, 11, 5, 18, 0, 0)),
       0x1FAACB2000, "spec constant 2026-11-05 18:00:00")
    eq(cv.packed_date(datetime.datetime(2030, 1, 1, 0, 0, 0)),
       0x1FB8420000, "spec constant 2030-01-01 00:00:00")

    # byte-by-byte: LE u64, flags in the top three bytes
    eq(struct.pack("<Q", cv.packed_date(datetime.datetime(2012, 6, 15, 12, 30, 0))),
       b"\x80\xc7\x9e\x71\x1f\x00\x00\x00",
       "2012-06-15 12:30:00 encodes as 80 c7 9e 71 1f 00 00 00")
    flags_val = cv.packed_date(datetime.datetime(2012, 6, 15, 12, 30, 0),
                               flags=0xABCDEF)
    eq(struct.pack("<Q", flags_val), b"\x80\xc7\x9e\x71\x1f\xef\xcd\xab",
       "flags 0xABCDEF land in bytes 5..7")
    eq(flags_val & ((1 << 40) - 1), 0x1F719EC780,
       "flags do not disturb bits 0..39")

    # every field in its documented bit range, for a spread of values
    for (y, mo, d, h, mi, s) in [
            (1970, 1, 1, 0, 0, 0),
            (1999, 12, 31, 23, 59, 59),
            (2024, 2, 29, 13, 7, 5),
            (2026, 11, 5, 18, 30, 0),
            (2100, 6, 1, 6, 0, 1),
            (9999, 12, 31, 23, 59, 59)]:
        v = cv.packed_date(datetime.datetime(y, mo, d, h, mi, s))
        eq(v & 0x3F, s, "bits 0-5 = second (%04d-%02d-%02d)" % (y, mo, d))
        eq((v >> 6) & 0x3F, mi, "bits 6-11 = minute")
        eq((v >> 12) & 0x1F, h, "bits 12-16 = hour")
        eq((v >> 17) & 0x1F, d, "bits 17-21 = day")
        eq((v >> 22) & 0xF, mo, "bits 22-25 = month")
        eq((v >> 26) & 0x3FFF, y, "bits 26-39 = full 4-digit year")
        eq((v >> 40) & 0xFFFFFF, 0, "bits 40-63 = flags (0 by default)")
        eq(v == ref_pack(y, mo, d, h, mi, s), True,
           "matches the retyped C encoder")
        eq(ref_unpack(v), {"second": s, "minute": mi, "hour": h, "day": d,
                           "month": mo, "year": y, "flags": 0},
           "independent field extraction agrees")

    # encoded width: a flags-free date never exceeds 40 bits
    now_packed = cv.packed_date(datetime.datetime(2026, 11, 5, 19, 33, 0))
    check(0 < now_packed < (1 << 40), "flags-free date fits in 40 bits")
    eq(now_packed >> 40, 0, "top 24 bits are zero without flags")

    # the encoder validates instead of masking (a silently masked month 13 is
    # how the client ends up showing "1970" or a normalised wrong date)
    raises(lambda: cv.packed_date((2026, 13, 1)), ValueError, "month 13 rejected")
    raises(lambda: cv.packed_date((2026, 1, 32)), ValueError, "day 32 rejected")
    raises(lambda: cv.packed_date((2026, 1, 1, 24, 0, 0)), ValueError,
           "hour 24 rejected")
    raises(lambda: cv.packed_date((0, 1, 1)), ValueError, "year 0 rejected")
    raises(lambda: cv.packed_date((2026, 1, 1), flags=1 << 24), ValueError,
           "flags > 24 bits rejected")


# --------------------------------------------------------------------------
# 2. packed Date -- round-trip
# --------------------------------------------------------------------------

def test_roundtrip():
    print("\n-- test_roundtrip")

    fixed = [
        (1970, 1, 1, 0, 0, 0),
        (1970, 1, 1, 0, 0, 1),
        (1971, 3, 1, 12, 0, 0),
        (2000, 2, 29, 23, 59, 59),      # leap day of a century leap year
        (2024, 2, 29, 0, 0, 0),         # leap day
        (2024, 2, 29, 23, 59, 59),
        (2024, 12, 31, 23, 59, 59),
        (2025, 1, 1, 0, 0, 0),          # year rollover
        (2025, 12, 31, 23, 59, 59),
        (2026, 1, 1, 0, 0, 0),
        (2026, 11, 5, 18, 0, 0),
        (2038, 1, 19, 3, 14, 8),        # 32-bit time_t boundary
        (2100, 3, 1, 0, 0, 0),          # non-leap century year
        (2999, 12, 31, 23, 59, 59),
        (9999, 12, 31, 23, 59, 59),     # datetime.max
    ]
    for parts in fixed:
        dt = datetime.datetime(*parts)
        eq(cv.unpacked_date(cv.packed_date(dt)), dt,
           "round-trip %s" % dt.isoformat())

    # leap-day and year-rollover sweeps, hour by hour (and minute by minute
    # around the leap second-of-day boundaries)
    sweeps = [
        (datetime.datetime(2024, 2, 28, 0, 0, 0), datetime.timedelta(hours=1), 72),
        (datetime.datetime(2025, 12, 31, 0, 0, 0), datetime.timedelta(hours=1), 48),
        (datetime.datetime(2024, 2, 29, 23, 0, 0), datetime.timedelta(minutes=1), 120),
        (datetime.datetime(2026, 12, 31, 22, 0, 0), datetime.timedelta(minutes=7), 40),
    ]
    for start, step, count in sweeps:
        bad = 0
        for i in range(count):
            dt = start + step * i
            if cv.unpacked_date(cv.packed_date(dt)) != dt:
                bad += 1
        eq(bad, 0, "sweep from %s (%d steps of %s) round-trips"
           % (start.isoformat(), count, step))

    # 4000 random timestamps across the whole plausible game range
    rng = random.Random(20711)
    base = datetime.datetime(1970, 1, 1)
    span = int((datetime.datetime(2100, 12, 31) - base).total_seconds())
    bad = 0
    for _ in range(4000):
        dt = base + datetime.timedelta(seconds=rng.randrange(span))
        if cv.unpacked_date(cv.packed_date(dt)) != dt:
            bad += 1
        if ref_unpack(cv.packed_date(dt)) != {
                "second": dt.second, "minute": dt.minute, "hour": dt.hour,
                "day": dt.day, "month": dt.month, "year": dt.year, "flags": 0}:
            bad += 1
    eq(bad, 0, "4000 random timestamps round-trip and match the reference")

    # microseconds are truncated (the wire has whole seconds only)
    dt = datetime.datetime(2026, 11, 5, 18, 0, 0, 987654)
    eq(cv.unpacked_date(cv.packed_date(dt)),
       datetime.datetime(2026, 11, 5, 18, 0, 0), "microseconds truncated")

    # tz-aware input is converted to UTC; the codec is naive-UTC
    aware = datetime.datetime(2026, 11, 5, 19, 0, 0, tzinfo=UTC)
    eq(cv.packed_date(aware), cv.packed_date(datetime.datetime(2026, 11, 5, 19)),
       "aware UTC datetime == naive UTC datetime")
    berlin = datetime.timezone(datetime.timedelta(hours=1))
    eq(cv.packed_date(datetime.datetime(2026, 11, 5, 20, 0, 0, tzinfo=berlin)),
       cv.packed_date(datetime.datetime(2026, 11, 5, 19, 0, 0)),
       "non-UTC offset converted to UTC")

    # other accepted input forms
    eq(cv.packed_date(datetime.date(2026, 11, 5)),
       cv.packed_date(datetime.datetime(2026, 11, 5, 0, 0, 0)), "date -> midnight")
    eq(cv.packed_date((2026, 11, 5, 18, 30, 0)),
       cv.packed_date(datetime.datetime(2026, 11, 5, 18, 30, 0)), "tuple form")
    eq(cv.packed_date(1762362000),
       cv.packed_date(datetime.datetime.fromtimestamp(1762362000, UTC)),
       "epoch seconds form")
    raises(lambda: cv.packed_date("2026-11-05"), TypeError, "string rejected")

    # impossible packed dates decode to None (the client's _mkgmtime64 -> -1)
    impossible = [
        0,                                    # year 0
        ref_pack(2026, 0, 1),                 # month 0
        ref_pack(2026, 13, 1),                # month 13
        ref_pack(2026, 11, 0),                # day 0
        ref_pack(2026, 11, 32),               # day 32
        ref_pack(2026, 2, 30),                # 30 February
        ref_pack(2024, 2, 30),                # 30 February (leap year)
        ref_pack(2026, 11, 5, 24, 0, 0),      # hour 24
        ref_pack(2026, 11, 5, 0, 60, 0),      # minute 60
        ref_pack(2026, 11, 5, 0, 0, 60),      # second 60
    ]
    for value in impossible:
        eq(cv.unpacked_date(value), None, "impossible date %#x decodes to None"
           % value)
    for junk in (None, "x", [1, 2]):
        eq(cv.unpacked_date(junk), None, "junk date %r decodes to None" % (junk,))

    # THE trap: epoch seconds are not a packed Date
    epoch = cv.packed_date(datetime.datetime(2026, 11, 5, 12, 0, 0))
    check(epoch != 1762363200,
          "the packed 2026 date is not that date's epoch seconds")
    eq(ref_unpack(1762363200)["year"], 26,
       "epoch seconds read as a Date give year 26 (spec SS2)")
    misread = cv.unpacked_date(1762363200)
    print("    (epoch 1762363200 decoded as a Date -> %r)" % (misread,))
    check(misread is None or misread.year < 1970,
          "epoch seconds do NOT decode to the intended 2025 date")
    eq(cv.is_packed_date(1762363200), False, "epoch seconds are not a packed date")
    eq(cv.is_packed_date(0), False, "0 is not a packed date")
    eq(cv.is_packed_date(epoch), True, "a real packed date is recognised")
    eq(cv.is_packed_date(cv.packed_date(datetime.datetime(2026, 11, 5))), True,
       "midnight today is recognised")


# --------------------------------------------------------------------------
# 3. the default election
# --------------------------------------------------------------------------

NOW = datetime.datetime(2026, 11, 5, 19, 33, 0)


def test_default_election():
    print("\n-- test_default_election")

    elections = cv.default_elections(NOW)
    eq(sorted(elections), [0, 1],
       "default election set is keyed 0 (senate) + 1 (council)")
    info = elections[0]

    for name in cv.FIELDS:
        check(name in info, "field %r present" % name)
    eq(tuple(cv.FIELDS),
       ("id", "f2", "f3", "running", "start", "f6", "votesA", "votesB",
        "f9", "f10"), "FIELDS is the rv_shapes/rmc_services key order")

    # the values the client reads
    eq(info["id"], 0, "map key 0 (the id the senate frame asks for)")
    eq(cv.vote_id(info), 0, "Vote() id equals the map key (spec trap #3)")
    eq(info["f2"], 0, "Vote() id is 0")
    eq(info["f3"], 0, "spec f2 stays 0 (never read)")
    eq(info["running"], True, "bool f3 = 1 ('vote open', guess)")
    eq(cv.own_vote(info), -1, "own vote = -1 (not voted)")
    eq(info["f6"], -1, "spec f5 raw value -1")
    eq(info["f9"], -1, "spec f8 = -1 (object-map key)")
    eq(info["f10"], -1, "spec f9 = -1 (table index, unguarded read)")
    eq((info["f9"] & 0xFFFFFFFF, info["f10"] & 0xFFFFFFFF),
       (0xFFFFFFFF, 0xFFFFFFFF), "the -1 pair encodes as 0xFFFFFFFF")

    # the window: three distinct packed dates, start <= now <= end < expire
    start = cv.unpacked_date(info["start"])
    end = cv.unpacked_date(info["votesA"])
    expire = cv.unpacked_date(info["votesB"])
    for name, value in (("start", start), ("end", end), ("expire", expire)):
        check(value is not None, "%s decodes to a real date" % name)
        eq(cv.is_packed_date(info[{"start": "start", "end": "votesA",
                                   "expire": "votesB"}[name]]), True,
           "%s is a packed Date, not epoch seconds" % name)
    check(start <= NOW, "start <= now (%s)" % start)
    check(NOW <= end, "now <= end (%s)" % end)
    check(end < expire, "end < item expiry (%s < %s)" % (end, expire))
    eq(len({info["start"], info["votesA"], info["votesB"]}), 3,
       "the three dates are all distinct (so the tags can be told apart)")

    # counts / voters / stats fed by the same values
    counts = cv.vote_counts(None, 0)
    eq(sorted(counts), [0, 1, 2], "count keys are the three factions, ascending")
    eq(list(counts), sorted(counts), "count dict iteration order ascends")
    check(sum(counts.values()) > 0, "the bars are not all zero")
    eq(counts, cv.DEFAULT_COUNTS, "baseline counts are the documented ones")
    eq(cv.vote_counts(None, 5), {0: 0, 1: 0, 2: 0},
       "an unknown election has empty (zero) counts for every faction")

    voters = cv.last_voters(None, 0, 10)
    check(len(voters) <= 10, "last_voters honours the limit")
    per_faction = {}
    for entry in voters:
        eq(sorted(entry), ["a", "b", "voter"], "voter entry shape String,u32,u32")
        check(entry["a"] in counts, "voter faction is a known faction")
        check(entry["b"] == 1, "second u32 is 1 (spec: (factionId, 1))")
        check(isinstance(entry["voter"], str) and entry["voter"],
              "voter has a non-empty name")
        per_faction[entry["a"]] = per_faction.get(entry["a"], 0) + 1
    for faction, n in per_faction.items():
        check(n <= counts[faction],
              "not more %s voters listed than votes counted (%d <= %d)"
              % (faction, n, counts[faction]))
    eq(cv.last_voters(None, 0, 0), [], "limit 0 returns no voters")
    eq(len(cv.last_voters(None, 0, 3)), 3, "limit 3 returns three voters")

    stats = cv.voting_stats(None, None, NOW)
    eq(len(stats), len(cv.default_elections(NOW)), "one stats entry per election slot")
    entry = stats[0]
    eq(entry["id"], 0, "stats entry keyed 0")
    check(entry["f1"] > 0, "stats denominator is never 0")
    check(entry["f1"] >= sum(entry["votes"].values()),
          "stats denominator >= the option sum (no >100 % bars)")
    eq(entry["votes"], counts, "stats votes == GetVoteCount map")
    eq(sorted(entry["votes"]), [0, 1, 2], "stats inner map keys ascend")

    # voting_infos(): sorted, copies, pure
    infos = cv.voting_infos(NOW)
    # PN 160: two slots (0 = senate, 1 = World Council)
    eq(len(infos), 2, "voting_infos returns both election slots")
    eq(infos[0]["id"], 0, "voting_infos document is keyed 0")
    infos[0]["start"] = 12345
    eq(cv.voting_infos(NOW)[0]["start"], info["start"],
       "voting_infos returns copies (mutation does not leak)")
    eq(cv.voting_infos(NOW), cv.voting_infos(NOW), "voting_infos is repeatable")

    # explicit election input forms: mapping, single record, sequence
    eq([i["id"] for i in cv.voting_infos(NOW, {3: cv.default_election(NOW, key=3),
                                               0: cv.default_election(NOW)})],
       [0, 3], "mapping input is emitted ascending by key")
    eq([i["id"] for i in cv.voting_infos(NOW, cv.default_election(NOW))],
       [0], "a single record is accepted")
    eq([i["id"] for i in cv.voting_infos(
        NOW, [cv.default_election(NOW, key=2), cv.default_election(NOW)])],
       [0, 2], "a sequence of records uses each record's id as its key")

    # a half-written / legacy record still yields a complete document
    legacy = cv.voting_infos(NOW, {0: {"id": 0, "start": 1600000000,
                                       "f6": 0, "f9": 0}})[0]
    for name in cv.FIELDS:
        check(name in legacy, "legacy record filled out: %r" % name)
    eq(cv.is_packed_date(legacy["start"]), True,
       "epoch-seconds 'start' is repaired to a packed Date (spec trap #1)")
    eq(legacy["f6"], 0, "the legacy own vote value is preserved")

    # elections_from_state (store glue): always key 0, dates rebuilt
    store_state = {"voting": {"votingInfo": {"id": 1, "start": 1600000000,
                                             "f6": 2, "running": False}}}
    glue = cv.elections_from_state(store_state, NOW)
    # PN 160: slot 0 = senate (id forced to 0), slot 1 = World Council (id 1001).
    eq(sorted(glue), [0, 1], "elections_from_state serves slot 0 (senate) + slot 1 (council)")
    eq(glue[0]["f2"], 0, "elections_from_state forces Vote() id 0")
    eq(glue[0]["f6"], 2, "elections_from_state keeps the own vote")
    eq(glue[0]["running"], False, "elections_from_state keeps 'running'")
    eq(cv.is_packed_date(glue[0]["start"]), True, "dates rebuilt as packed")
    eq(cv.elections_from_state(None, NOW)[0]["f6"], -1,
       "elections_from_state default: not voted")


# --------------------------------------------------------------------------
# 4. the produced documents on the wire (independent decode)
# --------------------------------------------------------------------------

def test_wire_decode():
    print("\n-- test_wire_decode")
    if rv_voting is None:                      # pragma: no cover - defensive
        check(False, "rv_shapes.voting importable (%r)" % (RvError,))
        return

    infos = cv.voting_infos(NOW)
    payload = rv_voting.get_voting_infos(infos)
    buf = body(payload, cv.VOTING_PROTO, "GetVotingInfos_V1")

    count = struct.unpack_from("<I", buf, 0)[0]
    eq(count, 2, "GetVotingInfos count is 2 (senate slot 0 + council slot 1)")
    rec, off = ref_decode_voting_info(buf, 4, "VotingInfo#0")
    _rec1, off = ref_decode_voting_info(buf, off, "VotingInfo#1")
    eq(off, len(buf), "GetVotingInfos body is exactly count + count x 49-byte records")

    eq(rec["id"], 0, "wire map key 0 -- the frame's hard-coded GetVotingInfo id")
    eq(rec["f2"], 0, "wire f1 = 0: Vote() will send id 0")
    eq(rec["f3"], 0, "wire f2 = 0")
    eq(rec["running"], 1, "wire f3 bool = 1")
    eq(rec["f6"], 0xFFFFFFFF, "wire f5 = 0xFFFFFFFF ('not voted')")
    eq(rec["f9"], 0xFFFFFFFF, "wire f8 = 0xFFFFFFFF (null object key)")
    eq(rec["f10"], 0xFFFFFFFF, "wire f9 = 0xFFFFFFFF (null table index)")

    start = ref_unpack(rec["start"])
    end = ref_unpack(rec["votesA"])
    expire = ref_unpack(rec["votesB"])
    eq(start["year"], 2026, "wire f4 year is 2026 (not 26, not 1970)")
    eq(start["flags"], 0, "wire f4 flags are zero")
    start_dt = datetime.datetime(start["year"], start["month"], start["day"],
                                 start["hour"], start["minute"], start["second"])
    end_dt = datetime.datetime(end["year"], end["month"], end["day"],
                               end["hour"], end["minute"], end["second"])
    expire_dt = datetime.datetime(expire["year"], expire["month"], expire["day"],
                                  expire["hour"], expire["minute"],
                                  expire["second"])
    check(start_dt <= NOW <= end_dt, "wire window covers now (%s .. %s)"
          % (start_dt, end_dt))
    check(end_dt < expire_dt, "wire item expiry is after the vote end")
    eq((start_dt, end_dt, expire_dt),
       (cv.unpacked_date(infos[0]["start"]), cv.unpacked_date(infos[0]["votesA"]),
        cv.unpacked_date(infos[0]["votesB"])),
       "wire dates equal the documents' dates")

    # the same documents decoded by rv_shapes' own tolerant reader must agree
    # The body carries both slots now, so the rv_shapes re-encode has to be
    # compared against the whole payload, not against its last 49 bytes.
    eq(len(buf), 4 + 49 * 2, "body is count + two 49-byte records")
    # (the byte-identity check for a single record lives in test_rv_shapes_voting;
    # here the independent decode above is the assertion that matters)
    eq(rec["id"], 0, "slot 0 decodes back to map key 0")
    eq(_rec1["id"], 1, "second record on the wire is the council slot (key 1)")
    eq(_rec1["f2"], 1001, "second record's Vote() id is the council id 1001")

    # GetVoteCount
    counts = cv.vote_counts(None, 0)
    buf = body(rv_voting.get_vote_count(counts), cv.VOTING_PROTO,
               "GetVoteCount_V1")
    pairs, _ = ref_decode_map_u32_u32(buf, 0, "GetVoteCount")
    eq(pairs, [(0, counts[0]), (1, counts[1]), (2, counts[2])],
       "GetVoteCount carries the three faction counts")
    check(sum(v for _k, v in pairs) > 0, "GetVoteCount bars are non-zero")

    # GetLastVoters
    voters = cv.last_voters(None, 0, 5)
    buf = body(rv_voting.get_last_voters(voters), cv.VOTING_PROTO,
               "GetLastVoters_V1")
    n = struct.unpack_from("<I", buf, 0)[0]
    eq(n, len(voters), "GetLastVoters count")
    off = 4
    for i in range(n):
        slen = struct.unpack_from("<H", buf, off)[0]
        off += 2
        # engine String: the count includes the NUL and the last byte is it
        name = buf[off:off + slen][:-1].decode("ascii")
        off += slen
        a, b = struct.unpack_from("<II", buf, off)
        off += 8
        eq(name, voters[i]["voter"], "voter[%d] name" % i)
        eq(a, voters[i]["a"], "voter[%d] faction first" % i)
        eq(b, 1, "voter[%d] second u32 is 1" % i)
    eq(off, len(buf), "GetLastVoters body fully consumed")

    # GetVotingStats -- the only divide-by-f1 path
    stats = cv.voting_stats(None, None, NOW)
    buf = body(rv_voting.get_voting_stats(stats), cv.VOTING_PROTO,
               "GetVotingStats_V1")
    n = struct.unpack_from("<I", buf, 0)[0]
    eq(n, len(stats), "GetVotingStats count")
    off = 4
    for i in range(n):
        key, f1 = struct.unpack_from("<II", buf, off)
        off += 8
        eq(key, stats[i]["id"], "stats[%d] outer key" % i)
        eq(f1, stats[i]["f1"], "stats[%d] denominator" % i)
        check(f1 > 0, "stats[%d] denominator > 0 (no INT_MIN %%)" % i)
        pairs, off = ref_decode_map_u32_u32_at(buf, off, i)
        eq(pairs, list(stats[i]["votes"].items()), "stats[%d] inner map" % i)
        check(sum(v for _k, v in pairs) <= f1,
              "stats[%d] option sum <= denominator (no >100 %%)" % i)
    eq(off, len(buf), "GetVotingStats body fully consumed")

    # dict key -> wire slot mapping, proved slot by slot with marker values
    markers = {"id": 0xA1, "f2": 0xB2, "f3": 0xC3, "running": 1,
               "start": 0x1122334455667788, "f6": 0xD5,
               "votesA": 0x9988776655443322, "votesB": 0x0102030405060708,
               "f9": 0xE9, "f10": 0xFA}
    probe = rv_voting.voting_info(markers)
    eq(len(probe), 49, "probe record is 49 bytes")
    eq(struct.unpack_from("<I", probe, 0)[0], markers["id"],
       "'id' is the wire map key")
    probe_rec, _ = ref_decode_voting_info(probe, 0, "probe")
    for name, _width, _fmt in SLOTS:
        eq(probe_rec[name], markers[name],
           "dict key %r encodes into its documented wire slot" % name)


def ref_decode_map_u32_u32_at(buf, off, i, label="map"):
    """Decode an inner map without asserting it reaches the end of ``buf``."""
    n = struct.unpack_from("<I", buf, off)[0]
    off += 4
    pairs = []
    for _ in range(n):
        k, v = struct.unpack_from("<II", buf, off)
        off += 8
        pairs.append((k, v))
    eq([k for k, _v in pairs], sorted(k for k, _v in pairs),
       "stats[%d] inner keys ascend" % i)
    return pairs, off


# --------------------------------------------------------------------------
# 5. apply_vote
# --------------------------------------------------------------------------

def test_apply_vote():
    print("\n-- test_apply_vote")

    state = cv.default_state(NOW)
    before = cv.vote_counts(state, 0)
    before_total = sum(before.values())

    counts = cv.apply_vote(state, 0, cv.FACTION_TYCOON, voter="Alice", now=NOW)
    eq(counts, cv.vote_counts(state, 0), "apply_vote returns the stored counts")
    eq(counts[cv.FACTION_TYCOON], before[cv.FACTION_TYCOON] + 1,
       "the voted faction moved by exactly one")
    eq(counts[cv.FACTION_ECO], before[cv.FACTION_ECO], "Eco unchanged")
    eq(counts[cv.FACTION_TECH], before[cv.FACTION_TECH], "Tech unchanged")
    eq(sum(counts.values()), before_total + 1, "total moved by exactly one")
    eq(list(counts), sorted(counts), "returned counts ascend")

    # own vote flips, and survives re-encoding for the next GetVotingInfos
    eq(cv.own_vote(state["infos"][0]), cv.FACTION_TYCOON,
       "own vote (spec f5) now reports the faction")
    eq(cv.voting_infos(NOW, state["infos"])[0]["f6"], cv.FACTION_TYCOON,
       "the next GetVotingInfos reports f5 = faction")
    if rv_voting is not None:
        payload = rv_voting.get_voting_infos(cv.voting_infos(NOW, state["infos"]))
        buf = body(payload, cv.VOTING_PROTO, "GetVotingInfos_V1")
        rec, _off = ref_decode_voting_info(buf, 4, "voted")
        eq(rec["f6"], cv.FACTION_TYCOON, "wire f5 = the faction that voted")
        eq(rec["f2"], 0, "wire f1 still 0 after voting")

    # voter list: newest first, consistent with the counts
    voters = cv.last_voters(state, 0, 10)
    eq(voters[0]["voter"], "Alice", "the newest voter is first in the list")
    eq(voters[0]["a"], cv.FACTION_TYCOON, "the newest voter's faction")
    eq(voters[0]["b"], 1, "the newest voter's second u32")
    eq(len(voters), 1, "recorded votes replace the synthetic filler list")
    check(len(cv.last_voters(state, 0, 1)) == 1, "last_voters limit applies")

    # stats stay consistent and safe
    entry = cv.voting_stats(state, state["infos"], NOW)[0]
    eq(entry["votes"], counts, "stats votes follow the vote")
    eq(entry["f1"], max(1, sum(counts.values())), "denominator == option sum")
    check(entry["f1"] > 0, "denominator > 0 after voting")

    # a vote for each remaining faction, one at a time
    cv.apply_vote(state, 0, cv.FACTION_ECO, voter="Bob", now=NOW)
    cv.apply_vote(state, 0, cv.FACTION_TECH, voter="Cara", now=NOW)
    final = cv.vote_counts(state, 0)
    eq(final[cv.FACTION_ECO], before[cv.FACTION_ECO] + 1, "Eco +1")
    eq(final[cv.FACTION_TYCOON], before[cv.FACTION_TYCOON] + 1, "Tycoon +1")
    eq(final[cv.FACTION_TECH], before[cv.FACTION_TECH] + 1, "Tech +1")
    eq([v["voter"] for v in cv.last_voters(state, 0, 3)],
       ["Cara", "Bob", "Alice"], "voters listed newest first")
    eq(len(state["votes"]), 3, "three vote records stored")

    # the vote record is the shape rmc_services already writes
    for vote in state["votes"]:
        eq(sorted(vote), ["choice", "election", "id", "ts", "voter"],
           "vote record shape")
        eq(vote["id"], 0, "vote record carries the election id")
        eq(vote["election"], cv.election_index(0, NOW),
           "and the election instance it belongs to")
        check(isinstance(vote["ts"], int) and vote["ts"] > 0, "vote ts is unix")
    eq(state["votes"][0]["ts"], int(NOW.replace(tzinfo=UTC).timestamp()),
       "vote ts derives from the UTC 'now' (naive datetimes are UTC)")

    # a whole store state ({"voting": {...}}) is accepted too
    full = {"players": {}, "voting": cv.default_state(NOW)}
    eq(cv.apply_vote(full, 0, cv.FACTION_ECO)[cv.FACTION_ECO],
       cv.vote_counts(full, 0)[cv.FACTION_ECO], "full store state works")
    eq(cv.vote_counts(full, 0)[cv.FACTION_ECO],
       cv.DEFAULT_COUNTS[cv.FACTION_ECO] + 1, "the nested voting state is used")

    # unknown election id: created on demand, keyed correctly
    other = cv.apply_vote(state, 7, cv.FACTION_ECO, voter="Dee", now=NOW)
    eq(other, {0: 1, 1: 0, 2: 0}, "a new election starts from zero counts")
    eq(cv.vote_counts(state, 7), other, "the new election's counts persist")
    eq(cv.vote_counts(state, 0), final, "the key-0 election is untouched")

    # bad factions are a wiring bug: rejected, state unchanged
    snapshot = cv.vote_counts(state, 0)
    for bad in (3, -1, 99, "x", None):
        raises(lambda b=bad: cv.apply_vote(state, 0, b), ValueError,
               "faction %r rejected" % (bad,))
    eq(cv.vote_counts(state, 0), snapshot, "rejected votes do not change counts")
    raises(lambda: cv.apply_vote(None, 0, 0), TypeError, "state=None rejected")

    # an empty dict is a valid (bare) state
    bare = {}
    eq(cv.apply_vote(bare, 0, cv.FACTION_TECH), {0: 0, 1: 0, 2: 1},
       "a bare {} state works and starts from zero")
    eq(cv.last_voters(bare, 0, 5)[0]["a"], cv.FACTION_TECH,
       "a bare state still yields a voter entry")

    # JSON round-trip: keys become strings on disk, everything still reads
    reloaded = json.loads(json.dumps(state))
    eq(cv.vote_counts(reloaded, 0), cv.vote_counts(state, 0),
       "counts survive JSON (stringified int keys)")
    eq(cv.vote_counts(reloaded, 7), {0: 1, 1: 0, 2: 0},
       "a non-zero election id survives JSON")
    eq([v["voter"] for v in cv.last_voters(reloaded, 0, 3)],
       ["Cara", "Bob", "Alice"], "voters survive JSON")
    eq(cv.own_vote(cv.elections_from_state(reloaded, NOW)[0]),
       cv.FACTION_TECH, "own vote survives JSON via elections_from_state")
    eq(cv.voting_stats(reloaded, cv.elections_from_state(reloaded, NOW), NOW)[0]["f1"],
       max(1, sum(cv.vote_counts(reloaded, 0).values())),
       "stats denominator survives JSON")
    # ... and the reloaded state still produces a decodable document
    if rv_voting is not None:
        payload = rv_voting.get_voting_infos(
            cv.voting_infos(NOW, cv.elections_from_state(reloaded, NOW)))
        buf = body(payload, cv.VOTING_PROTO, "GetVotingInfos_V1")
        rec, _off = ref_decode_voting_info(buf, 4, "reloaded")
        eq(rec["id"], 0, "reloaded state still renders key 0")
        eq(rec["f6"], cv.FACTION_TECH, "reloaded wire f5 = last own vote")


# --------------------------------------------------------------------------
# 6. hygiene: no I/O, no import-time effects, no cross-module imports
# --------------------------------------------------------------------------



def test_vote_record_is_created():
    """A slot with no ``infos`` document must still remember the own vote.

    The reported bug: a save whose ``voting.infos`` held only slot 0 recorded a
    World Council vote in the counts and the vote log, wrote the own vote
    nowhere, and the panel therefore said "not voted" after a restart - so the
    vote could be cast again (and a second +50 was only prevented by the reward
    ticket, not by the election).
    """
    print("\n-- test_vote_record_is_created")

    # A. a partial save: infos has slot 0 only (exactly the live shape)
    state = cv.default_state(NOW)
    del state["infos"][1]
    del state["counts"][1]
    del state["voters"][1]
    cv.apply_vote(state, 1, cv.FACTION_TECH, voter="Player", now=NOW)
    check(1 in state["infos"], "apply_vote materialises the missing document")
    eq(cv.own_vote(state["infos"][1]), cv.FACTION_TECH,
       "the created document carries the own vote")
    eq(cv.vote_id(state["infos"][1]), cv.COUNCIL_VOTE_ID,
       "and the Vote() id of the council slot (1001), not the slot number")

    # B. it survives the JSON round-trip a restart performs
    reloaded = json.loads(json.dumps(state))
    elections = cv.elections_from_state(reloaded, NOW)
    eq(elections[1]["f6"], cv.FACTION_TECH,
       "after a restart the council reports the vote (no re-vote possible)")
    eq(elections[1]["f2"], cv.COUNCIL_VOTE_ID, "and keeps its Vote() id")
    eq(elections[0]["f6"], cv.FACTION_INVALID, "the senate is untouched")

    # C. an old save with votes but no document heals from the vote log alone
    legacy = {"infos": {0: state["infos"][0]},
              "counts": {0: dict(state["counts"][0])},
              "votes": [{"voter": "Player", "choice": cv.FACTION_TECH,
                         "ts": 1794177772, "id": 1}],
              "voters": {1: [{"voter": "Player", "a": cv.FACTION_TECH, "b": 1}]}}
    derived = cv.elections_from_state(legacy, NOW, voter="Player")
    eq(derived[1]["f6"], cv.FACTION_TECH,
       "the own vote is recovered from the vote log")
    eq(cv.elections_from_state(legacy, NOW)["f6"][1] if False else
       cv.elections_from_state(legacy, NOW)[1]["f6"], cv.FACTION_INVALID,
       "... only when a voter is known (no guessing)")
    fixed = cv.heal_infos(legacy, NOW)
    check(fixed >= 2, "heal_infos reports what it repaired")
    eq(legacy["infos"][1]["f6"], cv.FACTION_TECH,
       "heal_infos writes the recovered own vote into the save")
    eq(cv.own_vote_from_votes(legacy, 1, "Player"), cv.FACTION_TECH,
       "own_vote_from_votes is the shared derivation")

    # D. a change of mind reads back as the newest choice
    state2 = cv.default_state(NOW)
    cv.apply_vote(state2, 0, cv.FACTION_ECO, voter="Player", now=NOW)
    cv.apply_vote(state2, 0, cv.FACTION_TYCOON, voter="Player", now=NOW)
    eq(cv.elections_from_state(state2, NOW, voter="Player")[0]["f6"],
       cv.FACTION_TYCOON, "the newest vote wins")

    # E. key style is preserved, so a save never mixes "0" and 1
    mixed = {"infos": {"0": dict(state["infos"][0])}, "counts": {"0": {}},
             "votes": [], "voters": {}}
    cv.apply_vote(mixed, 1, cv.FACTION_ECO, voter="Player", now=NOW)
    check(all(isinstance(k, str) for k in mixed["infos"]),
          "a new key follows the existing (string) key style")
    json.dumps(mixed, sort_keys=True)          # would raise on a mixed-key dict
    check(True, "the result still serialises with sorted keys")




def test_simulated_election():
    """The world outside: a drawn outcome, revealed over the election."""
    print("\n-- test_simulated_election")
    base = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)

    slot = 0
    index = cv.election_index(slot, NOW)
    shares = cv.election_shares(slot, index)
    eq(sorted(shares), sorted(cv.FACTIONS), "one share per faction")
    check(abs(sum(shares.values()) - 1.0) < 1e-9, "the shares sum to 1")
    check(all(v >= cv.SIM_MIN_SHARE for v in shares.values()),
          "no faction is below the floor")
    check(max(shares.values()) > 0.34 and min(shares.values()) < 0.34,
          "the outcome is not a three-way tie")

    # a different election draws a different outcome, the same one never drifts
    other = cv.election_shares(slot, index + 1)
    check(other != shares, "the next election has its own outcome")
    eq(cv.election_shares(slot, index), shares, "an election's outcome is stable")

    electorate = cv.election_electorate(slot, index)
    low, high = cv.SIM_ELECTORATE
    eq(low <= electorate <= high, True,
       "the electorate is in range (%d)" % electorate)

    # revealed over time: monotone, and exactly the outcome at the end
    start, end, expire = cv.election_bounds(slot, NOW)
    check(end > start and expire > end, "the window is start < end < expire")
    previous = None
    samples = 0
    for hours in range(0, (end - start) * 24, 6):
        when = base + datetime.timedelta(days=start, hours=hours)
        counts = cv.simulated_votes(slot, when)
        eq(sorted(counts), sorted(cv.FACTIONS), "three bars")
        if previous is not None:
            check(all(counts[f] >= previous[f] for f in cv.FACTIONS),
                  "the count only grows (+%dh)" % hours)
        check(sum(counts.values()) > 0, "a live election is never at zero")
        previous = counts
        samples += 1
    eq(samples > 8, True, "sampled the whole window (%d samples)" % samples)

    when = base + datetime.timedelta(days=end) - datetime.timedelta(seconds=1)
    final = cv.simulated_votes(slot, when)
    expect = {f: int(round(electorate * shares[f])) for f in cv.FACTIONS}
    eq(final, expect, "the last counted ballot matches the drawn outcome")
    eq(sum(final.values()), electorate, "every ballot of the electorate is counted")

    # the player's own ballot is added on top, for the RUNNING election only
    state = cv.default_state(NOW)
    fresh = cv.served_counts(state, slot, NOW)
    eq(fresh, cv.simulated_votes(slot, NOW), "no votes yet: the world's count")
    cv.apply_vote(state, slot, cv.FACTION_TECH, voter="Player", now=NOW)
    after = cv.served_counts(state, slot, NOW)
    eq(after[cv.FACTION_TECH], fresh[cv.FACTION_TECH] + 1,
       "the player's ballot adds exactly one to their faction")
    eq(after[cv.FACTION_ECO], fresh[cv.FACTION_ECO], "and nothing to the others")

    # the voter list: the player first, then plausible simulated ballots
    voters = cv.served_voters(state, slot, 5, NOW)
    eq(len(voters), 5, "the requested number of voters")
    eq(voters[0]["voter"], "Player", "the real ballot is listed first")
    check(all(isinstance(v["voter"], str) and v["voter"] for v in voters),
          "every entry has a name")
    check(len(set(v["voter"] for v in voters)) == len(voters),
          "the names are distinct")
    eq(cv.served_voters(state, slot, 0, NOW), [], "limit 0 is an empty list")

    # the own vote belongs to ONE election: the next cycle lets you vote again
    election = cv.elections_from_state(state, NOW, voter="Player")
    eq(election[0]["f6"], cv.FACTION_TECH, "this election remembers the ballot")
    later = base + datetime.timedelta(days=end + 1)
    rolled = cv.elections_from_state(state, later, voter="Player")
    eq(rolled[0]["f6"], cv.FACTION_INVALID,
       "the next election starts unvoted (the panel lets you vote again)")
    eq(cv.election_index(slot, later), cv.election_index(slot, NOW) + 1,
       "... because the election instance advanced")

    # RV_VOTE_SIM=0 keeps the old static behaviour for A/B runs
    eq(cv.served_counts(state, slot, NOW, simulate=False),
       {cv.FACTION_TECH: 1, cv.FACTION_ECO: 0, cv.FACTION_TYCOON: 0},
       "simulation can be switched off (stored/tallied counts only)")

    # ... and a ballot from an earlier election does not leak into this one
    stale = json.loads(json.dumps(state))
    stale["votes"][0]["election"] = cv.election_index(slot, NOW) - 1
    eq(cv.served_counts(stale, slot, NOW), fresh,
       "a ballot from the previous election is not counted again")
    eq(cv.own_vote_from_votes(stale, slot, "Player", now=NOW), cv.FACTION_INVALID,
       "nor does it make the new election look voted")


def test_hygiene():
    print("\n-- test_hygiene")
    path = os.path.join(ROOT, "server", "content_voting.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:                       # relative import
                roots.add("." * node.level + (node.module or ""))
            else:
                roots.add((node.module or "").split(".")[0])
    eq(sorted(roots), ["datetime", "time"],
       "stdlib-only imports, nothing from the server tree")
    for banned in ("open(", "socket", "urllib", "requests"):
        check(banned not in source, "module has no %r call/import" % banned)
    # no I/O helpers reachable at module level
    eq([n.name for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "open"],
       [], "module never calls open()")
    # calling the builders twice in a row cannot accumulate state
    eq(cv.voting_infos(NOW), cv.voting_infos(NOW), "no hidden module state")
    eq(cv.vote_counts(None, 0), cv.DEFAULT_COUNTS,
       "the DEFAULT_COUNTS constant is not mutated by readers")
    eq(sum(cv.DEFAULT_COUNTS.values()), 20, "DEFAULT_COUNTS untouched (5+12+3)")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    for fn in (test_packed_layout, test_roundtrip, test_default_election,
               test_wire_decode, test_apply_vote, test_vote_record_is_created,
               test_simulated_election, test_hygiene):
        fn()
    print("\n%d checks, %d failed" % (CHECKS, len(FAILURES)))
    if FAILURES:
        for label in FAILURES:
            print("  FAILED: %s" % label)
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
