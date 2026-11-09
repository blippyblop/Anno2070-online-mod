#!/usr/bin/env python3
"""Senate/election **CONTENT** for ``VotingServiceProtocol``.

The wire *shapes* live in :mod:`rv_shapes.voting` and are already verified; this
module produces the *values* that go into them, i.e. the election documents the
senate screen (`CMainMenu06SenateFrame`) actually renders.  Every decision here
is taken from ``decomp/_adhoc/voting-events-content.md`` (the content spec); the
short version:

* the senate frame hard-codes ``GetVotingInfo(out, id=0)``, so the voting map
  **must contain key 0** or the client shows its empty defaults and nothing
  renders;
* the id the client sends back in ``Vote(id, faction)`` is ``VotingInfo.f1``
  (not the map key; the two are equal in this MVP);
* ``f5`` is the player's own vote as a **faction id** -- ``FACTION_ECO=0``,
  ``FACTION_TYCOON=1``, ``FACTION_TECH=2`` and ``-1`` (0xFFFFFFFF) = "not voted";
  the client's own empty struct defaults ``f5 = f8 = f9 = -1``;
* ``f4`` / ``f6`` / ``f7`` are **not** epoch seconds: they are Anno's bit-packed
  ``Date`` (``sec:6, min:6, hour:5, day:5, month:4, year:14, flags:24``,
  encoder ``FUN_141352220``).  Epoch seconds decode as nonsense ("year 26,
  hour 24") and the client prints 1970/blank;
* ``f9`` is used as a table index **without a bounds check**, so send ``-1``;
* ``VotingStats.f1`` is a *denominator* (``100*sum/f1``): it must be ``> 0`` and
  ``>= sum`` or the client paints ``INT_MIN`` % / >100 % bars;
* the panels are fed by ``GetVotingInfos`` + ``GetVoteCount`` + ``GetLastVoters``
  and, after ``Vote(id, faction)`` (a **void** reply) returns, the client
  re-issues ``GetVoteCount``/``GetLastVoters`` itself.

Field names used here are the ones :func:`rv_shapes.voting.voting_info` and
``rmc_services.DEFAULT_STATE`` read, so a document from this module can be
handed to ``rv_shapes`` unchanged.  **Their numbering is shifted by one relative
to the spec** because their first slot is the map key:

==========================  ==========  ===================================================
dict key (this module)      spec name   meaning
==========================  ==========  ===================================================
``"id"``                    map key     election slot; **must** contain 0
``"f2"``                    ``f1``      id sent back in ``Vote()`` (== key for the MVP)
``"f3"``                    ``f2``      never read by any consumer; 0
``"running"`` (bool)        ``f3``      never read; 1 = "vote open" (guess, see below)
``"start"`` (u64)           ``f4``      packed ``Date`` -- vote start
``"f6"`` (u32)              ``f5``      the player's own vote = faction id, ``-1`` = none
``"votesA"`` (u64)          ``f6``      packed ``Date`` -- vote end
``"votesB"`` (u64)          ``f7``      packed ``Date`` -- senate item expiry
``"f9"`` (u32)              ``f8``      global object-map key, ``-1`` = null
``"f10"`` (u32)             ``f9``      per-effect table index, ``-1`` = null
==========================  ==========  ===================================================

Standard library only, no I/O, no import-time side effects, no mutable module
state: every function is pure (``apply_vote`` mutates the dict you hand it and
nothing else).

Guesses are marked ``# GUESS:`` with the live observation that would settle them.
"""

import datetime
import time

__all__ = [
    "VOTING_PROTO",
    "FACTION_INVALID", "FACTION_ECO", "FACTION_TYCOON", "FACTION_TECH",
    "FACTIONS", "FACTION_NAMES",
    "FIELDS",
    "DEFAULT_COUNTS", "DATE_OFFSETS",
    "packed_date", "unpacked_date", "unpacked_date_fields", "is_packed_date",
    "election_window", "default_election", "default_elections", "voting_infos",
    "elections_from_state",
    "vote_counts", "last_voters", "voting_stats", "apply_vote",
    "default_state", "own_vote", "vote_id",
]

VOTING_PROTO = "VotingServiceProtocol"


# --------------------------------------------------------------------------
# factions (enum proven in FUN_14073ea70)
# --------------------------------------------------------------------------

FACTION_INVALID = -1
FACTION_ECO = 0
FACTION_TYCOON = 1
FACTION_TECH = 2

#: the three senate bars (Lua windows VOTE2 / VOTE1 / VOTE3 respectively -- do
#: not assume wire option ids follow window order, take them from the log line)
FACTIONS = (FACTION_ECO, FACTION_TYCOON, FACTION_TECH)

FACTION_NAMES = {
    FACTION_ECO: "Eco",
    FACTION_TYCOON: "Tycoon",
    FACTION_TECH: "Tech",
}

#: Reputation a single senate / World Council vote is worth.
#: The client itself says "+50" when you vote (it is a local message -- Vote_V1
#: returns no body at all, so nothing on the wire carries it).  The real
#: service credited it to the player's career through the profile, which is why
#: it now lands in ``players.<pid>.reputation`` here and survives a restart.
VOTE_REWARD = 50

#: the election document fields, in wire order (see the module table above)
FIELDS = ("id", "f2", "f3", "running", "start", "f6",
          "votesA", "votesB", "f9", "f10")


# --------------------------------------------------------------------------
# packed Anno Date codec  (encoder FUN_141352220, decoder via _mkgmtime64)
# --------------------------------------------------------------------------
#
#   bits  0-5   second  0..59
#   bits  6-11  minute  0..59
#   bits 12-16  hour    0..23
#   bits 17-21  day     1..31
#   bits 22-25  month   1..12
#   bits 26-39  year    (full 4-digit year, *not* years-since-1900)
#   bits 40-63  flags   preserved verbatim; _mkgmtime64 ignores them
#
# The client reads these as **UTC** (``_mkgmtime64``), so everything here is
# naive-UTC: tz-aware input is converted, microseconds are truncated.

_MASK_SECOND = 0x3F
_MASK_MINUTE = 0x3F
_MASK_HOUR = 0x1F
_MASK_DAY = 0x1F
_MASK_MONTH = 0xF
_MASK_YEAR = 0x3FFF
_MASK_FLAGS = 0xFFFFFF

_DATE_BITS = 40                      # sec+min+hour+day+month+year
_UTC = datetime.timezone.utc


def _to_naive_utc(value):
    """Normalise ``value`` to a naive UTC ``datetime`` truncated to seconds.

    Accepts a ``datetime`` (aware or naive; naive is *taken as* UTC), a
    ``date`` (midnight), a ``(y, mo, d[, h, mi, s])`` sequence, or epoch
    seconds (int/float).  Raises ``TypeError`` for anything else.
    """
    if isinstance(value, datetime.datetime):
        dt = value
        if dt.tzinfo is not None and dt.utcoffset() is not None:
            dt = dt.astimezone(_UTC).replace(tzinfo=None)
        return dt.replace(microsecond=0)
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    if isinstance(value, (tuple, list)):
        parts = [int(v) for v in value]
        if len(parts) < 3:
            raise TypeError("date sequence needs at least (year, month, day)")
        while len(parts) < 6:
            parts.append(0)
        return datetime.datetime(*parts[:6])
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.datetime.fromtimestamp(float(value), _UTC).replace(
            tzinfo=None, microsecond=0)
    raise TypeError("unsupported date value %r" % (value,))


def packed_date(value, flags=0):
    """Encode ``value`` as Anno's bit-packed ``Date`` (a python ``int``).

    ``value`` is a ``datetime``/``date``/``(y, mo, d[, h, mi, s])``/epoch
    seconds (see :func:`_to_naive_utc`); ``flags`` occupies bits 40-63 (0 for
    every date the client produces itself).

    The C encoder masks silently (that is how "year 26, hour 24" happens); this
    version validates instead, so a bad date raises ``ValueError`` rather than
    becoming a plausible-looking 1970 tag.
    """
    dt = _to_naive_utc(value)
    try:
        flags = int(flags)
    except (TypeError, ValueError):
        raise ValueError("flags must be an int")
    if not 0 <= flags <= _MASK_FLAGS:
        raise ValueError("flags out of range 0..0x%X: %r" % (_MASK_FLAGS, flags))
    if not 1 <= dt.year <= _MASK_YEAR:
        # the client's _mkgmtime64 only really handles 1970..3000; outside that
        # it returns -1 and the tag shows 1970/blank
        raise ValueError("year out of Anno Date range: %r" % (dt.year,))
    v = dt.year & _MASK_YEAR
    v = (v << 4) | (dt.month & _MASK_MONTH)
    v = (v << 5) | (dt.day & _MASK_DAY)
    v = (v << 5) | (dt.hour & _MASK_HOUR)
    v = (v << 6) | (dt.minute & _MASK_MINUTE)
    v = (v << 6) | (dt.second & _MASK_SECOND)
    return v | (flags << _DATE_BITS)


def unpacked_date_fields(value):
    """``(year, month, day, hour, minute, second)`` or ``None``.

    Pure bit extraction -- the same order the client's getters read
    (``FUN_1413522d0`` .. ``FUN_141352210``).  Returns ``None`` for a value
    that is not an int.
    """
    try:
        v = int(value) & 0xFFFFFFFFFFFFFFFF
    except (TypeError, ValueError, OverflowError):
        return None
    second = v & _MASK_SECOND
    minute = (v >> 6) & _MASK_MINUTE
    hour = (v >> 12) & _MASK_HOUR
    day = (v >> 17) & _MASK_DAY
    month = (v >> 22) & _MASK_MONTH
    year = (v >> 26) & _MASK_YEAR
    return (year, month, day, hour, minute, second)


def unpacked_date(value):
    """Decode a packed ``Date`` to a naive **UTC** ``datetime``.

    ``None`` when the packed fields are not a real calendar date (year 0,
    month 13, day 32, hour 24, 30 February, ...) -- i.e. exactly the values for
    which the client's ``_mkgmtime64`` yields -1 and the tag renders blank.

    Note the client *normalises* some out-of-range fields instead of failing
    (that is why epoch seconds look like "year 26"); normalisation is
    deliberately **not** modelled here, so a decode failure is loud.
    """
    parts = unpacked_date_fields(value)
    if parts is None:
        return None
    year, month, day, hour, minute, second = parts
    try:
        return datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def is_packed_date(value):
    """True when ``value`` looks like a packed ``Date`` the client will accept.

    Guards the two failure modes of the store's older state files: epoch
    seconds (decodes to year 26 / hour 24 -> ``None``) and 0 (year 0).
    """
    dt = unpacked_date(value)
    return dt is not None and 1970 <= dt.year <= 3000


def _utcnow():
    return datetime.datetime.now(_UTC).replace(tzinfo=None, microsecond=0)


def _now(now=None):
    return _utcnow() if now is None else _to_naive_utc(now)


# --------------------------------------------------------------------------
# the default election set
# --------------------------------------------------------------------------

#: the MVP bars from the spec (SS5.2): plausible, not measured.
#: GUESS: the real live numbers come from other players voting; any values
#: render, so confirm only that the three bars are non-zero and sum to 100 %.
DEFAULT_COUNTS = {FACTION_ECO: 5, FACTION_TYCOON: 12, FACTION_TECH: 3}

_DAY = 24 * 60 * 60

#: start / end / item-expiry offsets around "now", in seconds.
#: GUESS: *which* of the three dates carries which tag is [medium] in the spec
#: (the tag filler lives in a Lua chunk that is not on disk).  Confirm visually:
#: if the header's "vote starts" tag shows a future date, the mapping is
#: reversed -- swap DATE_OFFSETS["start"] and DATE_OFFSETS["end"].
DATE_OFFSETS = {
    "start": -1 * _DAY,     # f4  TIME_WHEN_SENATE_VOTE_STARTS
    "end": 2 * _DAY,        # f6  TIME_WHEN_SENATE_VOTE_ENDS
    "expire": 9 * _DAY,     # f7  TIME_WHEN_SENATE_ITEM_EXPIRES
}


# --------------------------------------------------------------------------
# the simulated electorate: a real-looking election, without a world
# --------------------------------------------------------------------------
# A local server has no other players, so the bars would either be frozen
# filler or empty.  Instead every election is a **simulated contest**: the
# outcome (the final share per faction) is drawn once per election, and the
# counted votes are revealed over the election's lifetime with a little noise,
# so the panel shows a race that is decided by the time the window closes.
#
# Everything is a pure function of ``(slot, election index, time)`` - no
# accumulator, no RNG object, nothing to persist - so a restart mid-election
# shows exactly the same numbers, on any machine, at any time.

#: Elections are cycles anchored to a fixed date, so "which election is this"
#: is a fact about the calendar rather than about when the server started.
VOTE_ANCHOR = datetime.date(2026, 1, 1)

#: How long one election runs, in UTC days, per election slot: the senate is
#: the quick one (a law vote every few days), the World Council runs a season a
#: week.  Slots the server does not serve fall back to ``DEFAULT_VOTE_PERIOD``.
VOTE_PERIOD_DAYS = (3, 7)
DEFAULT_VOTE_PERIOD = 3

#: How many votes the whole world casts over one election (inclusive range).
SIM_ELECTORATE = (800, 4200)

#: How many steps the reveal curve is sampled at (more = smoother bar).
SIM_CURVE_STEPS = 60

#: No faction ever gets less than this share, so a bar is never empty.
SIM_MIN_SHARE = 0.04

_VOTE_SALT = 0x7E57_2070_0000_0001
_MIX_MASK = (1 << 64) - 1


def _mix(value):
    """splitmix64: a pure, well-mixed 64-bit hash (no RNG state to persist)."""
    z = (int(value) + 0x9E3779B97F4A7C15) & _MIX_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MIX_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MIX_MASK
    return (z ^ (z >> 31)) & _MIX_MASK


def _day_ordinal(when=None):
    """UTC days since 1970-01-01 (the election cycles are counted in these)."""
    base = _now(when)
    epoch = datetime.datetime(1970, 1, 1)
    return (base - epoch).days


def election_period_days(slot):
    """How many days one election of ``slot`` runs."""
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        slot = 0
    if 0 <= slot < len(VOTE_PERIOD_DAYS):
        return int(VOTE_PERIOD_DAYS[slot])
    return int(DEFAULT_VOTE_PERIOD)


def election_index(slot=0, now=None):
    """Which election of ``slot`` is running at ``now`` (stable per cycle)."""
    period = election_period_days(slot)
    anchor = _day_ordinal(VOTE_ANCHOR)
    return (_day_ordinal(now) - anchor) // period


def election_bounds(slot=0, now=None):
    """``(start_day, end_day, expire_day)`` of the running election."""
    period = election_period_days(slot)
    anchor = _day_ordinal(VOTE_ANCHOR)
    start = anchor + election_index(slot, now) * period
    return start, start + period, start + period + 7


def election_window(now=None, slot=0):
    """``{"start": dt, "end": dt, "expire": dt}`` of the running election.

    Anchored to the election cycle (not to "now"), so the dates advance by
    themselves as one election ends and the next begins -
    ``start <= now < end < expire`` by construction.
    """
    start, end, expire = election_bounds(slot, now)
    midnight = datetime.datetime(1970, 1, 1)
    return {"start": midnight + datetime.timedelta(days=start),
            "end": midnight + datetime.timedelta(days=end),
            "expire": midnight + datetime.timedelta(days=expire)}


def election_progress(slot=0, now=None):
    """How far the running election has come, ``0.0`` .. ``1.0``."""
    start, end, _expire = election_bounds(slot, now)
    period = max(1, end - start)
    elapsed = _day_ordinal(now) - start
    step = (int(elapsed) + 0.0) / period
    # within the day, refine by the clock so the bar creeps during a session
    base = _now(now)
    seconds = (base.hour * 3600 + base.minute * 60 + base.second) / 86400.0
    step += seconds / period
    return min(1.0, max(0.0, step))


def election_shares(slot=0, index=None):
    """The drawn outcome: ``{faction: share}`` for this election, summing to 1.

    Each election draws three weights and normalises them, with a floor so no
    bar is empty.  That is what makes one Senate vote a two-horse race and the
    next a landslide - like the example in the report (50 % / 20 % / 30 %).
    """
    if index is None:
        index = election_index(slot)
    seed = _mix(_VOTE_SALT ^ (int(index) * 0x9E3779B1) ^ (int(slot) * 0x85EBCA6B))
    raw = {faction: 1 + _mix(seed + 1 + faction) % 1000 for faction in FACTIONS}
    total = float(sum(raw.values())) or 1.0
    shares = {faction: raw[faction] / total for faction in FACTIONS}
    # lift everything to the floor, then re-normalise the rest
    fixed = {f: SIM_MIN_SHARE for f in FACTIONS}
    spare = 1.0 - SIM_MIN_SHARE * len(FACTIONS)
    rest = {f: max(0.0, shares[f] - SIM_MIN_SHARE) for f in FACTIONS}
    rest_total = sum(rest.values()) or 1.0
    for faction in FACTIONS:
        fixed[faction] = SIM_MIN_SHARE + spare * rest[faction] / rest_total
    return fixed


def election_electorate(slot=0, index=None):
    """How many votes the world casts over this election (``SIM_ELECTORATE``)."""
    if index is None:
        index = election_index(slot)
    low, high = SIM_ELECTORATE
    seed = _mix(_VOTE_SALT ^ 0xA5A5 ^ (int(index) * 31) ^ (int(slot) * 0x27D4EB2F))
    return int(low + seed % (high - low + 1))


def _reveal_curve(slot, index, steps=SIM_CURVE_STEPS):
    """Prefix fractions of the count, one per step: 0 before start, 1 at the end.

    Uneven on purpose (a quiet day, a busy evening), and strictly increasing, so
    the bars only ever grow.
    """
    base = _mix(_VOTE_SALT ^ 0x5EED ^ (int(index) * 7) ^ (int(slot) * 0x9E3779B1))
    weights = [1 + _mix(base + step) % 1000 for step in range(steps)]
    total = float(sum(weights)) or 1.0
    out, running = [], 0
    for weight in weights:
        running += weight
        out.append(running / total)
    return out


def simulated_votes(slot=0, now=None):
    """The world's counted votes so far: ``{faction: votes}`` (no player vote).

    ``electorate * share * reveal(progress) ** pace`` per faction: the pace is
    per faction and per election, so early counts can have a different leader
    than the result (that is the "random variation" of a real count) while the
    final numbers are exactly the drawn shares.
    """
    slot = int(slot)
    if slot not in (0, 1):
        # only the elections we actually serve are simulated; an ad-hoc id in a
        # test must keep its own stored counts
        return {faction: 0 for faction in FACTIONS}
    index = election_index(slot, now)
    shares = election_shares(slot, index)
    electorate = election_electorate(slot, index)
    curve = _reveal_curve(slot, index)
    # a live election is never at a dead zero: the first count is already in
    # (2 % of the curve), so the bars render from the first second of a cycle
    # while still reaching exactly 100 % at the end
    position = (0.02 + 0.98 * election_progress(slot, now)) * len(curve)
    whole = int(position)
    fraction = position - whole
    if whole >= len(curve):
        revealed = 1.0
    elif whole == 0:
        revealed = curve[0] * fraction
    else:
        previous = curve[whole - 1]
        revealed = previous + (curve[whole] - previous) * fraction
    seed = _mix(_VOTE_SALT ^ 0xBEEF ^ (int(index) * 11) ^ (slot * 0x2545F491))
    out = {}
    for faction in FACTIONS:
        pace = 0.65 + (_mix(seed + 1 + faction) % 1000) / 1000.0 * 0.9   # 0.65..1.55
        share_part = revealed ** pace if revealed > 0 else 0.0
        out[faction] = int(round(electorate * shares[faction] * share_part))
    return out


def default_election(now=None, key=0, vote_id=None, running=True,
                     own_vote=FACTION_INVALID):
    """One ``VotingInfo`` document (a plain dict, ``rv_shapes`` field names).

    ``key`` is the map key (**0 is the one the senate frame asks for**) and
    ``vote_id`` -- the value the client sends back in ``Vote()`` -- defaults to
    ``key``: keep them equal, the spec calls a mismatch trap #3.

    ``own_vote`` is ``-1`` (not voted) by default so the client shows the three
    buttons; ``apply_vote`` flips it to the faction that voted.
    """
    window = election_window(now)
    key = int(key)
    return {
        "id": key,                                        # map key / election slot
        "f2": key if vote_id is None else int(vote_id),   # spec f1 -> Vote()'s arg1
        "f3": 0,                                          # spec f2, never read
        # GUESS: spec f3 is a bool no reached consumer reads; 1 = "vote open" is
        # the natural reading.  Falsify by sending 0 and watching whether the
        # bars/buttons stay disabled.
        "running": bool(running),
        "start": packed_date(window["start"]),            # spec f4, packed Date
        "f6": int(own_vote),                              # spec f5, own vote / -1
        "votesA": packed_date(window["end"]),             # spec f6, packed Date
        "votesB": packed_date(window["expire"]),          # spec f7, packed Date
        "f9": FACTION_INVALID,                            # spec f8, object-map key
        "f10": FACTION_INVALID,                           # spec f9, table index
    }


#: Vote() id of the second (World Council) election slot.
COUNCIL_VOTE_ID = 1001


def default_elections(now=None):
    """The minimum viable election set.

    ``{0: VotingInfo}`` is what the SENATE frame needs (it hard-codes
    GetVotingInfo(0) - spec trap #2). PN 158 measured that the sibling menu entry
    "World Council vote" still reports "No current elections or votes." on a live
    session where the senate renders, so a SECOND slot is served here: the map key
    is the election slot, so slot 1 is the obvious council slot. Its vote id is
    1001 so a Vote() for either slot is unambiguous in the log.
    """
    return {0: default_election(now),
            1: default_election(now, key=1, vote_id=COUNCIL_VOTE_ID)}


def _normalise_infos(elections, now):
    """``[(key, info_dict), ...]`` ascending by key, every field present.

    ``elections`` may be ``None`` (-> :func:`default_elections`), a mapping
    ``key -> record`` (``rmc_services`` style store), a *single* record dict, or
    a sequence of records (``rv_shapes`` style); for the last two each record's
    ``id`` is its key.  Missing fields are filled from
    :func:`default_election` at that key, and a date field that is not a packed
    ``Date`` (an old state file storing epoch seconds, or 0) is replaced by the
    default window -- otherwise trap #1 in the spec ("year 26, hour 24") is
    exactly what the client would display.
    """
    if elections is None:
        elections = default_elections(now)
    items = []
    if isinstance(elections, dict) and any(name in elections for name in FIELDS):
        elections = [elections]          # a single VotingInfo record
    if isinstance(elections, dict):
        source = elections.items()
    else:
        source = []
        try:
            candidates = list(elections)
        except TypeError:
            candidates = []
        for record in candidates:
            if not isinstance(record, dict):
                continue
            raw_key = record.get("id")
            try:
                raw_key = int(raw_key)
            except (TypeError, ValueError):
                raw_key = 0
            source.append((raw_key, record))
    for raw_key, record in source:
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            continue
        if key < 0:                     # map keys are u32; -1 cannot be looked up
            continue
        base = default_election(now, key=key)
        if isinstance(record, dict):
            for name in FIELDS:
                if name in record and record[name] is not None:
                    base[name] = record[name]
            for name in ("start", "votesA", "votesB"):
                if not is_packed_date(base[name]):
                    base[name] = default_election(now, key=key)[name]
        items.append((key, base))
    items.sort(key=lambda kv: kv[0])
    return items


def voting_infos(now=None, elections=None):
    """The ``GetVotingInfos_V1`` list -- ``list[VotingInfo]`` for ``rv_shapes``.

    Ascending by map key, always containing key 0 when ``elections`` is
    ``None``.  Feed it straight in::

        reply = rv.voting.get_voting_infos(content_voting.voting_infos())

    Entries are copies; the caller's dicts are never mutated.
    """
    return [dict(record) for _key, record in _normalise_infos(elections, now)]


def own_vote(info):
    """The spec-``f5`` own vote of a document (``-1`` = not voted)."""
    try:
        return int(info.get("f6", FACTION_INVALID))
    except (AttributeError, TypeError, ValueError):
        return FACTION_INVALID


def vote_id(info):
    """The id ``Vote()`` will send for this document (spec-``f1``)."""
    try:
        return int(info.get("f2", 0))
    except (AttributeError, TypeError, ValueError):
        return 0


def elections_from_state(state=None, now=None, key=None, voter=None):
    """Build the election map from a store state.

    Glue for ``rmc_services.h_get_voting_infos``: ``state`` may be the whole
    store state or its ``voting`` sub-dict.  Only the two values that are
    meaningful across a restart are carried over -- the player's own vote
    (spec ``f5``) and the "running" bool -- and the three dates are rebuilt
    around ``now`` so a legacy state file that still stores epoch seconds
    cannot hit the "year 26, hour 24" trap.  The map key and the id ``Vote()``
    sends back are both forced to the slot key (spec trap #3).

    ``key`` used to select a single slot; it now only exists for callers that
    want one slot back (``key=0`` -> ``{0: ...}``).  Every slot is restored --
    restoring only slot 0 is why a World Council vote came back as "not voted"
    after a restart (the council is slot 1, Vote() id 1001).

    ``voter`` is the session player: when the stored document has no usable own
    vote (a save written before the record existed, or one whose ``infos`` map
    never had that slot), the own vote is recovered from the vote log, which is
    what makes an existing save heal itself instead of needing a migration.
    """
    elections = default_elections(now)
    if key is not None:
        elections = {int(key): elections[int(key)]} if int(key) in elections \
            else {int(key): default_election(now, key=int(key))}
    st = _voting_state(state) or {}
    infos = st.get("infos")
    legacy = st.get("votingInfo")
    for slot, election in elections.items():
        record = None
        if isinstance(infos, dict):
            record = _lookup(infos, slot)
        if not isinstance(record, dict) and isinstance(legacy, dict) and slot == 0:
            record = legacy
        own = FACTION_INVALID
        if isinstance(record, dict):
            try:
                own = int(record.get("f6", FACTION_INVALID))
            except (TypeError, ValueError):
                own = FACTION_INVALID
        if isinstance(record, dict) and "election" in record:
            try:
                if int(record.get("election")) != election_index(slot, now):
                    # the document belongs to a previous election: nobody has
                    # voted in the new one yet, so the buttons must come back
                    own = FACTION_INVALID
            except (TypeError, ValueError):
                pass
        if own not in FACTIONS and voter is not None:
            # self-healing: the durable vote log knows what the document lost
            own = own_vote_from_votes(st, slot, voter, now=now)
        if isinstance(record, dict) or own in FACTIONS:
            election["f6"] = own if own in FACTIONS else FACTION_INVALID
        if isinstance(record, dict) and "running" in record:
            election["running"] = bool(record["running"])
    return elections


# --------------------------------------------------------------------------
# plain-dict state (persisted by the store, driven by the tests)
# --------------------------------------------------------------------------

def default_state(now=None):
    """A fresh voting state: one key-0 election, seeded bars, no votes.

    Shape (JSON-safe, keys become strings on disk -- the readers below coerce)::

        {"infos": {0: VotingInfo},         # map key -> document
         "counts": {0: {faction: n}},      # get vote-count bars
         "votes": [{"voter","choice","ts","id"}],   # rmc_services' own record
         "voters": {0: [{"voter","a","b"}]}}        # GetLastVoters entries
    """
    infos = default_elections(now)
    return {
        "infos": infos,
        "counts": {key: dict(DEFAULT_COUNTS) for key in infos},
        "votes": [],
        "voters": {key: [] for key in infos},
    }


def _voting_state(state):
    """The voting sub-dict of ``state`` (accepts a whole store state too)."""
    if state is None:
        return None
    if not isinstance(state, dict):
        return None
    own = ("infos", "votingInfo", "counts", "votes", "voters", "electorate")
    if not any(name in state for name in own):
        inner = state.get("voting")
        if isinstance(inner, dict):
            return inner
    return state


def _as_election_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _lookup(container, key):
    """``container[key]`` tolerating JSON's stringified int keys."""
    if not isinstance(container, dict):
        return None
    if key in container:
        return container[key]
    text = str(key)                       # JSON stringifies int keys
    if text in container:
        return container[text]
    return None


def _put(container, key, value):
    """``container[key] = value`` keeping the container's existing key style.

    A state file that has been to disk uses ``"0"``/``"1"``; a fresh in-memory
    one uses ``0``/``1``.  Mixing the two in one dict is what makes
    ``json.dump(sort_keys=True)`` raise ``TypeError: '<' not supported between
    instances of 'int' and 'str'`` - so a new key copies the style already in
    use (and a key that is already present is reused, whatever its spelling).
    """
    if not isinstance(container, dict):
        return None
    existing = None
    for candidate in (key, str(key)):
        if candidate in container:
            existing = candidate
            break
    if existing is None:
        str_keys = sum(1 for k in container if isinstance(k, str))
        int_keys = sum(1 for k in container if isinstance(k, int))
        existing = str(key) if str_keys > int_keys else key
    container[existing] = value
    return value


def own_vote_from_votes(state, election_id, voter, now=None):
    """The faction ``voter`` cast in the RUNNING election, or ``FACTION_INVALID``.

    The vote log (``state["votes"]``) is the durable record: it survives every
    older build, and a save can carry it while the ``infos`` document that is
    *supposed* to mirror it is missing (that is exactly how a World Council
    vote came back as "not voted").  Newest record wins, so a change of mind
    reads back as the latest choice.
    """
    st = _voting_state(state) or {}
    votes = st.get("votes") if isinstance(st, dict) else None
    if not isinstance(votes, (list, tuple)):
        return FACTION_INVALID
    try:
        election_id = int(election_id)
    except (TypeError, ValueError):
        return FACTION_INVALID
    wanted = str(voter or "")
    running = election_index(election_id, now)
    for vote in reversed(votes):
        if not isinstance(vote, dict):
            continue
        if _as_election_id(vote.get("id", 0)) != election_id:
            continue                  # a record without ``id`` is election 0
        recorded = _vote_instance(vote)
        if recorded is not None and recorded != running:
            continue                  # a ballot from an earlier election
        if str(vote.get("voter", "") or "") != wanted:
            continue
        try:
            choice = int(vote.get("choice", FACTION_INVALID))
        except (TypeError, ValueError):
            continue
        if choice in FACTIONS:
            return choice
    return FACTION_INVALID


def _ensure_info(state, election_id, now=None):
    """The ``infos`` record for one slot, created if the save has none.

    ``apply_vote`` used to write the own-vote field only when a record already
    existed, and nothing ever created one: a save whose ``infos`` map held just
    slot 0 therefore recorded a council vote in the counts/votes and lost the
    own vote for ever (the "World Council vote can be done again after a
    restart" report).  Creating the record here also repairs such a save the
    first time anything touches the election.
    """
    election_id = _as_election_id(election_id)
    st = _voting_state(state)
    if not isinstance(st, dict):
        return None
    infos = st.get("infos")
    if not isinstance(infos, dict):
        infos = st["infos"] = {}
        legacy = st.get("votingInfo")
        if isinstance(legacy, dict):
            record = {"id": 0}
            record.update({k: v for k, v in legacy.items() if k != "id"})
            _put(infos, 0, record)
    record = _lookup(infos, election_id)
    if isinstance(record, dict):
        return record
    fresh = default_election(now, key=election_id,
                             vote_id=_default_vote_id(election_id))
    return _put(infos, election_id, fresh)


def _default_vote_id(election_id):
    """The ``Vote()`` id for a slot we have to materialise."""
    election_id = _as_election_id(election_id)
    return COUNCIL_VOTE_ID if election_id == 1 else election_id


def voters_in_election(state, election_id):
    """The distinct voters the log knows for one election, oldest first."""
    st = _voting_state(state) or {}
    votes = st.get("votes") if isinstance(st, dict) else None
    out = []
    for vote in votes if isinstance(votes, (list, tuple)) else ():
        if not isinstance(vote, dict):
            continue
        if _as_election_id(vote.get("id", 0)) != _as_election_id(election_id):
            continue
        name = str(vote.get("voter", "") or "")
        if name and name not in out:
            out.append(name)
    return out


def heal_infos(state, now=None, voter=None):
    """Make ``infos`` hold a record per served slot, with the own vote filled in.

    Called on every store load (``rmc_services._migrate_state``) so a save
    written by an older build is repaired *before* it is served - and so the
    file itself carries the own vote, which is what the status report and any
    hand inspection read.  Returns the number of fields created or fixed.

    The own vote is a *per-player* value, so with no ``voter`` it is only
    written when the election's log has exactly one voter (this server is
    single-player, so that is the normal case); a shared election is left for
    ``elections_from_state(voter=...)`` to answer per session.  Guessing the
    wrong player's vote into the document would be worse than leaving it unset.
    """
    st = _voting_state(state)
    if not isinstance(st, dict):
        return 0
    fixed = 0
    # legacy ballots: attribute them to the running election of their slot
    if isinstance(st.get("votes"), list):
        for vote in st["votes"]:
            if isinstance(vote, dict) and "election" not in vote:
                vote["election"] = election_index(vote.get("id", 0), now)
                fixed += 1
    for slot in default_elections(now):
        record = _lookup(st.get("infos"), slot)
        existed = isinstance(record, dict)
        record = _ensure_info(st, slot, now)
        if not isinstance(record, dict):
            continue
        if not existed:
            fixed += 1
        if isinstance(record, dict) and "election" not in record:
            # an older save has no idea which election its ballot belongs to:
            # attribute it to the running one (it was cast today) so the next
            # cycle can start clean
            record["election"] = election_index(slot, now)
            fixed += 1
        try:
            own = int(record.get("f6", FACTION_INVALID))
        except (TypeError, ValueError):
            own = FACTION_INVALID
        if own in FACTIONS:
            continue
        if voter is not None:
            who = str(voter)
        else:
            known = voters_in_election(st, slot)
            who = known[0] if len(known) == 1 else None
        if who is None:
            continue
        derived = own_vote_from_votes(st, slot, who, now=now)
        if derived in FACTIONS:
            record["f6"] = derived
            fixed += 1
    return fixed


def _tally_votes(state, election_id):
    """Faction -> votes, tallied from ``state["votes"]`` (rmc_services shape)."""
    tally = {}
    votes = state.get("votes") if isinstance(state, dict) else None
    if not isinstance(votes, (list, tuple)):
        return tally
    for vote in votes:
        if not isinstance(vote, dict):
            continue
        try:
            vote_election = _as_election_id(vote.get("id", election_id))
            choice = int(vote.get("choice", FACTION_INVALID))
        except (TypeError, ValueError):
            continue
        if vote_election != election_id:
            continue
        if "id" not in vote and election_id != 0:
            # a vote record without ``id`` belongs to election 0 only
            continue
        tally[choice] = tally.get(choice, 0) + 1
    return tally


def vote_counts(state=None, election_id=0):
    """``GetVoteCount_V1`` map: ``{faction: votes}``, **ascending** keys.

    With ``state=None`` this is the MVP baseline (:data:`DEFAULT_COUNTS`) for
    election 0 and empty otherwise, so a caller can render without a store.
    When the state has no counts for that election the votes list is tallied
    instead; the three factions are always present (zeros) so all three bars
    draw.
    """
    election_id = _as_election_id(election_id)
    if state is None:
        base = dict(DEFAULT_COUNTS) if election_id == 0 else {}
    else:
        st = _voting_state(state)
        counts = _lookup(st.get("counts") if isinstance(st, dict) else None,
                         election_id)
        if isinstance(counts, dict):
            base = counts
        else:
            base = _tally_votes(st or {}, election_id)
    out = {}
    for faction in FACTIONS:
        out[faction] = 0
    for raw_faction, raw_count in (base or {}).items():
        try:
            faction = int(raw_faction)
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        out[faction] = max(0, count)
    return {faction: out[faction] for faction in sorted(out)}


def _synthetic_voters(counts, limit):
    """Filler voter entries so the list under the bars is not empty.

    GUESS: the names are never compared to anything (the spec: "filler names
    fine, they are only shown in the voter list"); the spec's own example is
    ``"Eco-Voter"``.  Falsified if the list shows duplicates of the local
    player or the client filters unknown names out.
    """
    out = []
    for faction in sorted(counts):
        name = FACTION_NAMES.get(faction, "Faction %d" % faction)
        for i in range(int(counts[faction])):
            if len(out) >= limit:
                return out
            out.append({"voter": "%s Voter %d" % (name, i + 1),
                        "a": faction,
                        # GUESS: the second u32 is opaque to C++ (only Lua
                        # reads it); the spec says send (factionId, 1).
                        "b": 1})
    return out


def last_voters(state=None, election_id=0, limit=10):
    """``GetLastVoters_V1`` entries: ``{"voter", "a", "b"}``, newest first.

    The spec's ``VoteEntry`` is ``String, u32 factionId, u32``; ``b`` is sent
    as 1.  Recorded votes win; when none are recorded the list is filled from
    the counts (see :func:`_synthetic_voters`) so the panel is never blank.
    """
    election_id = _as_election_id(election_id)
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 10
    if limit == 0:
        return []
    recorded = []
    if state is not None:
        st = _voting_state(state) or {}
        voters = _lookup(st.get("voters") if isinstance(st, dict) else None,
                         election_id)
        if isinstance(voters, (list, tuple)):
            for entry in voters:
                if not isinstance(entry, dict):
                    continue
                try:
                    recorded.append({"voter": str(entry.get("voter", "") or ""),
                                     "a": int(entry.get("a", FACTION_INVALID)),
                                     "b": int(entry.get("b", 1))})
                except (TypeError, ValueError):
                    continue
    # newest first: apply_vote appends, so reverse the stored order
    recorded.reverse()
    if recorded:
        return recorded[:limit]
    return _synthetic_voters(vote_counts(state, election_id), limit)


def _denominator(state, election_id, total):
    """``VotingStats.f1`` -- never 0, never below the option sum."""
    weight = 0
    if state is not None:
        st = _voting_state(state) or {}
        raw = _lookup(st.get("electorate") if isinstance(st, dict) else None,
                      election_id)
        try:
            weight = int(raw)
        except (TypeError, ValueError):
            weight = 0
    # GUESS: the spec calls f1 "total votes / total elector weight".  We send
    # the option sum (so the three bars add up to 100 %) and honour an explicit
    # ``state["electorate"]`` override if a store ever knows the electorate.
    # Confirm by watching GetVotingStats: percentages must total 100.
    return max(1, int(total), weight)


def voting_stats(state=None, elections=None, now=None, counts_fn=None):
    """``GetVotingStats_V1`` entries: ``{"id", "f1", "votes"}`` per election.

    ``f1`` is the client's percentage denominator (``100*sum/f1``); it is
    ``max(1, sum, electorate)`` so the divide can never produce ``INT_MIN`` or
    a >100 % bar.  Only served if the client asks -- the senate frame itself is
    fed by ``GetVoteCount``.
    """
    infos = _normalise_infos(elections, now)
    entries = []
    for key, _record in infos:
        counts = (counts_fn(key) if counts_fn is not None
                  else vote_counts(state, key))
        entries.append({
            "id": key,
            "f1": _denominator(state, key, sum(counts.values())),
            "votes": counts,
        })
    return entries


#: Handles for the simulated voters (ours, not the game's text): a plausible
#: player name per simulated ballot, deterministic so the list never shuffles.
_SIM_ADJECTIVES = ("Solar", "Tidal", "Nordic", "Verdant", "Cobalt", "Amber",
                   "Granite", "Harbour", "Prairie", "Meridian", "Basalt",
                   "Cloudline", "Driftwood", "Ironwood", "Lantern")
_SIM_NOUNS = ("Envoy", "Trader", "Deacon", "Factor", "Warden", "Scribe",
              "Prospector", "Counsellor", "Foreman", "Delegate", "Steward",
              "Assessor", "Harbourmaster", "Auditor", "Broker")


def simulated_voter_name(slot, index, rank):
    """A stable fake ballot name for ``rank`` in one election."""
    seed = _mix(_VOTE_SALT ^ 0x4E414D45 ^ (int(index) * 13) ^ (int(slot) * 0x9E3779B1)
                ^ (int(rank) * 0xC2B2AE35))
    adjective = _SIM_ADJECTIVES[seed % len(_SIM_ADJECTIVES)]
    noun = _SIM_NOUNS[(seed >> 12) % len(_SIM_NOUNS)]
    number = 1 + (seed >> 24) % 99
    return "%s %s %02d" % (adjective, noun, number)


def _vote_instance(vote, default=None):
    """The election instance a stored vote belongs to (``None`` = unrecorded)."""
    if not isinstance(vote, dict):
        return default
    raw = vote.get("election")
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def instance_votes(state, election_id, now=None, instance=None):
    """The vote log entries that belong to the RUNNING election of ``slot``."""
    slot = _as_election_id(election_id)
    if instance is None:
        instance = election_index(slot, now)
    st = _voting_state(state) or {}
    votes = st.get("votes") if isinstance(st, dict) else None
    out = []
    for vote in votes if isinstance(votes, (list, tuple)) else ():
        if not isinstance(vote, dict):
            continue
        if _as_election_id(vote.get("id", 0)) != slot:
            continue
        recorded = _vote_instance(vote)
        if recorded is not None and recorded != instance:
            continue                       # a vote from an earlier election
        try:
            choice = int(vote.get("choice", FACTION_INVALID))
        except (TypeError, ValueError):
            continue
        if choice not in FACTIONS:
            continue
        out.append({"voter": str(vote.get("voter", "") or ""),
                    "choice": choice,
                    "ts": vote.get("ts", 0),
                    "election": recorded})
    return out


def served_counts(state=None, election_id=0, now=None, simulate=True):
    """``GetVoteCount_V1``: the simulated world **plus** the player's votes.

    This is what the panels actually show.  ``simulate=False`` gives the old
    behaviour (stored counts only) and exists for tests and for
    ``RV_VOTE_SIM=0``.
    """
    slot = _as_election_id(election_id)
    out = simulated_votes(slot, now) if simulate else {f: 0 for f in FACTIONS}
    for vote in instance_votes(state, slot, now):
        out[vote["choice"]] = out.get(vote["choice"], 0) + 1
    return {faction: max(0, int(out.get(faction, 0))) for faction in FACTIONS}


def served_voters(state=None, election_id=0, limit=10, now=None, simulate=True):
    """``GetLastVoters_V1``: the player's ballots first, then the simulated ones.

    The panel shows the newest voters under the bars; a real election count ends
    with strangers, so the tail is filled from the simulation (consistent with
    the bars: the faction mix follows the current counts).
    """
    slot = _as_election_id(election_id)
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 10
    if limit == 0:
        return []
    out = []
    for vote in reversed(instance_votes(state, slot, now)):
        out.append({"voter": vote["voter"] or "Player", "a": vote["choice"], "b": 1})
        if len(out) >= limit:
            return out
    if not simulate:
        return out[:limit]
    counts = simulated_votes(slot, now)
    index = election_index(slot, now)
    # fill from the biggest faction down, so the tail looks like the count
    order = sorted(FACTIONS, key=lambda f: (-counts.get(f, 0), f))
    taken = {f: 0 for f in FACTIONS}
    rank = 0
    while len(out) < limit and rank < limit + SIM_CURVE_STEPS:
        faction = order[rank % len(order)]
        share = counts.get(faction, 0)
        if taken[faction] * len(order) < max(1, share):
            out.append({"voter": simulated_voter_name(slot, index, rank + 1),
                        "a": faction, "b": 1})
            taken[faction] += 1
        rank += 1
        if rank > 4 * (limit + len(order)):
            break
    return out[:limit]


def apply_vote(state, election_id, faction, voter="", now=None):
    """Record one vote in ``state`` and return the election's new counts.

    ``state`` is a plain dict (see :func:`default_state`); it is mutated in
    place -- nothing else happens (no I/O, no clock except ``ts`` when ``now``
    is omitted).  Returns ``{faction: votes}`` ascending, identical to
    ``vote_counts(state, election_id)`` afterwards.

    ``election_id`` is the first u32 of the ``Vote_V1`` request, i.e. the
    ``VotingInfo`` id the client got back (spec ``f1`` -- in this MVP the same
    number as the map key, 0); ``faction`` is the second u32, the tab index.

    ``faction`` must be one of ``0/1/2`` -- that is the proven client enum
    (``FUN_14073ea70``) and the tab index ``Vote()`` sends, so a bad value here
    is a wiring bug and raises ``ValueError`` rather than corrupting the bars.
    """
    if state is None or not isinstance(state, dict):
        raise TypeError("apply_vote needs a mutable plain-dict state")
    election_id = _as_election_id(election_id)
    try:
        faction = int(faction)
    except (TypeError, ValueError):
        raise ValueError("faction must be an int: %r" % (faction,))
    if faction not in FACTIONS:
        raise ValueError("faction must be one of %r (got %r)"
                         % (FACTIONS, faction))

    st = _voting_state(state)
    counts = dict(vote_counts(state, election_id))

    stored = st.get("counts")
    if not isinstance(stored, dict):
        stored = st["counts"] = {}
    counts[faction] = counts.get(faction, 0) + 1
    counts = {f: counts[f] for f in sorted(counts)}
    target = _lookup(stored, election_id)
    if not isinstance(target, dict):
        target = _put(stored, election_id, dict(counts))
    else:
        target.clear()
        target.update(counts)

    votes = st.get("votes")
    if not isinstance(votes, list):
        votes = st["votes"] = []
    votes.append({
        "voter": str(voter or ""),
        "choice": faction,
        "ts": int(time.time() if now is None
                  else _now(now).replace(tzinfo=_UTC).timestamp()),
        "id": election_id,
        # which election this ballot belongs to: a new election must let the
        # player vote again (own vote) and pay again (reward ticket)
        "election": election_index(election_id, now),
    })

    voters = st.get("voters")
    if not isinstance(voters, dict):
        voters = st["voters"] = {}
    bucket = _lookup(voters, election_id)
    if not isinstance(bucket, list):
        bucket = _put(voters, election_id, [])
    bucket.append({"voter": str(voter or ""), "a": faction, "b": 1})

    # the next GetVotingInfos must report f5 = this faction (spec SS5.3) --
    # and it must survive a restart, so the record is CREATED when the save has
    # none for this slot.  Writing only into an existing record is why a World
    # Council vote (slot 1) came back as "not voted" while the senate (slot 0,
    # whose record every save has) was fine.
    record = _ensure_info(st, election_id, now)
    if isinstance(record, dict):
        record["f6"] = faction
        record["election"] = election_index(election_id, now)
    legacy = st.get("votingInfo")
    if election_id == 0 and isinstance(legacy, dict) and legacy is not record:
        legacy["f6"] = faction

    return counts
