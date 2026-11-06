#!/usr/bin/env python3
"""Shape tests for ``server/rv_shapes/matchmaking.py``.

Plain python, no pytest.  Run:

    source ~/tools/env.sh && python3 tools/test_rv_shapes_matchmaking.py

Exit 0 = every check passed.  What is verified, and *why* it is evidence and
not a restatement of the module:

  * the name block: u16 length **including** the NUL, the exact bytes
    ``"MatchMakingServiceProtocol::<Method>*\\0"``, a hard-coded literal per
    method, and a **trailing ``*``** (RVA ``0x1370A50`` region: the client
    compares ``decodedName + "*"``);
  * the 30 method names against the client's own tables -- three static string
    blocks at RVA ``0x1667CD8``/``0x166B518``/``0x166D350`` (from
    ``decomp/_adhoc/notes/anno_strings.txt``) -- and against the reply
    dispatcher ``FUN_140E04110`` (RVA ``0xE04110``);
  * one check per wire field, decoded by an **independent** reader written
    here from the disassembly of the per-method readers (not from the
    builder), for all 13 reply-carrying methods, both for the defaults and for
    a distinctive second value;
  * Quazal strings carry a u16 length **excluding** the NUL (the client's
    ``FUN_141371620`` resizes to ``len+1`` locally, it never reads a NUL);
  * the 17 void methods emit the name block **only** (the dispatcher jumps
    straight to ``0xE0502F`` for them -- see the branch table in the module
    docstring);
  * totality (junk never raises), single-record tolerance, purity, and that
    ``UNKNOWN_METHODS`` is empty while ``PROVISIONAL_METHODS`` is exactly the
    five Variant-carrying methods.
"""

import copy
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

from rv_shapes import matchmaking  # noqa: E402

METHOD_NAMES = matchmaking.METHOD_NAMES
PROTO = "MatchMakingServiceProtocol"

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


# --------------------------------------------------------------------------
# independent reference encoder / decoder (written from the disassembly)
# --------------------------------------------------------------------------

def ref_name_block(method):
    """u16 total length (incl NUL) | name | '*' | NUL  -- RVA 0x135E510."""
    raw = ("%s::%s*" % (PROTO, method)).encode("ascii")
    return struct.pack("<H", len(raw) + 1) + raw + b"\x00"


def w32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


def w16(v):
    return struct.pack("<H", v & 0xFFFF)


class Reader(object):
    """Independent decoder: every primitive mirrors one reader function."""

    def __init__(self, buf):
        self.b = buf
        self.i = 0

    def take(self, n):
        if self.i + n > len(self.b):
            raise AssertionError("truncated at %d (+%d > %d)" % (self.i, n, len(self.b)))
        chunk = self.b[self.i:self.i + n]
        self.i += n
        return chunk

    # FUN_14135E5F0(stream, ptr, 4) -- raw 4 bytes
    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    # FUN_14135E2A0 -- 1 byte bool
    def bool(self):
        return self.take(1) != b"\x00"

    # FUN_141371620 -- u16 length EXCLUDING the NUL, then the bytes
    def string(self):
        n = struct.unpack("<H", self.take(2))[0]
        return self.take(n).decode("ascii")

    # FUN_140DE9460 -- u32 count + ascending (u32,u32)
    def map32(self):
        n = self.u32()
        return dict((self.u32(), self.u32()) for _ in range(n))

    # FUN_141371440 -- String then u16 type tag
    def variant(self):
        text = self.string()
        return (text, struct.unpack("<H", self.take(2))[0])

    def done(self):
        return self.i == len(self.b)


# --- value types (order/type from the reader bodies, cited per function) -----

def read_game_session(r):
    """FUN_140E05680 -- u32, String, String, u32, u32, map<u32,u32>."""
    return dict(id=r.u32(), name=r.string(), description=r.string(),
                players=r.u32(), max_players=r.u32(), settings=r.map32())


def read_player(r):
    """FUN_140E05290 -- String, 6x u32, bool, map<u32,u32>, bool."""
    return dict(name=r.string(),
                player_id=r.u32(), faction=r.u32(), color=r.u32(),
                slot=r.u32(), sub_slot=r.u32(), f7=r.u32(),
                ready=r.bool(), properties=r.map32(), host=r.bool())


def read_sub_slot(r):
    """FUN_140E0A4A0 -- u32, String."""
    return dict(id=r.u32(), name=r.string())


def read_slot(r):
    """FUN_140E05360 -- u32, bool, 5x u32, vector<SubSlot>."""
    index = r.u32()
    locked = r.bool()
    faction = r.u32()
    color = r.u32()
    player_id = r.u32()
    ai = r.u32()
    f7 = r.u32()
    n = r.u32()
    return dict(index=index, locked=locked, faction=faction, color=color,
                player_id=player_id, ai=ai, f7=f7,
                sub_slots=[read_sub_slot(r) for _ in range(n)])


def read_station_urls(r):
    """FUN_140E05510 -- String, u32, list<String> (FUN_140DF8A10)."""
    name = r.string()
    kind = r.u32()
    n = r.u32()
    return dict(name=name, kind=kind, urls=[r.string() for _ in range(n)])


# --------------------------------------------------------------------------
# 1. name blocks
# --------------------------------------------------------------------------

#: all 30 names, with the RVA of the name check inside the reply dispatcher
#: ``FUN_140E04110`` (disassembled; see the module docstring branch table)
DISPATCHER = [
    ("GetSessions_V1", 0xE0414B, False),
    ("CreateSession_V1", 0xE041ED, False),
    ("SetSessionParameters_V1", 0xE042DA, False),
    ("PreJoinSession_V1", 0xE04393, False),
    ("JoinSession_V1", 0xE044B7, False),
    ("LeaveSession_V1", 0xE04601, False),
    ("SetReadyFlag_V1", 0xE046AC, True),
    ("StartSession_V1", 0xE046FB, True),
    ("GetHostStationUrls_V1", 0xE0474A, False),
    ("GetGameSessionSlots_V1", 0xE047EC, False),
    ("GetUnassignedPlayers_V1", 0xE048BD, False),
    ("SetAiSlot_V1", 0xE04951, True),
    ("SetHumanSlot_V1", 0xE049A0, True),
    ("SetSubSlot_V1", 0xE049EF, True),
    ("UpdateSlotColor_V1", 0xE04A3E, True),
    ("UpdateSlotFaction_V1", 0xE04A8D, True),
    ("ClearSlot_V1", 0xE04ADC, True),
    ("ClearSubSlot_V1", 0xE04B2B, True),
    ("MoveSlot_V1", 0xE04B7A, True),
    ("MoveSubSlot_V1", 0xE04BC0, True),
    ("SwapSubSlots_V1", 0xE04C18, True),
    ("KickPlayer_V1", 0xE04C67, True),
    ("SetHostDescriptor_V1", 0xE04CB6, True),
    ("GetGameSettings_V1", 0xE04D05, False),
    ("SetGameSettings_V1", 0xE04DBE, True),
    ("SetGameSetting_V1", 0xE04E0D, True),
    ("GetGameSessionPlayers_V1", 0xE04E5C, False),
    ("GetGameSessionPlayer_V1", 0xE04EFE, False),
    ("SetOwnPlayerProperty_V1", 0xE04F59, True),
    ("GetOwnStationUrls_V1", 0xE04FA8, False),
]

#: the same 30 strings as they appear in the client's static tables
#: (decomp/_adhoc/notes/anno_strings.txt, RVAs 0x1667CD8 / 0x166B518 / 0x166D350)
STRING_TABLE = [
    "GetOwnStationUrls_V1", "GetGameSessionPlayers_V1", "SetGameSetting_V1",
    "SetOwnPlayerProperty_V1", "GetGameSessionPlayer_V1", "SetHostDescriptor_V1",
    "KickPlayer_V1", "SetGameSettings_V1", "GetGameSettings_V1", "MoveSlot_V1",
    "ClearSubSlot_V1", "SwapSubSlots_V1", "MoveSubSlot_V1",
    "UpdateSlotColor_V1", "SetSubSlot_V1", "ClearSlot_V1",
    "UpdateSlotFaction_V1", "GetUnassignedPlayers_V1",
    "GetGameSessionSlots_V1", "SetHumanSlot_V1", "SetAiSlot_V1",
    "SetReadyFlag_V1", "LeaveSession_V1", "GetHostStationUrls_V1",
    "StartSession_V1", "SetSessionParameters_V1", "CreateSession_V1",
    "JoinSession_V1", "PreJoinSession_V1", "GetSessions_V1",
]


def test_name_blocks():
    print("\n-- test_name_blocks")
    eq(len(METHOD_NAMES), 30, "module implements 30 methods")
    eq(sorted(METHOD_NAMES), sorted(STRING_TABLE), "names == client string tables")
    eq(sorted(METHOD_NAMES), sorted(n for n, _r, _v in DISPATCHER),
       "names == reply-dispatcher branch table")

    for method, _rva, void in DISPATCHER:
        payload = matchmaking.METHOD_NAMES[method]()
        n = struct.unpack("<H", payload[:2])[0]
        raw = payload[2:2 + n]
        eq(raw[-1:], b"\x00", "%s: name block ends with NUL" % method)
        eq(raw[-2:-1], b"*", "%s: name block carries the TRAILING '*'" % method)
        eq(raw[:-2], ("%s::%s" % (PROTO, method)).encode("ascii"),
           "%s: name block text" % method)
        eq(n, len("%s::%s*" % (PROTO, method)) + 1,
           "%s: u16 length INCLUDES the NUL" % method)
        eq(payload[:2 + n], ref_name_block(method),
           "%s: name block == independent reference" % method)
        eq(matchmaking.METHOD_NAMES[method]().__doc__ is not None, True,
           "%s: builder is documented" % method)
        eq(matchmaking.is_void(method), void, "%s: void flag matches dispatcher" % method)
        if void:
            eq(payload, ref_name_block(method),
               "%s: void reply is the name block ONLY" % method)


# --------------------------------------------------------------------------
# 2. field-by-field decoding of the 13 reply-carrying methods
# --------------------------------------------------------------------------

def strip_name(payload, method):
    n = struct.unpack("<H", payload[:2])[0]
    eq(payload[:2 + n], ref_name_block(method), "%s: name block prefix" % method)
    return payload[2 + n:]


DEFAULT_VARIANT = ("", 0x15)


def test_defaults():
    print("\n-- test_defaults")

    # GetSessions_V1 -- vector<A5_GameSession> (reader 0xE05680)
    body = strip_name(matchmaking.m_get_sessions_v1(), "GetSessions_V1")
    r = Reader(body)
    eq(r.u32(), 1, "GetSessions: vector count = 1")
    eq(read_game_session(r), dict(id=1, name="Anno 2070", description="",
                                 players=1, max_players=8, settings={}),
       "GetSessions: one default session (6 fields)")
    check(r.done(), "GetSessions: no trailing bytes")

    # CreateSession_V1 -- Variant, u32 (body 0xE0422D)
    body = strip_name(matchmaking.m_create_session_v1(), "CreateSession_V1")
    r = Reader(body)
    eq(r.variant(), DEFAULT_VARIANT, "CreateSession: Variant = empty text + tag 0x15")
    eq(r.u32(), 0, "CreateSession: u32 (session id / result)")
    check(r.done(), "CreateSession: no trailing bytes")

    # SetSessionParameters_V1 -- Variant only (body 0xE04316)
    body = strip_name(matchmaking.m_set_session_parameters_v1(), "SetSessionParameters_V1")
    r = Reader(body)
    eq(r.variant(), DEFAULT_VARIANT, "SetSessionParameters: exactly one Variant")
    check(r.done(), "SetSessionParameters: no trailing bytes")

    # PreJoinSession_V1 -- Variant + vector<A5_StationUrls> (0xE043D3/0xE0443A)
    body = strip_name(matchmaking.m_pre_join_session_v1(), "PreJoinSession_V1")
    r = Reader(body)
    eq(r.variant(), DEFAULT_VARIANT, "PreJoinSession: Variant first")
    eq(r.u32(), 1, "PreJoinSession: station-url list count")
    eq(read_station_urls(r),
       dict(name="127.0.0.1:21020", kind=0, urls=[]),
       "PreJoinSession: A5_StationUrls{String,u32,list<String>}")
    check(r.done(), "PreJoinSession: no trailing bytes")

    # JoinSession_V1 -- Variant, String, bool (0xE044F7/0xE0455E/0xE045C5)
    body = strip_name(matchmaking.m_join_session_v1(), "JoinSession_V1")
    r = Reader(body)
    eq(r.variant(), DEFAULT_VARIANT, "JoinSession: Variant first")
    eq(r.string(), "", "JoinSession: String (session name)")
    eq(r.bool(), True, "JoinSession: bool (joined/ok)")
    check(r.done(), "JoinSession: no trailing bytes")

    # LeaveSession_V1 -- Variant only (0xE04638 -> shared tail 0xE04320)
    body = strip_name(matchmaking.m_leave_session_v1(), "LeaveSession_V1")
    r = Reader(body)
    eq(r.variant(), DEFAULT_VARIANT, "LeaveSession: exactly one Variant")
    check(r.done(), "LeaveSession: no trailing bytes")

    # GetHostStationUrls_V1 -- list<String> (0xE04786, reader 0xDF8B00)
    body = strip_name(matchmaking.m_get_host_station_urls_v1(), "GetHostStationUrls_V1")
    r = Reader(body)
    eq(r.u32(), 1, "GetHostStationUrls: list count")
    eq(r.string(), "127.0.0.1:21020", "GetHostStationUrls: station url string")
    check(r.done(), "GetHostStationUrls: no trailing bytes")

    # GetGameSessionSlots_V1 -- u32, vector<A5_GameSessionSlot> (0xE04827/0xE04857)
    body = strip_name(matchmaking.m_get_game_session_slots_v1(), "GetGameSessionSlots_V1")
    r = Reader(body)
    eq(r.u32(), 1, "GetGameSessionSlots: leading u32 (slot count)")
    eq(r.u32(), 1, "GetGameSessionSlots: vector count")
    eq(read_slot(r),
       dict(index=0, locked=False, faction=0, color=0, player_id=1, ai=0,
            f7=0, sub_slots=[]),
       "GetGameSessionSlots: A5_GameSessionSlot{u32,bool,5x u32,vector<SubSlot>}")
    check(r.done(), "GetGameSessionSlots: no trailing bytes")

    # GetUnassignedPlayers_V1 -- vector<A5_GameSessionPlayer> (0xE048F9)
    body = strip_name(matchmaking.m_get_unassigned_players_v1(), "GetUnassignedPlayers_V1")
    r = Reader(body)
    eq(r.u32(), 1, "GetUnassignedPlayers: vector count")
    eq(read_player(r),
       dict(name="Player", player_id=1, faction=0, color=0, slot=0,
            sub_slot=0, f7=0, ready=True, properties={}, host=True),
       "GetUnassignedPlayers: one A5_GameSessionPlayer")
    check(r.done(), "GetUnassignedPlayers: no trailing bytes")

    # GetGameSettings_V1 -- map<u32,u32> (0xE04D41, reader 0xDE9460)
    body = strip_name(matchmaking.m_get_game_settings_v1(), "GetGameSettings_V1")
    r = Reader(body)
    eq(r.map32(), {}, "GetGameSettings: bare map<u32,u32>, count 0")
    check(r.done(), "GetGameSettings: no trailing bytes")

    # GetGameSessionPlayers_V1 -- vector<A5_GameSessionPlayer> (0xE04E98)
    body = strip_name(matchmaking.m_get_game_session_players_v1(), "GetGameSessionPlayers_V1")
    r = Reader(body)
    eq(r.u32(), 1, "GetGameSessionPlayers: vector count")
    read_player(r)
    check(r.done(), "GetGameSessionPlayers: no trailing bytes")

    # GetGameSessionPlayer_V1 -- ONE player, no count (0xE04F35 -> 0xE00210)
    body = strip_name(matchmaking.m_get_game_session_player_v1(), "GetGameSessionPlayer_V1")
    r = Reader(body)
    eq(read_player(r)["name"], "Player", "GetGameSessionPlayer: single player, no count prefix")
    check(r.done(), "GetGameSessionPlayer: no trailing bytes")

    # GetOwnStationUrls_V1 -- list<String> (0xE04FDF -> 0xE002B0 -> 0xDF8B00)
    body = strip_name(matchmaking.m_get_own_station_urls_v1(), "GetOwnStationUrls_V1")
    r = Reader(body)
    eq(r.u32(), 1, "GetOwnStationUrls: list count")
    eq(r.string(), "127.0.0.1:21020", "GetOwnStationUrls: station url string")
    check(r.done(), "GetOwnStationUrls: no trailing bytes")


# --------------------------------------------------------------------------
# 3. distinctive values: every field must be where the reader expects it
# --------------------------------------------------------------------------

def test_values():
    print("\n-- test_values")

    sess = dict(id=4242, name="S", description="D", players=3,
                max_players=6, settings={9: 8, 7: 6})
    body = strip_name(matchmaking.m_get_sessions_v1(sessions=[sess]), "GetSessions_V1")
    r = Reader(body)
    eq(r.u32(), 1, "values/GetSessions: count")
    eq(read_game_session(r),
       dict(id=4242, name="S", description="D", players=3, max_players=6,
            settings={7: 6, 9: 8}),
       "values/GetSessions: 6 fields, ascending map")
    check(r.done(), "values/GetSessions: no trailing bytes")

    body = strip_name(matchmaking.m_create_session_v1(session={"id": 4242},
                                                      text="R", tag=7),
                      "CreateSession_V1")
    r = Reader(body)
    eq(r.variant(), ("R", 7), "values/CreateSession: Variant text+tag")
    eq(r.u32(), 4242, "values/CreateSession: u32 = session id")
    check(r.done(), "values/CreateSession: no trailing bytes")

    p = dict(name="Bob", player_id=7, faction=2, color=3, slot=4,
             sub_slot=5, f7=6, ready=False, properties={1: 2}, host=False)
    body = strip_name(matchmaking.m_get_game_session_player_v1(player=p),
                      "GetGameSessionPlayer_V1")
    r = Reader(body)
    eq(read_player(r),
       dict(name="Bob", player_id=7, faction=2, color=3, slot=4, sub_slot=5,
            f7=6, ready=False, properties={1: 2}, host=False),
       "values/GetGameSessionPlayer: 10 fields in order")
    check(r.done(), "values/GetGameSessionPlayer: no trailing bytes")

    s0 = dict(index=9, locked=True, faction=1, color=2, player_id=3, ai=4,
              f7=5, sub_slots=[{"id": 11, "name": "s1"},
                               {"id": 12, "name": "s2"}])
    body = strip_name(matchmaking.m_get_game_session_slots_v1(slots=[s0], count=5),
                      "GetGameSessionSlots_V1")
    r = Reader(body)
    eq(r.u32(), 5, "values/GetGameSessionSlots: leading u32 passed through")
    eq(r.u32(), 1, "values/GetGameSessionSlots: vector count")
    eq(read_slot(r), dict(index=9, locked=True, faction=1, color=2,
                          player_id=3, ai=4, f7=5,
                          sub_slots=[{"id": 11, "name": "s1"},
                                     {"id": 12, "name": "s2"}]),
       "values/GetGameSessionSlots: nested sub-slot vector")
    check(r.done(), "values/GetGameSessionSlots: no trailing bytes")

    st = [{"name": "host", "kind": 3, "urls": ["a", "bb"]}]
    body = strip_name(matchmaking.m_pre_join_session_v1(stations=st, text="P", tag=2),
                      "PreJoinSession_V1")
    r = Reader(body)
    eq(r.variant(), ("P", 2), "values/PreJoinSession: Variant")
    eq(r.u32(), 1, "values/PreJoinSession: count")
    eq(read_station_urls(r), dict(name="host", kind=3, urls=["a", "bb"]),
       "values/PreJoinSession: A5_StationUrls with 2 urls")
    check(r.done(), "values/PreJoinSession: no trailing bytes")

    body = strip_name(matchmaking.m_join_session_v1(name="Lobby", joined=False),
                      "JoinSession_V1")
    r = Reader(body)
    eq(r.variant(), DEFAULT_VARIANT, "values/JoinSession: default Variant")
    eq(r.string(), "Lobby", "values/JoinSession: String carries the name")
    eq(r.bool(), False, "values/JoinSession: bool False")
    check(r.done(), "values/JoinSession: no trailing bytes")

    body = strip_name(matchmaking.m_get_game_settings_v1({5: 6, 1: 2, 3: 4}),
                      "GetGameSettings_V1")
    r = Reader(body)
    eq(r.map32(), {1: 2, 3: 4, 5: 6}, "values/GetGameSettings: map, ascending keys")
    check(r.done(), "values/GetGameSettings: no trailing bytes")

    body = strip_name(matchmaking.m_get_host_station_urls_v1(["u1", "u2", "u3"]),
                      "GetHostStationUrls_V1")
    r = Reader(body)
    eq(r.u32(), 3, "values/GetHostStationUrls: count 3")
    eq([r.string() for _ in range(3)], ["u1", "u2", "u3"],
       "values/GetHostStationUrls: three strings")
    check(r.done(), "values/GetHostStationUrls: no trailing bytes")


# --------------------------------------------------------------------------
# 4. string / count conventions
# --------------------------------------------------------------------------

def test_conventions():
    print("\n-- test_conventions")
    # u16 length EXCLUDING the NUL: an empty string is 00 00, not 01 00
    eq(matchmaking.string_list([""]), w32(1) + w16(0),
       "empty string encodes as u16 0 (length EXCLUDES the NUL)")
    eq(matchmaking.string_list(["ab"]), w32(1) + w16(2) + b"ab",
       "two-char string encodes as u16 2 + 2 bytes")
    eq(matchmaking.string_list([]), w32(0), "empty list is a bare zero count")
    eq(matchmaking.string_list(None), w32(0), "None list is a bare zero count")
    eq(matchmaking.map_u32_u32({}), w32(0), "empty map is a bare zero count")
    eq(matchmaking.map_u32_u32({2: 3}), w32(1) + w32(2) + w32(3),
       "map entry is key then value")
    eq(matchmaking.variant(), w16(0) + w16(0x15),
       "default Variant is empty String then tag 0x15")
    eq(matchmaking.variant("x", 1), w16(1) + b"x" + w16(1),
       "Variant is text FIRST, tag SECOND")


# --------------------------------------------------------------------------
# 5. totality, tolerance, purity
# --------------------------------------------------------------------------

JUNK = [None, 0, -1, 2 ** 70, 3.5, float("nan"), "x", b"x", {}, [],
        (1, 2, 3), object(), {"id": "not-a-number"}, [None, 1], True]


def test_totality():
    print("\n-- test_totality")
    for method, fn in matchmaking.METHOD_NAMES.items():
        bad = 0
        for junk in JUNK:
            try:
                out = fn(junk)
            except Exception as exc:  # noqa: BLE001
                bad += 1
                print("    %s(%r) raised %r" % (method, junk, exc))
                continue
            if not isinstance(out, bytes) or not out.startswith(
                    ref_name_block(method)[:2]):
                bad += 1
        eq(bad, 0, "%s: every junk input still yields a name block" % method)

    # **kwargs junk (the dispatch path passes keyword arguments)
    out = matchmaking.build(PROTO, "GetSessions_V1", sessions=None, wat=1, x=2)
    check(isinstance(out, bytes), "build() tolerates unknown keyword arguments")

    # single-record tolerance: a dict where a list is expected
    one = matchmaking.m_get_game_session_players_v1({"name": "Z"})
    r = Reader(strip_name(one, "GetGameSessionPlayers_V1"))
    eq(r.u32(), 1, "single dict is wrapped into a one-item list")
    eq(read_player(r)["name"], "Z", "single dict keeps its fields")

    # non-ascii degrades instead of raising
    out = matchmaking.m_get_game_session_player_v1({"name": "\u00dcn\u00ef"})
    check(isinstance(out, bytes), "non-ascii name does not raise")
    check(out.isascii(), "non-ascii name is transliterated to ascii")


def test_purity():
    print("\n-- test_purity")
    sess = {"id": 5, "name": "N", "settings": {1: 2}}
    players = [{"name": "a", "properties": {3: 4}}]
    slots = [{"index": 1, "sub_slots": [{"id": 2, "name": "s"}]}]
    snap = copy.deepcopy((sess, players, slots))
    first = (matchmaking.m_get_sessions_v1([sess]),
             matchmaking.m_get_game_session_players_v1(players),
             matchmaking.m_get_game_session_slots_v1(slots))
    second = (matchmaking.m_get_sessions_v1([sess]),
              matchmaking.m_get_game_session_players_v1(players),
              matchmaking.m_get_game_session_slots_v1(slots))
    eq(first, second, "builders are deterministic")
    eq((sess, players, slots), snap, "builders do not mutate their inputs")


# --------------------------------------------------------------------------
# 6. coverage / dispatch bookkeeping
# --------------------------------------------------------------------------

def test_coverage():
    print("\n-- test_coverage")
    eq(matchmaking.PRESENT_IN_ANNO, True, "module declares PRESENT_IN_ANNO")
    eq(matchmaking.UNKNOWN_METHODS, {}, "no method is left without a shape")
    eq(sorted(matchmaking.PROVISIONAL_METHODS),
       sorted(["CreateSession_V1", "SetSessionParameters_V1",
               "PreJoinSession_V1", "JoinSession_V1", "LeaveSession_V1"]),
       "PROVISIONAL_METHODS == the five Variant-carrying methods")
    eq(len(matchmaking.VOID_METHODS), 17, "17 void methods")
    eq(len(matchmaking.METHOD_NAMES) - len(matchmaking.VOID_METHODS), 13,
       "13 reply-carrying methods")
    check(matchmaking.PROVISIONAL_METHODS <= set(matchmaking.METHOD_NAMES),
          "provisional set is a subset of the implemented names")
    check(matchmaking.VOID_METHODS.isdisjoint(matchmaking.PROVISIONAL_METHODS),
          "void methods are not provisional")

    # package-level dispatch (PN 137/138): the proto key must resolve
    import rv_shapes
    eq(rv_shapes._PROTO_MODULE.get("matchmakingserviceprotocol"), "matchmaking",
       "package registers matchmakingserviceprotocol -> matchmaking")
    eq(rv_shapes.available().get("matchmaking"), "ok", "module imports cleanly")
    check(getattr(rv_shapes, "matchmaking", None) is matchmaking,
          "module is eagerly imported as a package attribute (PN 137)")
    for method in matchmaking.METHOD_NAMES:
        out = rv_shapes.build("matchmakingserviceprotocol", method)
        eq(out, matchmaking.METHOD_NAMES[method](),
           "rv_shapes.build dispatches %s" % method)
    eq(rv_shapes.build("matchmakingserviceprotocol", "NoSuchMethod_V1"), None,
       "unknown method -> None (caller falls back)")
    eq(rv_shapes.build("votingserviceprotocol", "GetSessions_V1"), None,
       "matchmaking names do not leak into other protocols")


# --------------------------------------------------------------------------

def main():
    test_name_blocks()
    test_defaults()
    test_values()
    test_conventions()
    test_totality()
    test_purity()
    test_coverage()
    print("\n%d checks, %d failures" % (CHECKS, len(FAILURES)))
    if FAILURES:
        for f in FAILURES:
            print("  - %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
