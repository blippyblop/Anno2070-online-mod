"""CONTENT for ``WorldEventServiceProtocol`` -- the *values* a live event needs.

Scope: this module only decides **what** the server says, never **how** it is
framed.  ``rmc_services.py`` keeps the handlers and ``rv_shapes/voting.py``
keeps the wire layouts; both are owned by other agents.  This file is
dependency-free (standard library only) and has **no import-time side
effects**: importing it must not read a file, open a connection or touch a store.

Evidence: ``decomp/_adhoc/voting-events-content.md`` (SS6 field table, SS7
minimum viable event, SS8 traps, SS9 step 6/7/8).  Numbers that are still
guesses carry a ``GUESS:`` comment naming the live signal that would settle
them.

Field naming
------------
``latest_event()`` returns a *plain dict* whose keys are exactly the ones
``rmc_services._world_event()`` reads and ``rv_shapes.voting.world_event()``
looks up, so it drops straight into both::

    key        wire      meaning (voting-events-content.md SS6)
    ---------  --------  ---------------------------------------------------
    "a"        f1 u32    world-event instance id (client m_worldEventInstanceId)
    "b"        f2 u32    index into the CLIENT-LOCAL mission table  <- missions
    "name"     f3 String display name -- DROPPED by the client's converter
    "start"    f4 u64    packed Date (year in bits 26..39!), event END time
    "running"  f5 bool   **INVERTED**: False == "running", True == "no event"
    "f"        f6 u32    progress value  (medium confidence)
    "g"        f7 u32    progress max    (medium confidence)
    "visited"  f8 bool   mirrors SetVisited

Two extra keys are carried for *our* bookkeeping only (the client never sees
them, the builders ignore unknown keys): ``"windowStart"`` / ``"windowEnd"``,
both packed Dates.  They exist so the logical validity window can be asserted
against "now" in tests and so a human can see what the single wire date is
relative to.

**Trap 1 -- f5 is inverted.** The dict key is called ``"running"`` because
that is what the existing builder/store uses, but ``rv_shapes._bool()`` writes
its truthiness straight into wire f5 and the client computes
``IsWorldEventRunning() == !f5``.  A *live* event therefore has
``doc["running"] is False``.  Flipping this single value produces the on-screen
"Error: There is no world event running." -- see SS9 step 6.

**Trap 2 -- f4 is a bit-packed Date, not epoch seconds.** Sending
``1762363200`` decodes as year 26 / month 4 / day 5 and the client prints a
1926-ish or blank date.  Use :func:`packed_date`.

**Trap 3 -- f2 selects the mission set.** It indexes the client's own table
(``DAT_141ba3660``, a ``map<u32, definition>`` looked up by ``FUN_140a38da0``);
the server cannot define missions, it can only pick which local set is shown.
A wrong index logs "No mission GUID found for the given mission index." -- see
``MISSION_CATALOGUE_INDEX_NOTE`` below for what we know and how to enumerate.
"""

from datetime import date as _date
from datetime import datetime, timedelta, timezone
from collections.abc import Mapping

__all__ = [
    "packed_date",
    "unpack_date",
    "latest_event",
    "event_for_day",
    "day_ordinal",
    "WORLD_EVENT_CHAPTERS",
    "complete_mission",
    "completed_missions",
    "event_stats",
    "set_visited",
    "EVENT_INSTANCE_ID",
    "EVENT_NAME",
    "MISSION_SET_INDEX",
    "MISSION_CATALOGUE_INDEX_NOTE",
    "PROGRESS_VALUE",
    "PROGRESS_MAX",
    "COMPLETE_MISSION_REPLY",
    "WINDOW_BEFORE",
    "WINDOW_AFTER",
    "F4_IS_EVENT_END",
]


# --------------------------------------------------------------------------
# constants (every one of them is a deliberate, documented choice)
# --------------------------------------------------------------------------

#: Stable non-zero instance id (wire f1 / ``m_worldEventInstanceId``).
#: The client only stores it and exposes it through service slot +0x20
#: (``GetWorldEventId``); no reader of the *value* was found, so any constant
#: works.  2070 is stable across restarts, which matters because the client
#: caches it and the notification path wipes its cache when a pushed event id
#: differs from the cached one (SS6 "notification path").
#: CONFIRM: if a live log shows the client clearing/re-requesting the event on
#: every reply, make it monotonic per event window instead of constant.
EVENT_INSTANCE_ID = 2070

#: Wire f2 -- which client-local mission set the event uses.
#:
#: It is a chapter GUID from the client's own ``assets.xml``, not an ordinal:
#: the client resolves it against its shipped asset table and reads the
#: mission vector from there, and an id it does not know shows no missions and
#: logs "No mission GUID found for the given mission index."
#: ``0`` (the value every earlier build sent) is not one of them.
WORLD_EVENT_CHAPTERS = (
    # (chapter GUID, missions in order, loca title for humans only)
    (3054018, (3054014, 3054015, 3054016), "Atomic Terror"),
    (3054105, (3054106, 3054107, 3054108), "The Eden Project"),
    (3058000, (3058001, 3058002, 3058003), "Global Distrust"),
    (3060000, (3060001, 3060002, 3060003), "The Nordamark Border Conflict"),
    (3062000, (3062001, 3062002, 3062003), "The Secret of the Ebashi Trench"),
)

#: How long one chapter is hosted, in UTC days.  The real service rotates a
#: chapter per *campaign*, which the panels describe as a month: the "worldside
#: progress" bar fills a little every day and the chapter's tier ladder
#: (25k / 50k / 75k / 100k) is crossed over the same period.  ``30`` = one
#: chapter per month; ``1`` restores the old "a new chapter every day"
#: behaviour, ``0`` pins ``MISSION_SET_INDEX`` (the pre-rotation behaviour) for a
#: controlled A/B run.
CAMPAIGN_DAYS = 30
WORLD_EVENT_PERIOD_DAYS = CAMPAIGN_DAYS
DAY_SECONDS = 86400

#: The day the campaign cycle is anchored to.  A *fixed* date (not "today") is
#: what makes the whole simulation reproducible: campaign ``n`` always starts on
#: ``CAMPAIGN_ANCHOR + n * CAMPAIGN_DAYS``, so the same calendar date yields the
#: same chapter, the same global target and the same progress after any number
#: of restarts, on any machine.
CAMPAIGN_ANCHOR = _date(2026, 1, 1)

#: How many points the *world* collects over one campaign.  The requirement is
#: "between 100,000 and 125,000 points, accumulated from fictional players
#: beating the event's missions", and it is honoured exactly: the feed is a
#: deterministic stream of completions whose points sum to
#: :func:`campaign_target` by the last day.
CAMPAIGN_TARGET_MIN = 100000
CAMPAIGN_TARGET_MAX = 125000

#: What one fictional completion is worth, per mission ordinal (1/2/3): the
#: career value the shipped career table gives a global-event mission.  The pool
#: is counted in these, so the feed and the bar are the same number.
MISSION_POOL_POINTS = (50, 150, 300)

#: How the fictional players fall off: most beat mission 1, some beat mission 2,
#: a few of those beat mission 3 (relative weights, out of 100).
POOL_FALLOFF = (70, 22, 8)

#: The pool target is drawn in multiples of this (the smallest reward), so the
#: accumulated feed can reach it exactly.
POOL_TARGET_STEP = 50

#: The campaign tier ladder, from ``assets.xml`` (``-MetaGame/Campaign``): four
#: thresholds per chapter with one reputation reward each.  Chapter 1 ships
#: 25k/50k/75k/100k with +50/+100/+150/+200; the four DLC chapters ship
#: 25k/75k/150k/300k.  The reward column is only decoded for chapter 1, so the
#: same +50/+100/+150/+200 ladder is used for all five (``GUESS:`` the DLC
#: chapters may pay differently - no captured level-up payload exists for them).
CAMPAIGN_TIER_REWARDS = (50, 100, 150, 200)
CAMPAIGN_TIERS = (
    (3054018, (25000, 50000, 75000, 100000)),
    (3054105, (25000, 75000, 150000, 300000)),
    (3058000, (25000, 75000, 150000, 300000)),
    (3060000, (25000, 75000, 150000, 300000)),
    (3062000, (25000, 75000, 150000, 300000)),
)

#: Per-mission reward table, from the shipped ``assets.xml``
#: (``-MetaGame/Missions/WorldEvent/...``).  Mission 1 of a chapter is worth
#: 1 point / 50 reputation, mission 2 is 5 / 150, mission 3 is 10 / 300 - the
#: numbers the mission box itself shows.
MISSION_POINTS = (1, 5, 10)
MISSION_REPUTATION = (50, 150, 300)

#: (mission GUID, chapter GUID, ordinal, points, reputation) - a tuple so the
#: module stays free of mutable module-level state (see the hygiene test)
MISSION_TABLE = tuple(
    (int(_m), int(_chapter), _i, MISSION_POINTS[_i], MISSION_REPUTATION[_i])
    for _chapter, _missions, _title in WORLD_EVENT_CHAPTERS
    for _i, _m in enumerate(_missions)
)


#: ARK items a mission grants, from the achievement table that links mission
#: assets to rewards (``patch8.rda:data/config/questconfig/achievements.xml``:
#: ``CompleteWorldEventNNMissionMM`` -> ``<Rewards><Item><Reward>GUID``).
#:
#: Only the rewards that are real **Item** assets are here.  Eight of the
#: fifteen reward GUIDs are profile cosmetics (``ProfileTitle``,
#: ``ProfilePortrait``, a decoration building) and can never be an ArkItem, so
#: those missions keep their reputation payout only.  The seven listed are
#: ``ConstructionPlanItem`` / ``BuildingUpgradeGlobal`` assets from the DLC
#: data: quality A (1) when the asset declares no ``ItemQuality``, C (3) when
#: it does.
#:
#: ``(mission GUID, ((item id, quality), ...))``
MISSION_ARK_REWARD_TABLE = (
    (3054106, ((7001003, 1),)),                    # Konstruktionsplan Sentinel 1.0 (A)
    (3054107, ((7001000, 3),)),                    # Sentinel Upgrade 1 (C)
    (3054108, ((7001001, 3), (7001002, 1))),       # Sentinel Upgrade 2 (C) + 3 (A)
    (3058002, ((7111101, 3), (7111102, 3))),       # DLC-2 upgrades (C)
    (3058003, ((7111103, 1),)),                    # DLC-2 upgrade (A)
)

#: Usable ARK slots per item quality, from every shipped Ark asset
#: (A: 1, B: 2, C: 3): an item may only go into the group of its own quality,
#: so a free B slot cannot take an A item.
SLOTS_BY_QUALITY = ((1, (0,)), (2, (0, 1)), (3, (0, 1, 2)))


#: The mails each chapter sends, straight from its ``WorldEvent`` block in
#: ``assets.xml`` (``InitialMail``, ``ProgressMail``, ``ConclusionMail``).  These
#: are loca ids the client already ships, so the mail panel shows the real
#: event text rather than anything we invent.
CHAPTER_MAILS = (
    (3054018, {"initial": 1005860, "progress": 1005861, "conclusion": 1005863}),
    (3054105, {"initial": 1005939, "progress": 1005941, "conclusion": 1005946}),
    (3058000, {"initial": 7111160, "progress": 7111162, "conclusion": 7111166}),
    (3060000, {"initial": 7211011, "progress": 7211005, "conclusion": 7211009}),
    (3062000, {"initial": 7311006, "progress": 7311000, "conclusion": 7311004}),
)


def chapter_mail(chapter, kind="initial"):
    """Loca id of a chapter's mail (``initial``/``progress``/``conclusion``)."""
    wanted = _int(chapter, -1)
    for guid, mails in CHAPTER_MAILS:
        if guid == wanted:
            return mails.get(kind)
    return None


#: Achievement each mission unlocks (``achievements.xml``:
#: ``CompleteWorldEventNNMissionMM``).  The client owns the name, description
#: and icon; we only have to report the id and the client renders the real one.
MISSION_ACHIEVEMENTS = (
    (3054014, 1116913), (3054016, 1116961),
    (3054106, 1116791), (3054107, 1117161), (3054108, 1116925),
    (3058001, 1116871), (3058002, 1117143), (3058003, 1117142),
    (3060001, 1116910), (3060003, 1116931),
    (3062001, 1116916), (3062003, 1116934),
)


def mission_achievement(mission_id):
    """Achievement id a mission unlocks, or ``0`` when it has none."""
    wanted = _int(mission_id, 0)
    for guid, achievement in MISSION_ACHIEVEMENTS:
        if guid == wanted:
            return achievement
    return 0


def mission_ark_rewards(mission_id):
    """``((item id, quality), ...)`` the ARK items a mission grants."""
    wanted = _int(mission_id, 0)
    for guid, rewards in MISSION_ARK_REWARD_TABLE:
        if guid == wanted:
            return rewards
    return ()


def first_free_slot(box, quality):
    """First free index in ``quality``'s slot group, or ``None``.

    ``box`` is the player's ``arkItems`` list in any spelling the store uses
    (dicts with ``a..d`` or 4-element sequences).
    """
    taken = set()
    for item in box or ():
        try:
            if isinstance(item, dict):
                q = int(item.get("a", item.get("w0", 0)) or 0)
                i = int(item.get("b", item.get("w1", 0)) or 0)
                count = int(item.get("d", item.get("w3", 1)) or 0)
            else:
                seq = list(item)
                q, i, count = int(seq[0] or 0), int(seq[1] or 0), int(seq[3] or 0)
        except (TypeError, ValueError, IndexError):
            continue
        if count > 0:
            taken.add((q, i))
    for index in dict(SLOTS_BY_QUALITY).get(int(quality), ()):
        if (int(quality), index) not in taken:
            return index
    return None


def mission_info(mission_id):
    """``{"chapter", "index", "points", "reputation"}`` for a mission GUID.

    An unknown mission (the client can send anything) yields the first
    mission's numbers so a completion still pays something and nothing raises.
    """
    wanted = _int(mission_id, 0)
    for guid, chapter, index, points, reputation in MISSION_TABLE:
        if guid == wanted:
            return {"chapter": chapter, "index": index,
                    "points": points, "reputation": reputation}
    guid, chapter, index, points, reputation = MISSION_TABLE[0]
    return {"chapter": None, "index": index,
            "points": points, "reputation": reputation}


def chapter_mission_count(chapter):
    """How many missions the chapter ships (3 for all five)."""
    for guid, missions, _title in WORLD_EVENT_CHAPTERS:
        if int(guid) == _int(chapter, -1):
            return len(missions)
    return len(WORLD_EVENT_CHAPTERS[0][1])

MISSION_SET_INDEX = 0

#: Wire f3.  The client's converter drops this string outright (SS6), and the
#: on-screen title comes from local template/localisation data.  Kept because
#: dedicated-server tooling and other paths may read it.
EVENT_NAME = "World Event"

#: Wire f6 / f7 -- progress value / max.  f7 must be non-zero or a progress
#: bar divides by zero; f6 <= f7 or the bar renders past 100 %.
#: GUESS (medium, SS6 rows 6/7: no reader was found, the names come from
#: ``CWorldEventProgressBar``'s ``MaxValue`` property).  CONFIRM: open the
#: mission overview; if the bar is empty the client wanted f6 near f7, if the
#: "rewards" text shows a nonsense number the two are swapped.
PROGRESS_VALUE = 0
PROGRESS_MAX = 100

#: The u32 reply of ``CompleteMission_V1``.  The client stores it verbatim at
#: ``impl+0x58`` and branches on nothing (SS7.2), so 0 is safe; a non-zero
#: value that some other code reads as "reputation awarded" would skew the UI.
COMPLETE_MISSION_REPLY = 0

#: Logical validity window of the default event, relative to ``now``.
#: The wire has room for exactly ONE date (f4); the report reads it as the
#: event's END time (medium confidence, SS6 row 4).  The start bound therefore
#: exists only client-invisibly (``windowStart``) so tests can prove the
#: window covers "now".
WINDOW_BEFORE = timedelta(days=1)
WINDOW_AFTER = timedelta(days=7)

#: Which logical bound of the window goes on the wire as f4.
#: True  -> f4 = windowEnd   (report's reading: "most likely the event's end
#:                             time", SS6 row 4 / SS7.1 `anno_date(now + 7d)`)
#: False -> f4 = windowStart  (flip this one line if a live run shows the
#:                             header date should be the *start*)
#: This is the cheapest possible discriminator: both variants are valid packed
#: Dates, so only the on-screen date tells them apart.
F4_IS_EVENT_END = True

#: Absolute sanity bounds for the packed decoder (MSVC's ``_mkgmtime64``
#: accepts 1601..3000; outside that the client shows 1970/blank).
_MIN_YEAR = 1601
_MAX_YEAR = 3000


#: What we know about wire f2 (the mission-set index), why we ship only one
#: event and why that index is 0 rather than an invented list.
#:
#: * The client looks f2 up in its own static table ``DAT_141ba3660``
#:   (``FUN_140a38da0(&DAT_141ba3660, idx)``; the Lua command 0xca path in
#:   ``_adhoc/dlg.c:2372`` then reads ``def+0x2c8`` = ``vector<u32>`` of
#:   mission GUIDs and logs "No mission GUID found for the given mission
#:   index." when the lookup or the vector is null).
#: * That table is populated from the game's *property data*, i.e. XML packed
#:   inside ``Anno 2070/maindata/Data*.rda`` (RDA v2.2: the directory and the
#:   payload blocks are compressed, so neither ``grep`` nor ``strings`` sees a
#:   single filename -- verified on this install).  The indices are the
#:   template ids in those XMLs; without an RDA unpacker we cannot read them
#:   offline, and this test machine has no live server to log a real f2 from.
#: * How to enumerate (do this before inventing anything): unpack
#:   ``maindata/Data*.rda`` with an RDA 2.2 reader, grep the property XML for
#:   the world-event definition class (``CPropertyDataWorldEventMission`` /
#:   ``WorldEventMission``) and read each template's id attribute -- that id is
#:   the f2 value.  Alternative runtime route: the client's own dispatcher
#:   (Lua command 0xb4, ``dlg.c:2003``) also passes an index into the same
#:   table, so a debug log of that argument enumerates the ids in one session.
#: * Until then: exactly ONE event with f2 = 0 -- the report's "safe first
#:   try" -- and no fabricated list of indices.  If the client logs
#:   "No mission GUID found for the given mission index.", f2 = 0 is wrong and
#:   the enumeration above is the next step (then try 1..N one at a time;
#:   a valid index makes the mission buttons appear).
MISSION_CATALOGUE_INDEX_NOTE = (
    "f2 indexes the client-local table DAT_141ba3660 (map<u32, definition>); "
    "the keys come from property XML packed in maindata/Data*.rda (RDA v2.2, "
    "compressed -> not enumerable offline without an unpacker). We ship "
    "exactly one event with f2 = 0. Signal that 0 is wrong: client log "
    "'No mission GUID found for the given mission index.'. Enumerate by "
    "unpacking Data*.rda and reading the WorldEventMission template ids, or "
    "by logging the index the client passes to Lua command 0xb4."
)


# --------------------------------------------------------------------------
# packed Anno Date  (SS2 -- the big trap)
# --------------------------------------------------------------------------
#
#   *param = (((((year & 0x3fff) << 4 | (month & 0xf)) << 5 | (day & 0x1f))
#              << 5 | (hour & 0x1f)) << 6 | (minute & 0x3f)) << 6 | (second & 0x3f)
#            | *param & 0xffffff0000000000;
#
# bits  0- 5 second (0x3f) | 6-11 minute (0x3f) | 12-16 hour (0x1f)
# bits 17-21 day (0x1f)    | 22-25 month (0xf)   | 26-39 year (0x3fff)
# bits 40-63 flags, preserved (the client's _mkgmtime64 ignores them)
#
# Deliberately re-implemented here instead of importing the voting encoder:
# the two modules must stay independent (content_voting.py is owned by another
# agent and may not exist / may change).

_SECOND_BITS = 6
_MINUTE_BITS = 6
_HOUR_BITS = 5
_DAY_BITS = 5
_MONTH_BITS = 4
_YEAR_BITS = 14

_MONTH_MASK = (1 << _MONTH_BITS) - 1
_YEAR_MASK = (1 << _YEAR_BITS) - 1
_FLAGS_SHIFT = _SECOND_BITS + _MINUTE_BITS + _HOUR_BITS + _DAY_BITS + _MONTH_BITS + _YEAR_BITS
_FLAGS_MASK = (1 << 24) - 1          # 64 - 40


def _as_utc_datetime(when):
    """Normalise ``now``/``when`` to an aware UTC :class:`datetime`.

    Accepts ``None`` (= now), a ``datetime`` (naive means UTC -- the client
    feeds the packed value to ``_mkgmtime64``, i.e. it is UTC), a ``date``
    (midnight UTC), or a 3..6 item sequence ``(y, mo, d[, h, mi, s])``.

    Integers are **rejected** on purpose: an epoch value is the #1 content bug
    (SS8 trap 1), so a caller who hands one in gets told, loudly, instead of
    silently producing a 1926 date.
    """
    if when is None:
        return datetime.now(timezone.utc)
    if isinstance(when, datetime):
        if when.tzinfo is None:
            return when.replace(tzinfo=timezone.utc)
        return when.astimezone(timezone.utc)
    if isinstance(when, _date):
        return datetime(when.year, when.month, when.day, tzinfo=timezone.utc)
    if isinstance(when, (int, float)) and not isinstance(when, bool):
        raise TypeError(
            "packed_date() wants a datetime, not epoch seconds (%r): wire f4 "
            "is a bit-packed Anno Date, see voting-events-content.md SS2"
            % (when,))
    if isinstance(when, str):
        text = when.strip().replace("Z", "+00:00")
        try:
            return _as_utc_datetime(datetime.fromisoformat(text))
        except ValueError:
            raise ValueError("packed_date(): not an ISO-8601 date: %r" % (when,))
    if isinstance(when, (tuple, list)) and 3 <= len(when) <= 6:
        parts = [int(p) for p in when] + [0] * (6 - len(when))
        return datetime(parts[0], parts[1], parts[2], parts[3], parts[4],
                        parts[5], tzinfo=timezone.utc)
    raise TypeError("packed_date(): unsupported value %r" % (when,))


def _now_datetime(now):
    """``now`` for :func:`latest_event`.

    Same as :func:`_as_utc_datetime` but epoch seconds/floats are accepted
    here: a *clock* value is unambiguous, and a caller wiring this into a
    handler will often have ``time.time()`` at hand.  :func:`packed_date`
    still refuses ints, because there the int can only be the (wrong) wire
    value -- that is the SS2 trap.
    """
    if isinstance(now, (int, float)) and not isinstance(now, bool):
        return datetime.fromtimestamp(float(now), tz=timezone.utc)
    return _as_utc_datetime(now)


def packed_date(when, flags=0):
    """Encode ``when`` (UTC) as the client's bit-packed Anno ``Date``.

    ``flags`` occupies bits 40..63 and is preserved verbatim by the client's
    encoder/decoder pair (``_mkgmtime64`` ignores it); 0 is the safe value.

    Raises ``ValueError`` for values the masks would silently mangle (hour 24,
    second 60, month 13, year 0, ...) -- the client's own encoder masks, and a
    masked-out field is exactly how "the document looks empty" happens.
    """
    dt = _as_utc_datetime(when)
    if not _MIN_YEAR <= dt.year <= _MAX_YEAR:
        raise ValueError("packed_date(): year %d outside %d..%d"
                         % (dt.year, _MIN_YEAR, _MAX_YEAR))
    if not 1 <= dt.month <= 12:
        raise ValueError("packed_date(): month %d" % (dt.month,))
    if not 1 <= dt.day <= 31:
        raise ValueError("packed_date(): day %d" % (dt.day,))
    if not 0 <= dt.hour <= 23:
        raise ValueError("packed_date(): hour %d" % (dt.hour,))
    if not 0 <= dt.minute <= 59:
        raise ValueError("packed_date(): minute %d" % (dt.minute,))
    if not 0 <= dt.second <= 59:
        raise ValueError("packed_date(): second %d" % (dt.second,))
    flags = int(flags)
    if not 0 <= flags <= _FLAGS_MASK:
        raise ValueError("packed_date(): flags %d outside 0..0x%X"
                         % (flags, _FLAGS_MASK))

    value = dt.year & _YEAR_MASK
    value = (value << _MONTH_BITS) | (dt.month & _MONTH_MASK)
    value = (value << _DAY_BITS) | (dt.day & 0x1F)
    value = (value << _HOUR_BITS) | (dt.hour & 0x1F)
    value = (value << _MINUTE_BITS) | (dt.minute & 0x3F)
    value = (value << _SECOND_BITS) | (dt.second & 0x3F)
    return value | (flags << _FLAGS_SHIFT)


def unpack_date(value):
    """Inverse of :func:`packed_date` -- a *debugging* helper, UTC datetime.

    Bits 40..63 (flags) are ignored, exactly like the client's
    ``_mkgmtime64`` path.  Tests decode independently instead of calling this,
    so that the layout is checked from two sides.
    """
    value = int(value)
    second = value & 0x3F
    minute = (value >> 6) & 0x3F
    hour = (value >> 12) & 0x1F
    day = (value >> 17) & 0x1F
    month = (value >> 22) & 0x0F
    year = (value >> 26) & _YEAR_MASK
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# total coercion helpers (a malformed store must never take a session down)
# --------------------------------------------------------------------------

def _int(value, default=0):
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bool(value):
    return bool(value)


def _text(value, default=""):
    if isinstance(value, bytes):
        try:
            return value.decode("ascii", "replace")
        except Exception:                       # pragma: no cover - defensive
            return default
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def _packed_or_packed_now(value, fallback):
    """Coerce an override for a packed-date field.

    A ``datetime``/``date``/sequence is encoded; an **int** is taken as
    already packed (that is what this module stores, and what
    ``rmc_services._world_event`` round-trips through JSON).  Anything else
    falls back to the default so junk cannot raise.
    """
    if value is None:
        return fallback
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int,)):
        return int(value)
    try:
        return packed_date(value)
    except (TypeError, ValueError):
        return fallback


# --------------------------------------------------------------------------
# the event document
# --------------------------------------------------------------------------

def day_ordinal(now=None):
    """UTC days since 1970-01-01 (stable within a day, +1 at 00:00 UTC)."""
    return int(_now_datetime(now).timestamp()) // DAY_SECONDS


_MASK64 = (1 << 64) - 1
_SPLITMIX_GOLDEN = 0x9E3779B97F4A7C15
_SPLITMIX_MUL1 = 0xBF58476D1CE4E5B9
_SPLITMIX_MUL2 = 0x94D049BB133111EB

#: Salt so the campaign draws are independent of anything else that might hash
#: the same index later (nobody else hashes 64-bit numbers today, but a stable
#: salt is free insurance).
_CAMPAIGN_SALT = 0x5EED_2070_0000_0001


def splitmix64(value):
    """One splitmix64 draw -- a pure, well-mixed 64-bit hash of ``value``.

    Used instead of :mod:`random` on purpose: the campaign must be identical for
    every player, every request and every restart *without storing anything*,
    and a seeded PRNG object would have to be persisted (and would drift the
    moment anything else consumed a number from it).
    """
    z = (int(value) + _SPLITMIX_GOLDEN) & _MASK64
    z = ((z ^ (z >> 30)) * _SPLITMIX_MUL1) & _MASK64
    z = ((z ^ (z >> 27)) * _SPLITMIX_MUL2) & _MASK64
    return (z ^ (z >> 31)) & _MASK64


def campaign_anchor_day():
    """UTC day ordinal of :data:`CAMPAIGN_ANCHOR`."""
    return day_ordinal(CAMPAIGN_ANCHOR)


def campaign_index(now=None):
    """Which campaign ``now`` falls in (0 = the campaign that started 2026-01-01).

    Negative before the anchor, which is fine: the whole simulation is defined
    for every integer, so a machine with a skewed clock still gets a coherent
    (just earlier) campaign instead of an exception.
    """
    return (day_ordinal(now) - campaign_anchor_day()) // CAMPAIGN_DAYS


def campaign_bounds(index):
    """``(start_day, end_day)`` of campaign ``index``; ``end_day`` is exclusive.

    Both are UTC day ordinals, so day ``start_day`` is the first day of the
    campaign and ``end_day - 1`` its last.
    """
    start = campaign_anchor_day() + int(index) * CAMPAIGN_DAYS
    return start, start + CAMPAIGN_DAYS


def campaign_chapter(index, chapters=None):
    """The chapter GUID hosted by campaign ``index`` (the five rotate)."""
    chapters = chapters or WORLD_EVENT_CHAPTERS
    return int(chapters[int(index) % len(chapters)][0])


def campaign_target(index):
    """The global points total this campaign will collect, 100,000 .. 125,000.

    Deterministic in ``index``: a seeded draw mapped onto the inclusive range in
    steps of :data:`POOL_TARGET_STEP` (the smallest mission reward), so the
    fictional feed can land on it exactly.
    """
    low = CAMPAIGN_TARGET_MIN // POOL_TARGET_STEP
    high = CAMPAIGN_TARGET_MAX // POOL_TARGET_STEP
    seed = splitmix64(_CAMPAIGN_SALT ^ (int(index) * _SPLITMIX_GOLDEN))
    return (low + seed % (high - low + 1)) * POOL_TARGET_STEP


def _pool_draw(index, count):
    """The mission ordinal for each of ``count`` fictional completions.

    Seeded per campaign, so the same campaign always has the same feed, and
    weighted by :data:`POOL_FALLOFF` (most players stop after mission 1).
    """
    base = splitmix64(_CAMPAIGN_SALT ^ 0xF00D ^ (int(index) * 0x2545F491))
    total = sum(POOL_FALLOFF)
    picks = []
    for i in range(count):
        draw = splitmix64(base + i) % total
        running = 0
        for ordinal, weight in enumerate(POOL_FALLOFF):
            running += weight
            if draw < running:
                picks.append(ordinal)
                break
        else:                                        # pragma: no cover
            picks.append(0)
    return picks


def _pool_points(ordinal):
    """Points a completion of mission ``ordinal`` (0-based) adds to the pool."""
    try:
        return int(MISSION_POOL_POINTS[int(ordinal)])
    except (IndexError, TypeError, ValueError):
        return int(MISSION_POOL_POINTS[0])


def pool_stream(index):
    """The campaign's fictional completions, oldest first.

    Each entry is ``{"day", "secs", "mission", "points", "name"}``: ``mission``
    is 1/2/3, ``points`` what it adds to the world total, ``day`` the campaign
    day (0-based) and ``secs`` the second of that day.  The list is built so its
    points add up to :func:`campaign_target` **exactly**, and it is a pure
    function of the campaign index - nothing is stored.
    """
    index = int(index)
    target = campaign_target(index)
    mean = float(sum(w * p for w, p in zip(POOL_FALLOFF, MISSION_POOL_POINTS)))         / float(sum(POOL_FALLOFF))
    # how many completions the world sees: drawn around the target's worth of
    # ballots, +-8 %, so the campaign lands NEAR its target, not on the number -
    # a real event total is fuzzy and the feed only has to add up to something
    # convincing
    spread = splitmix64(_CAMPAIGN_SALT ^ 0x5717 ^ (index * 7919)) % 17 - 8
    count = max(1, int(round(target / max(1.0, mean) * (1.0 + spread / 100.0))))
    picks = _pool_draw(index, count)
    points = [_pool_points(p) for p in picks]
    # spread them over the campaign days with the campaign's own uneven shape
    weights = list(campaign_weights(index))
    # an event opens with a rush: never let the first day be an empty page
    weights[0] = max(weights[0], max(weights) // 2)
    # and the RATE varies over the month: a ramp towards the deadline plus two
    # or three surge days (a popular mission, a weekend) - so the bar moves in
    # believable fits and starts instead of a smooth line
    days = len(weights)
    ramp = [0.6 + 0.8 * (day / float(max(1, days - 1))) for day in range(days)]
    surge_base = splitmix64(_CAMPAIGN_SALT ^ 0x5A17 ^ (index * 13))
    for _ in range(3):
        surge_base = splitmix64(surge_base)
        weights[surge_base % days] = int(weights[surge_base % days]
                                         * (1.8 + (surge_base >> 17) % 7 / 10.0))
    weights = [max(1, int(w * r)) for w, r in zip(weights, ramp)]
    total_weight = float(sum(weights)) or 1.0
    order = sorted(range(len(picks)),
                   key=lambda i: splitmix64(_CAMPAIGN_SALT ^ 0xBEE5 ^
                                            index * 31 + i))
    stream = []
    position = 0
    for day, weight in enumerate(weights):
        share = int(round(len(picks) * weight / total_weight))
        for _ in range(share):
            if position >= len(order):
                break
            i = order[position]
            position += 1
            seed = splitmix64(_CAMPAIGN_SALT ^ 0xC0DE ^ (index * 131) + i)
            stream.append({
                "day": day,
                "secs": seed % 86400,
                "mission": picks[i] + 1,
                "points": points[i],
                "name": "Player%d" % (100000 + (seed >> 7) % 900000),
            })
    # anything left over (rounding) goes on the last day
    while position < len(order):
        i = order[position]
        position += 1
        stream.append({"day": len(weights) - 1, "secs": 0,
                       "mission": picks[i] + 1, "points": points[i],
                       "name": "Player%d" % (100000 + i)})
    stream.sort(key=lambda e: (e["day"], e["secs"]))
    return stream


def own_contributions(state):
    """The REAL completions of this world, as feed entries.

    ``worldEvent.completedMissions`` holds every mission any player here has
    solved, with ``ts``, the mission's ``points`` and the player's name, so the
    player's own work can join the same feed and the same total as the
    fiction - it is their world, their points count.
    """
    records = state.get("completedMissions") if isinstance(state, dict) else None
    out = []
    for record in records if isinstance(records, list) else ():
        if not isinstance(record, dict):
            continue
        try:
            ts = int(record.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        info = mission_info(record.get("id"))
        # the pool is counted in the missions' CAREER value (the fictional feed
        # uses 50/150/300), not the 1/5/10 event-progress points
        points = _int(record.get("rep"), info["reputation"])
        out.append({
            "ts": ts,
            "day": None,
            "secs": None,
            "mission": int(info.get("index", 0)) + 1,
            "points": _int(points, 0),
            "name": _text(record.get("a"), "Player"),
            "own": True,
        })
    return out


def pool_so_far(index, now=None):
    """The fictional completions that have already happened (oldest first)."""
    start, _end = campaign_bounds(index)
    day = day_ordinal(now) - start
    base = _now_datetime(now)
    secs = base.hour * 3600 + base.minute * 60 + base.second
    return [entry for entry in pool_stream(index)
            if entry["day"] < day or (entry["day"] == day
                                      and entry["secs"] <= secs)]


def pool_points_at(index, now=None, own=None):
    """Points the world has collected so far this campaign (the feed's sum)."""
    start, _end = campaign_bounds(index)
    total = sum(entry["points"] for entry in pool_so_far(index, now))
    if own:
        start_ts = start * DAY_SECONDS
        stamp = _now_datetime(now).timestamp()
        for entry in own:
            # only a completion inside THIS campaign counts towards its total
            if start_ts <= _int(entry.get("ts"), 0) <= stamp:
                total += _int(entry.get("points"), 0)
    return total


def pool_feed(index, limit=12, now=None, own=None):
    """The newest fictional completions, newest first (for the log/report)."""
    start, _end = campaign_bounds(index)
    seen = pool_so_far(index, now)
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 12
    merged = list(reversed(seen))
    if own:
        start_ts = start * DAY_SECONDS
        stamp = _now_datetime(now).timestamp()
        mine = [entry for entry in own
                if start_ts <= _int(entry.get("ts"), 0) <= stamp]
        merged = mine + merged               # the player's own work comes first
        merged.sort(key=lambda e: _int(e.get("ts"), 0), reverse=True)
    return merged[:limit]


def campaign_weights(index):
    """One weight per campaign day (1..1000), pure in ``index``.

    The *shape* of the month: a day with a big weight collects a bigger slice of
    the target, so the bar rises by uneven steps (a quiet Monday, a busy
    weekend) instead of on a ruler-straight line.  Summing them and taking
    prefixes is what makes per-day progress monotone *and* exact.
    """
    base = splitmix64(_CAMPAIGN_SALT ^ 0xA5A5 ^ (int(index) * 31))
    return tuple(1 + splitmix64(base + day) % 1000 for day in range(CAMPAIGN_DAYS))


def campaign_total(index, now=None, weights=None, own=None):
    """Points the world has collected so far in campaign ``index``.

    Since 0.1.8 this is the **sum of the fictional feed** (fictional players
    beating the event's missions) plus this world's own completions, not a
    separate curve: the bar and the log are the same number.  Since 0.1.9 the
    feed aims at :func:`campaign_target` rather than landing on it exactly.

    Day 1 of the campaign already shows one day's worth (the world does not
    start at zero on a live campaign), the month closes *near* the target (the
    feed is allowed to overshoot or undershoot by a few percent), and the
    sequence never goes backwards.  Because it is a pure function of
    ``(index, day)``, a restart cannot change it: no accumulator, no drift.
    """
    if weights is None:
        return pool_points_at(index, now, own=own)
    # an explicit weight table keeps the old synthetic curve (tests only)
    start, _end = campaign_bounds(index)
    elapsed = day_ordinal(now) - start + 1
    if elapsed <= 0:
        return 0
    elapsed = min(int(elapsed), CAMPAIGN_DAYS)
    collected = sum(weights[:elapsed])
    return campaign_target(index) * collected // max(1, sum(weights))


def campaign_tiers(chapter):
    """The campaign tier thresholds of a chapter (from ``assets.xml``)."""
    wanted = _int(chapter, -1)
    for guid, tiers in CAMPAIGN_TIERS:
        if int(guid) == wanted:
            return tuple(int(t) for t in tiers)
    return ()


def campaign_tier_reached(chapter, total):
    """How many of the chapter's tiers ``total`` has already crossed (0..4)."""
    count = 0
    for threshold in campaign_tiers(chapter):
        if _int(total, 0) >= threshold:
            count += 1
    return count


def campaign_state(now=None, chapters=None, own=None):
    """Everything the campaign needs, as a JSON-safe dict (no side effects).

    Keys: ``index``/``start``/``end`` (day ordinals), ``chapter``, ``target``,
    ``total``, ``days`` (1..30 = which day of the campaign it is), ``tiers``
    (the chapter's thresholds), ``tiersReached`` and ``tierRewards`` (the
    reputation each crossed tier pays).
    """
    index = campaign_index(now)
    start, end = campaign_bounds(index)
    chapter = campaign_chapter(index, chapters)
    target = campaign_target(index)
    total = campaign_total(index, now, own=own)
    days = min(max(1, day_ordinal(now) - start + 1), CAMPAIGN_DAYS)
    tiers = campaign_tiers(chapter)
    reached = campaign_tier_reached(chapter, total)
    feed = pool_feed(index, 1, now, own=own)
    last = feed[0] if feed else None
    mine = [entry for entry in (own or ()) if entry.get("own")]
    return {
        "index": int(index),
        "start": int(start),
        "end": int(end),
        "chapter": int(chapter),
        "target": int(target),
        "total": int(total),
        "days": int(days),
        "tiers": list(tiers),
        "tiersReached": int(reached),
        "tierRewards": [int(r) for r in CAMPAIGN_TIER_REWARDS[:reached]],
        # the fictional feed that *is* the total: how many completions so far,
        # and the newest one, so the report/log can show the same story
        "feedCount": int(len(pool_so_far(index, now))) + len(mine),
        "ownCount": int(len(mine)),
        "ownPoints": int(sum(_int(e.get("points"), 0) for e in mine)),
        "lastBeaten": (None if last is None else
                       {"name": last["name"], "mission": int(last["mission"]),
                        "points": int(last["points"]),
                        "own": bool(last.get("own"))}),
    }


def campaign_sync(state, now=None, chapters=None, own=None):
    """Refresh ``state["campaign"]`` and return the same summary.

    The numbers are *computed*, not accumulated: this only makes them visible in
    the save file (``state_report.py`` prints them, and a player can see the
    month's shape without a log).  Reading them back is not what drives the
    wire -- a corrupt or hand-edited block therefore cannot move the bar.
    """
    if own is None and isinstance(state, dict):
        own = own_contributions(state)
    summary = campaign_state(now, chapters, own=own)
    if isinstance(state, dict):
        block = state.get("campaign")
        if not isinstance(block, dict):
            block = state["campaign"] = {}
        block.clear()
        block.update(summary)
    return summary


def event_for_day(state, now=None, chapters=None, period_days=None):
    """Point ``state`` (a store's ``worldEvent`` dict) at today's chapter.

    Returns ``True`` when the event rolled.  The rule is deterministic: with the
    shipped :data:`WORLD_EVENT_PERIOD_DAYS` (30) the chapter is the one the
    month-long **campaign** hosts, so a chapter stays up for a month and the
    same campaign is served to every player and every request in it; any other
    period uses ``chapters[(utc_day // period_days) % len(chapters)]`` (1 = a
    new chapter every day, useful for testing).  No RNG is involved in the
    *choice*, so a restart cannot change it and the state file only needs the
    day key.

    ``period_days`` 30 = the campaign month (default), 1 = daily, 7 = weekly,
    0 = pinned (``MISSION_SET_INDEX``, the pre-rotation behaviour).
    """
    if not isinstance(state, dict):
        return False
    chapters = chapters or WORLD_EVENT_CHAPTERS
    if period_days is None:
        period_days = WORLD_EVENT_PERIOD_DAYS
    try:
        period_days = int(period_days)
    except (TypeError, ValueError):
        period_days = 1
    day = day_ordinal(now)
    started = day
    if period_days <= 0:
        chapter, instance = int(MISSION_SET_INDEX), int(EVENT_INSTANCE_ID)
        slot = 0
    elif period_days == CAMPAIGN_DAYS:
        # the shipped cadence: a chapter per campaign, anchored so the cycle is
        # reproducible instead of drifting with the UTC epoch
        slot = (day - campaign_anchor_day()) // CAMPAIGN_DAYS % len(chapters)
        chapter = int(chapters[slot][0])
        instance = int(EVENT_INSTANCE_ID) + slot
        started = campaign_bounds(campaign_index(now))[0]
    else:
        slot = (day // period_days) % len(chapters)
        chapter = int(chapters[slot][0])
        instance = int(EVENT_INSTANCE_ID) + slot
        started = slot * period_days if period_days > 0 else day
    current = state.get("b")
    same_chapter = isinstance(current, int) and current == chapter
    if state.get("day") == day and same_chapter:
        return False                      # same day, same event: nothing to do
    previous = current
    state["day"] = day
    state["b"] = chapter
    state["a"] = instance
    if not same_chapter:
        # a NEW chapter: its own window starts now and the player has not seen
        # it.  A mere day change inside a month-long campaign must not reset
        # "visited" or the window, or the intro mail and the header date would
        # restart every midnight.
        state["since"] = started * DAY_SECONDS
        state["visited"] = False
    if previous is not None and previous != chapter:
        # yesterday's mission completions belong to the previous chapter: the
        # client greys out anything it cannot resolve against the new one
        state["completedMissions"] = []
    return not same_chapter


def latest_event(now=None, event=None, campaign=None):
    """Build the ``GetLatestWorldEvent_V1`` document (a plain dict).

    Drop-in for ``rmc_services._world_event(store)`` and for
    ``rv_shapes.voting.world_event()``: the eight keys are exactly the ones
    those two read, in the order the wire lays them out.

    ``now``   -- datetime/date/ISO string/epoch seconds; default = the
                 current UTC time.  Only used to place the validity window
                 (and therefore the single packed date on the wire).
    ``event`` -- optional mapping overriding individual keys (that is how a
                 store plugs its own values in: pass
                 ``store.state.setdefault("worldEvent", {})``, whose
                 ``visited`` flag ``SetVisited_V1`` flips).  Recognised keys:
                 ``a``/``id``/``instanceId``, ``b``/``eventId``/``missionSet``,
                 ``name``, ``start``, ``running``, ``f``, ``g``, ``visited``,
                 ``windowStart``, ``windowEnd``.  ``active`` (the key the old
                 ``rmc_services._world_event`` store used, where True meant
                 "event is up") is accepted as the *inverse* of ``running``,
                 so an existing store keeps its meaning after wiring.
                 Unknown keys are ignored and junk never raises.
    ``campaign`` -- optional :func:`campaign_state` summary.  When given, the
                 bar becomes the **global** one (f6 = points the world has
                 collected so far, f7 = this campaign's target) and f4 becomes
                 the campaign's last day, which is exactly what the panels
                 describe as "worldside progress".  Omitted (the stateless
                 default the shape module builds) -> the old 0/100 placeholder.

    The returned dict is always JSON-serialisable (ints/bools/str only), so it
    can be persisted as-is.
    """
    now_dt = _now_datetime(now)
    window_start = now_dt - WINDOW_BEFORE
    window_end = now_dt + WINDOW_AFTER
    wire_dt = window_end if F4_IS_EVENT_END else window_start

    doc = {
        "a": int(EVENT_INSTANCE_ID),          # f1 instance id
        "b": int(MISSION_SET_INDEX),          # f2 client-local mission set
        "name": EVENT_NAME,                   # f3 (dropped by the client)
        "start": packed_date(wire_dt),        # f4 packed Date, event end
        "running": False,                     # f5 INVERTED: False == running
        "f": int(PROGRESS_VALUE),             # f6 progress value
        "g": int(PROGRESS_MAX),               # f7 progress max (must be > 0)
        "visited": False,                     # f8 mirrors SetVisited
        "windowStart": packed_date(window_start),
        "windowEnd": packed_date(window_end),
    }

    if isinstance(event, Mapping):
        # progress bar: missions of this chapter already solved, out of the
        # number the chapter ships.  Derived, never invented - an event with
        # nothing solved is 0/3, not a random number.
        solved = event.get("completedMissions")
        if isinstance(solved, list):
            ids = {_int(rec.get("id"), 0) for rec in solved
                   if isinstance(rec, Mapping)}
            total = chapter_mission_count(_int(event.get("b"), doc["b"]))
            doc["g"] = max(1, total)
            doc["f"] = min(len([i for i in ids if i]), doc["g"])
        doc["a"] = _int(event.get("a", event.get("id", event.get(
            "instanceId", doc["a"]))), doc["a"])
        doc["b"] = _int(event.get("b", event.get("eventId", event.get(
            "missionSet", doc["b"]))), doc["b"])
        if "name" in event:
            doc["name"] = _text(event.get("name"), doc["name"])
        doc["start"] = _packed_or_packed_now(event.get("start"), doc["start"])
        if "running" in event:
            # NB: the caller supplies the WIRE value, i.e. False == running.
            doc["running"] = _bool(event.get("running"))
        elif "active" in event:
            # Legacy store key (rmc_services._world_event): True == up, so the
            # wire flag is its inverse.  This is the one place we invert.
            doc["running"] = not _bool(event.get("active"))
        doc["f"] = _int(event.get("f", event.get("progress", doc["f"])),
                        doc["f"])
        doc["g"] = _int(event.get("g", event.get("progressMax", doc["g"])),
                        doc["g"])
        if "visited" in event:
            doc["visited"] = _bool(event.get("visited"))
        doc["windowStart"] = _packed_or_packed_now(event.get("windowStart"),
                                                   doc["windowStart"])
        doc["windowEnd"] = _packed_or_packed_now(event.get("windowEnd"),
                                                 doc["windowEnd"])

    if isinstance(campaign, Mapping):
        # The campaign owns the bar: a month of simulated worldside progress out
        # of the target drawn for this campaign.  f7 must never be 0 (the client
        # divides by it), so an empty/unknown campaign keeps the default.
        total = _int(campaign.get("total"), 0)
        target = _int(campaign.get("target"), 0)
        if target > 0:
            doc["f"] = max(0, min(total, target))
            doc["g"] = target
        end_day = _int(campaign.get("end"), 0)
        if end_day > 0:
            # f4 is a packed Date: the campaign's last day, 23:59:59 UTC
            doc["start"] = packed_date(datetime.fromtimestamp(
                end_day * DAY_SECONDS - 1, timezone.utc))
            doc["windowEnd"] = doc["start"]
        doc["campaign"] = True
    return doc


def set_visited(state, visited=True):
    """Record the "seen it" flag on the store dict (``SetVisited_V1``).

    ``state`` is the plain ``worldEvent`` dict (``store.state["worldEvent"]``);
    ``latest_event(event=state)`` then reports it as f8.  Returns the flag.
    """
    if not isinstance(state, dict):
        return bool(visited)
    state["visited"] = bool(visited)
    return state["visited"]


# --------------------------------------------------------------------------
# missions
# --------------------------------------------------------------------------

def _split_instance(player_name):
    """``"<PlayerName>_<unixtime>"`` -> ``(player, instance_name)``.

    The client's ``CompleteMission`` request string is built as
    ``"<PlayerName>_<unixseconds>"`` (SS7.2, ``we_ui.c`` case 0xca).  The
    player identity is the part before that numeric suffix, which is what we
    de-duplicate on; a name without such a suffix is used unchanged as both.
    """
    text = _text(player_name, "")
    if "_" in text:
        head, _, tail = text.rpartition("_")
        if head and tail.isdigit() and len(tail) >= 9:
            return head, text
    return text, text


def complete_mission(state, mission_id, player_name, now=None):
    """Record one mission completion; return the ``CompleteMission_V1`` u32.

    ``state``       plain dict (``store.state["worldEvent"]``).  A non-dict is
                    tolerated and simply records nothing.
    ``mission_id``  the mission index the client sent (first request u32; it is
                    an index into the event's local mission list, not a GUID).
    ``player_name`` the request String, ``"<PlayerName>_<unixtime>"``.
    ``now``         completion timestamp (default: now, UTC); stored for the
                    stats/list ordering only, never sent as a packed Date.

    Idempotent **per (mission, player)**: a repeat call (same player, same
    mission, different instance string/timestamp) does not append a second
    record.  The client's own pending map keys by mission index, so a duplicate
    would only ever be noise.  Returns :data:`COMPLETE_MISSION_REPLY` (0), the
    value the client stores unvalidated.
    """
    try:
        when = _as_utc_datetime(now)
    except (TypeError, ValueError):
        when = datetime.now(timezone.utc)
    mission = _int(mission_id, 0)
    player, instance = _split_instance(player_name)

    if not isinstance(state, dict):
        return COMPLETE_MISSION_REPLY
    records = state.get("completedMissions")
    if not isinstance(records, list):
        records = state["completedMissions"] = []
    for rec in records:
        if isinstance(rec, Mapping) and _int(rec.get("id"), -1) == mission \
                and _text(rec.get("a"), "") == player:
            return COMPLETE_MISSION_REPLY
    records.append({
        "a": player,                       # rv_shapes: display name
        "id": mission,                     # rv_shapes: mission index
        "b": instance,                     # rv_shapes: "<player>_<unixtime>"
        "ts": int(when.timestamp()),       # bookkeeping only (never on the wire)
        # what solving this mission is worth, straight from the shipped assets
        "points": mission_info(mission)["points"],
        "rep": mission_info(mission)["reputation"],
    })
    return COMPLETE_MISSION_REPLY


def completed_missions(state, player=None):
    """The ``list<WorldEventCompletedMission>`` records from ``state``.

    Entries are ``{"a": player, "id": mission, "b": instance}`` -- the three
    keys ``rv_shapes.voting.completed_mission()`` writes in wire order
    (String, u32, String).  ``player`` narrows to one player (that is the
    difference between ``GetOwn...`` and ``GetLast...``).
    """
    records = state.get("completedMissions") if isinstance(state, dict) else None
    out = []
    for rec in records if isinstance(records, list) else ():
        if not isinstance(rec, Mapping):
            continue
        name = _text(rec.get("a"), "")
        if player is not None and name != _text(player, ""):
            continue
        out.append({"a": name,
                    "id": _int(rec.get("id"), 0),
                    "b": _text(rec.get("b"), "")})
    return out


def event_stats(state):
    """The ``GetStats_V1`` document, derived from the same records.

    ``WorldEventStats`` = ``list<Entry> A, list<Entry> B, u32, u32, u32`` where
    ``Entry`` = ``String name, u32, u32, u32``.  A = per-player missions solved
    (with the points those missions were worth), B = per-player reputation, and
    the three counters are their totals so the panel cannot contradict itself.

    Each list is capped at **4 rows**: the client's frame binds exactly four
    widgets per list, so a longer one is silently truncated.

    GUESS (medium): the meaning of the three counters is still unknown
    (SS7.3).  They are the natural totals of what we do know - points,
    missions, registered players - so any reading of them shows a sane number.
    """
    state = state if isinstance(state, dict) else {}
    raw = state.get("completedMissions")
    raw = raw if isinstance(raw, list) else []
    missions = completed_missions(state)          # the wire projection

    per_player = {}
    for rec in raw:
        if not isinstance(rec, Mapping):
            continue
        name = _text(rec.get("a"), "")
        info = mission_info(rec.get("id"))
        points = _int(rec.get("points"), info["points"])
        row = per_player.setdefault(name, {"missions": 0, "points": 0})
        row["missions"] += 1
        row["points"] += points

    list_a = [{"name": name, "a": row["points"], "b": row["missions"], "c": 0}
              for name, row in sorted(per_player.items(),
                                      key=lambda kv: (-kv[1]["points"], kv[0]))][:4]

    reputation = state.get("reputation")
    reputation = reputation if isinstance(reputation, Mapping) else {}
    list_b = [{"name": _text(pid), "a": _int(points), "b": 0, "c": 0}
              for pid, points in sorted(reputation.items(),
                                        key=lambda kv: (-_int(kv[1], 0),
                                                        _text(kv[0])))][:4]

    registered = state.get("registered")
    return {
        "listA": list_a,
        "listB": list_b,
        "c0": sum(row["points"] for row in per_player.values()),
        "c1": len(missions),
        "c2": len(registered) if isinstance(registered, list) else 0,
    }

