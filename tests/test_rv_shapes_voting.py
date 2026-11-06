#!/usr/bin/env python3
"""Shape tests for ``server/rv_shapes/voting.py``.

Plain python, no pytest.  Run:

    source ~/tools/env.sh && python3 tools/test_rv_shapes_voting.py

Exit 0 = every check passed.  Verified here:
  * exact method-name blocks (hard-coded literals + independent reference),
  * byte-exact field encodings for fixed inputs of all eight value types,
  * count-prefix correctness (empty list/map => 0x00000000, no stray bytes),
  * totality: junk input never raises and still yields bytes,
  * single-record tolerance, purity (no input mutation, repeatable bytes),
  * METHOD_NAMES + UNKNOWN_METHODS cover exactly the 20 methods in
    decomp/batch/step3_voting_events/VOTING_EVENTS.md, disjointly, and now
    implement all 20 (GetVoteCount_V1 via the reader FUN_140de9460,
    UploadWorldEventTemplate_V1 via the writer FUN_140e09bb0),
  * the two former unknowns: exact map<u32,u32> / WorldEventTemplate bytes,
    including the 12 u32 block, the mission map and the two orderings,
  * the module stays free of I/O and mimic_server imports.
"""

import copy
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "server"))

from rv_shapes import voting  # noqa: E402

VOTING = "VotingServiceProtocol"
WE = "WorldEventServiceProtocol"

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


def ref_name_block(proto, method):
    """Independent implementation: u16 total length (incl NUL) + name + NUL."""
    raw = ("%s::%s*" % (proto, method)).encode("ascii")
    return struct.pack("<H", len(raw) + 1) + raw + b"\x00"


def s(text):
    """Quazal String: u16 len excluding NUL + bytes."""
    raw = text.encode("ascii")
    return struct.pack("<H", len(raw)) + raw


def w32(v):
    return struct.pack("<I", v & 0xFFFFFFFF)


def w64(v):
    return struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)


B_TRUE = b"\x01"
B_FALSE = b"\x00"


# --------------------------------------------------------------------------
# 1. method-name blocks
# --------------------------------------------------------------------------

def test_name_blocks():
    print("\n-- test_name_blocks")
    # hard-coded literals: 31-byte name + NUL => u16 32; 39-byte name + NUL => 40
    eq(voting.name_block(voting.VOTING_PROTO, "Vote_V1"),
       b"\x20\x00VotingServiceProtocol::Vote_V1*\x00",
       "literal name block Vote_V1")
    eq(voting.name_block(voting.WORLD_EVENT_PROTO, "GetStats_V1"),
       b"\x28\x00WorldEventServiceProtocol::GetStats_V1*\x00",
       "literal name block GetStats_V1")

    for method, fn in sorted(voting.METHOD_NAMES.items()):
        proto = voting.METHOD_PROTOCOLS[method]
        expected = ref_name_block(proto, method)
        payload = fn()
        check(isinstance(payload, bytes), "%s returns bytes" % method)
        check(payload.startswith(expected),
              "%s starts with its exact name block" % method)
        eq(struct.unpack("<H", payload[:2])[0], len(expected) - 2,
           "%s name length prefix includes the NUL" % method)
        check(b"*" in payload[:len(expected)],
              "%s name is '*' terminated" % method)

    # proto constants are the exact service names used by the client
    eq(voting.VOTING_PROTO, "VotingServiceProtocol", "voting proto name")
    eq(voting.WORLD_EVENT_PROTO, "WorldEventServiceProtocol",
       "world-event proto name")


# --------------------------------------------------------------------------
# 2. value-type encodings
# --------------------------------------------------------------------------

def test_value_types():
    print("\n-- test_value_types")

    # VoteEntry: String, u32, u32
    eq(voting.vote_entry({"voter": "bob", "a": 7, "b": 9}),
       b"\x03\x00bob" + w32(7) + w32(9), "vote_entry mapping")
    eq(voting.vote_entry(("bob", 7, 9)),
       b"\x03\x00bob" + w32(7) + w32(9), "vote_entry tuple")
    eq(voting.vote_entry({"voter": "bob", "choice": 7, "ts": 9}),
       b"\x03\x00bob" + w32(7) + w32(9), "vote_entry choice/ts aliases")
    eq(voting.vote_entry(), b"\x00\x00" + w32(0) + w32(0), "vote_entry default")

    # VotingInfo: u32, u32, u32, bool, u64, u32, u64, u64, u32, u32
    info = {"id": 1, "f2": 2, "f3": 3, "running": True,
            "start": 0x1122334455667788, "f6": 6,
            "votesA": 0x0000000100000002, "votesB": 0x0000000300000004,
            "f9": 9, "f10": 10}
    eq(voting.voting_info(info),
       w32(1) + w32(2) + w32(3) + B_TRUE + w64(0x1122334455667788) + w32(6)
       + w64(0x0000000100000002) + w64(0x0000000300000004) + w32(9) + w32(10),
       "voting_info mapping (10 fields, 40 bytes)")
    eq(len(voting.voting_info(info)), 4 + 4 + 4 + 1 + 8 + 4 + 8 + 8 + 4 + 4,
       "voting_info length")
    eq(voting.voting_info([1, 2, 3, False, 4, 5, 6, 7, 8, 9]),
       w32(1) + w32(2) + w32(3) + B_FALSE + w64(4) + w32(5)
       + w64(6) + w64(7) + w32(8) + w32(9), "voting_info sequence")
    eq(len(voting.voting_info()), 49, "voting_info default length")

    # VotingStats: u32 key, u32 f1, map<u32,u32>
    eq(voting.voting_stats(3, 4, {2: 5, 1: 6}),
       w32(3) + w32(4) + w32(2) + w32(1) + w32(6) + w32(2) + w32(5),
       "voting_stats positional (map sorted by key)")
    eq(voting.voting_stats({"id": 3, "f1": 4, "votes": {2: 5, 1: 6}}),
       w32(3) + w32(4) + w32(2) + w32(1) + w32(6) + w32(2) + w32(5),
       "voting_stats record")
    eq(voting.voting_stats(), w32(0) * 3, "voting_stats default (empty map)")

    # VoteMultiplier: 3 x u32
    eq(voting.vote_multiplier({"a": 1, "b": 2, "c": 3}),
       w32(1) + w32(2) + w32(3), "vote_multiplier mapping")
    eq(voting.vote_multiplier([7, 8, 9]), w32(7) + w32(8) + w32(9),
       "vote_multiplier sequence")
    eq(len(voting.vote_multiplier()), 12, "vote_multiplier is 0xc bytes")

    # WorldEvent: u32, u32, String, u64, bool, u32, u32, bool
    we = {"a": 1, "b": 2, "name": "X", "start": 7,
          "running": True, "f": 5, "g": 6, "visited": False}
    eq(voting.world_event(we),
       w32(1) + w32(2) + b"\x01\x00X" + w64(7) + B_TRUE + w32(5) + w32(6)
       + B_FALSE, "world_event mapping")
    eq(len(voting.world_event(we)), 4 + 4 + 3 + 8 + 1 + 4 + 4 + 1,
       "world_event length")
    eq(voting.world_event(), w32(0) + w32(0) + b"\x00\x00" + w64(0) + B_FALSE
       + w32(0) + w32(0) + B_FALSE, "world_event default")

    # WorldEventStatsEntry: String, 3 x u32
    eq(voting.we_stats_entry({"name": "e", "a": 1, "b": 2, "c": 3}),
       b"\x01\x00e" + w32(1) + w32(2) + w32(3), "we_stats_entry mapping")

    # WorldEventStats: list A, list B, 3 x u32
    eq(voting.we_stats([{"name": "e", "a": 1, "b": 2, "c": 3}], [], 7, 8, 9),
       w32(1) + b"\x01\x00e" + w32(1) + w32(2) + w32(3)
       + w32(0) + w32(7) + w32(8) + w32(9), "we_stats lists + 3 u32")
    eq(voting.we_stats({"listA": [], "listB": [], "c0": 1, "c1": 2, "c2": 3}),
       w32(0) + w32(0) + w32(1) + w32(2) + w32(3), "we_stats record")
    eq(len(voting.we_stats()), 20, "we_stats default length (5 u32)")

    # WorldEventCompletedMission: String, u32, String
    eq(voting.completed_mission({"a": "m", "id": 42, "b": "t"}),
       b"\x01\x00m" + w32(42) + b"\x01\x00t", "completed_mission mapping")
    eq(voting.completed_mission(), b"\x00\x00" + w32(0) + b"\x00\x00",
       "completed_mission default")

    # --- WorldEventTemplate (request side, writer 0x140e09bb0) ------------
    # WorldEventMissionTemplate: 2 x u32
    eq(voting.world_event_mission_template({"a": 1, "b": 2}),
       w32(1) + w32(2), "mission template mapping")
    eq(voting.world_event_mission_template((7, 8)), w32(7) + w32(8),
       "mission template sequence")
    eq(voting.world_event_mission_template(), w32(0) * 2,
       "mission template default is 8 bytes")

    # map<u32, mission> : count + ascending (key, a, b)
    eq(voting.map_u32_mission_template({7: (8, 9), 3: (4, 5)}),
       w32(2) + w32(3) + w32(4) + w32(5) + w32(7) + w32(8) + w32(9),
       "mission map sorted ascending, key + 2 u32")
    eq(voting.map_u32_mission_template({1: {"a": 2, "b": 3}}),
       w32(1) + w32(1) + w32(2) + w32(3), "mission map from record value")
    eq(voting.map_u32_mission_template(None), w32(0),
       "mission map empty => count 0")
    eq(voting.map_u32_mission_template({}), w32(0),
       "mission map {} => count 0")

    # vector<u32>
    eq(voting.u32_list([4, 5]), w32(2) + w32(4) + w32(5), "u32_list")
    eq(voting.u32_list(), w32(0), "u32_list default")
    eq(voting.u32_list("ab"), w32(0), "u32_list never iterates a string")

    # full record: u32, String, 12*u32, map, vector, map
    tmpl = {"id": 5, "name": "X", "u1": 1, "u2": 2,
            "missions": {7: (8, 9), 3: (4, 5)},
            "values": [10, 11], "scores": {2: 1}}
    expected_tmpl = (w32(5) + b"\x01\x00X" + w32(1) + w32(2) + w32(0) * 10
                     + w32(2) + w32(3) + w32(4) + w32(5) + w32(7) + w32(8)
                     + w32(9)
                     + w32(2) + w32(10) + w32(11)
                     + w32(1) + w32(2) + w32(1))
    eq(voting.world_event_template(tmpl), expected_tmpl,
       "world_event_template field order (writer 0x140e09bb0)")
    eq(len(voting.world_event_template(tmpl)),
       4 + 3 + 4 * voting.WE_TEMPLATE_U32_COUNT + (4 + 2 * 12) + (4 + 8)
       + (4 + 8), "world_event_template length")
    eq(voting.WE_TEMPLATE_U32_COUNT, 12, "12 u32 inside the template")

    # positional form: index 0 id, 1 name, 2..13 the twelve u32, then the three
    # containers -- identical bytes to the mapping form
    seq = [5, "X"] + [0] * 12
    seq[2], seq[3] = 1, 2
    seq += [{3: (4, 5), 7: (8, 9)}, [10, 11], {2: 1}]
    eq(voting.world_event_template(seq), expected_tmpl,
       "world_event_template positional form agrees")

    # default record: id, empty string, 12 zero u32, 3 empty containers
    eq(voting.world_event_template(),
       w32(0) + b"\x00\x00" + w32(0) * 12 + w32(0) * 3,
       "world_event_template default")
    eq(len(voting.world_event_template()), 4 + 2 + 48 + 12,
       "world_event_template default length = 66")


# --------------------------------------------------------------------------
# 3. containers and count prefixes
# --------------------------------------------------------------------------

def test_containers():
    print("\n-- test_containers")
    eq(voting.list_of(voting.vote_entry, []), w32(0), "empty list_of => 0 count")
    eq(voting.list_of(voting.vote_entry, None), w32(0), "None list_of => 0")
    eq(voting.list_of(voting.vote_entry, "abc"), w32(0),
       "string is not iterated into items")
    eq(voting.list_of(voting.vote_entry, {"voter": "a"}),
       w32(1) + voting.vote_entry({"voter": "a"}),
       "single mapping is treated as one item")
    eq(voting.list_of(voting.vote_entry, [{"voter": "a"}, {"voter": "b"}]),
       w32(2) + voting.vote_entry({"voter": "a"}) + voting.vote_entry(
           {"voter": "b"}), "list_of count prefix")
    eq(voting.list_of(None, [1, 2, 3]), w32(0), "non-callable fn => 0 count")
    eq(voting.list_of(lambda _x: "not bytes", [1]), w32(1),
       "non-bytes item chunk is dropped, count kept")

    eq(voting.map_u32_u32(), w32(0), "empty map => 0 count")
    eq(voting.map_u32_u32({}), w32(0), "empty dict => 0 count")
    eq(voting.map_u32_u32(None), w32(0), "None map => 0 count")
    eq(voting.map_u32_u32({9: 1, 2: 2, 5: 3}),
       w32(3) + w32(2) + w32(2) + w32(5) + w32(3) + w32(9) + w32(1),
       "map sorted ascending by key")
    eq(voting.map_u32_u32([(1, 2), (0, 3)]),
       w32(2) + w32(0) + w32(3) + w32(1) + w32(2), "map from pairs")

    # empty-container replies keep the count prefix and nothing else
    eq(voting.get_voting_infos(), ref_name_block(VOTING, "GetVotingInfos_V1")
       + w32(0), "get_voting_infos([]) count 0")
    eq(voting.get_last_voters([]), ref_name_block(VOTING, "GetLastVoters_V1")
       + w32(0), "get_last_voters([]) count 0")
    eq(voting.get_voting_stats([]), ref_name_block(VOTING, "GetVotingStats_V1")
       + w32(0), "get_voting_stats([]) count 0")
    eq(voting.get_own_completed_missions([]),
       ref_name_block(WE, "GetOwnCompletedMissions_V1") + w32(0),
       "get_own_completed_missions([]) count 0")
    eq(voting.get_last_completed_missions([]),
       ref_name_block(WE, "GetLastCompletedMissions_V1") + w32(0),
       "get_last_completed_missions([]) count 0")
    eq(voting.get_stats({}), ref_name_block(WE, "GetStats_V1") + w32(0) * 5,
       "get_stats({}) = two empty lists + three u32")


# --------------------------------------------------------------------------
# 4. per-method payloads
# --------------------------------------------------------------------------

def test_method_payloads():
    print("\n-- test_method_payloads")

    eq(voting.get_voting_infos([{"id": 5}]),
       ref_name_block(VOTING, "GetVotingInfos_V1") + w32(1)
       + voting.voting_info({"id": 5}), "get_voting_infos one entry")

    eq(voting.get_last_voters([{"voter": "a", "a": 1, "b": 2}]),
       ref_name_block(VOTING, "GetLastVoters_V1") + w32(1)
       + b"\x01\x00a" + w32(1) + w32(2), "get_last_voters entry shape")

    eq(voting.get_voting_stats([{"id": 3, "f1": 4, "votes": {1: 2}}]),
       ref_name_block(VOTING, "GetVotingStats_V1") + w32(1) + w32(3) + w32(4)
       + w32(1) + w32(1) + w32(2), "get_voting_stats entry shape")

    # GetVoteCount: bare map<u32,u32> (reader FUN_140de9460)
    eq(voting.get_vote_count({9: 1, 2: 3}),
       ref_name_block(VOTING, "GetVoteCount_V1") + w32(2) + w32(2) + w32(3)
       + w32(9) + w32(1), "get_vote_count = count + ascending pairs")
    eq(voting.get_vote_count(),
       ref_name_block(VOTING, "GetVoteCount_V1") + w32(0),
       "get_vote_count empty => single zero count (no u64)")
    eq(voting.get_vote_count([(5, 6), (5, 7)]),
       ref_name_block(VOTING, "GetVoteCount_V1") + w32(2) + w32(5) + w32(6)
       + w32(5) + w32(7), "get_vote_count from pairs")
    body = voting.get_vote_count({1: 2})[
        len(ref_name_block(VOTING, "GetVoteCount_V1")):]
    eq(len(body), 12, "get_vote_count body = 3 u32 for one entry")
    eq(struct.unpack("<I", body[:4])[0], 1, "get_vote_count starts with count")

    # void replies: name block only, no trailing u32
    for fn, proto, method in (
            (voting.vote, VOTING, "Vote_V1"),
            (voting.clear_voting_templates, VOTING, "ClearVotingTemplates_V1"),
            (voting.upload_voting_template, VOTING, "UploadVotingTemplate_V1"),
            (voting.upload_vote_multipliers, VOTING, "UploadVoteMultipliers_V1"),
            (voting.register, WE, "Register_V1"),
            (voting.unregister, WE, "Unregister_V1"),
            (voting.set_visited, WE, "SetVisited_V1"),
            (voting.clear_world_event_templates, WE,
             "ClearWorldEventTemplates_V1"),
            (voting.upload_world_event_template, WE,
             "UploadWorldEventTemplate_V1"),
            (voting.set_reputation_points, WE, "SetReputationPoints_V1")):
        eq(fn(), ref_name_block(proto, method), "%s has an empty body" % method)

    eq(voting.upload_world_event_template({"id": 1}),
       ref_name_block(WE, "UploadWorldEventTemplate_V1"),
       "upload_world_event_template is void, not an echo")
    eq(voting.upload_world_event_template(
        {"id": 1, "name": "T", "missions": {1: (2, 3)}, "values": [4]}),
       ref_name_block(WE, "UploadWorldEventTemplate_V1"),
       "upload_world_event_template ignores the whole record")

    eq(voting.vote(7, 1), ref_name_block(VOTING, "Vote_V1"),
       "vote ignores request fields (void reply)")
    eq(voting.upload_vote_multipliers([{"a": 1, "b": 2, "c": 3}]),
       ref_name_block(VOTING, "UploadVoteMultipliers_V1"),
       "upload_vote_multipliers is void, not an echo")

    # GetLatestWorldEvent: single record, NO count prefix
    ev = {"a": 1, "b": 2, "name": "E", "start": 3, "running": True,
          "f": 4, "g": 5, "visited": True}
    block = ref_name_block(WE, "GetLatestWorldEvent_V1")
    payload = voting.get_latest_world_event(ev)
    body = payload[len(block):]
    eq(payload, block + voting.world_event(ev),
       "get_latest_world_event = name + bare WorldEvent")
    eq(len(body), 4 + 4 + 3 + 8 + 1 + 4 + 4 + 1,
       "get_latest_world_event has no count prefix")
    eq(voting.get_latest_world_event({"name": "solo"}),
       ref_name_block(WE, "GetLatestWorldEvent_V1")
       + voting.world_event({"name": "solo"}),
       "get_latest_world_event accepts a single record")

    eq(voting.complete_mission(0), ref_name_block(WE, "CompleteMission_V1")
       + w32(0), "complete_mission u32 reply")
    eq(voting.complete_mission(77), ref_name_block(WE, "CompleteMission_V1")
       + w32(77), "complete_mission echoes its result")

    eq(voting.are_reputation_points_missing(True),
       ref_name_block(WE, "AreReputationPointsMissing_V1") + B_TRUE,
       "are_reputation_points_missing(True) => 0x01")
    eq(voting.are_reputation_points_missing(False),
       ref_name_block(WE, "AreReputationPointsMissing_V1") + B_FALSE,
       "are_reputation_points_missing(False) => 0x00")
    eq(voting.are_reputation_points_missing(),
       ref_name_block(WE, "AreReputationPointsMissing_V1") + B_FALSE,
       "are_reputation_points_missing default")


# --------------------------------------------------------------------------
# 5. totality on junk
# --------------------------------------------------------------------------

JUNK = [None, 0, -1, 2 ** 80, -(2 ** 80), "", "text", b"\xff\x00bytes",
        True, False, 3.5, float("nan"), [], (), {}, set(), object(),
        {"id": None}, {"name": None}, {"votes": "nope"}, {"listA": 3},
        {"a": {"nested": 1}}, [1, 2, 3], [None, "x", {}, []], ("a",),
        {1: "x", "y": 2}, {"a": [1, 2]}, "\u00dcn\u00efcode", [{"a": 1}],
        {"f1": object()}, object(), lambda: 1, range(3), {None: None}]

METHOD_ARGS = {
    "GetVotingInfos_V1": ("infos",),
    "Vote_V1": ("vote_id", "choice"),
    "GetVoteCount_V1": ("counts",),
    "GetLastVoters_V1": ("voters",),
    "GetVotingStats_V1": ("entries",),
    "ClearVotingTemplates_V1": (),
    "UploadVotingTemplate_V1": ("template_id", "level"),
    "UploadVoteMultipliers_V1": ("multipliers",),
    "Register_V1": (),
    "Unregister_V1": (),
    "GetLatestWorldEvent_V1": ("event",),
    "CompleteMission_V1": ("result",),
    "GetOwnCompletedMissions_V1": ("missions",),
    "GetLastCompletedMissions_V1": ("missions",),
    "SetVisited_V1": ("visited",),
    "ClearWorldEventTemplates_V1": (),
    "UploadWorldEventTemplate_V1": ("template",),
    "AreReputationPointsMissing_V1": ("missing",),
    "SetReputationPoints_V1": ("points", "what"),
    "GetStats_V1": ("stats",),
}

VALUE_FUNCS = ("vote_entry", "voting_info", "voting_stats", "vote_multiplier",
               "world_event", "we_stats_entry", "we_stats",
               "completed_mission", "world_event_mission_template",
               "map_u32_mission_template", "u32_list",
               "world_event_template")


def test_totality():
    print("\n-- test_totality")
    for name in VALUE_FUNCS:
        fn = getattr(voting, name)
        for junk in JUNK:
            try:
                out = fn(junk)
            except Exception as exc:  # noqa: BLE001 - the point of the test
                check(False, "%s(%r) raised %r" % (name, junk, exc))
                continue
            check(isinstance(out, bytes), "%s(%r) -> bytes" % (name, junk))

    # binary search just for the exposed helpers
    for junk in JUNK:
        for label, call in (
                ("list_of", lambda j: voting.list_of(voting.vote_entry, j)),
                ("list_of_badfn", lambda j: voting.list_of(j, [1])),
                ("map_u32_u32", voting.map_u32_u32)):
            try:
                out = call(junk)
            except Exception as exc:  # noqa: BLE001
                check(False, "%s(%r) raised %r" % (label, junk, exc))
                continue
            check(isinstance(out, bytes), "%s(%r) -> bytes" % (label, junk))

    for method, argnames in sorted(METHOD_ARGS.items()):
        fn = voting.METHOD_NAMES[method]
        proto = voting.METHOD_PROTOCOLS[method]
        block = ref_name_block(proto, method)
        for junk in JUNK:
            for mode in ("positional", "kw", "build_kw"):
                try:
                    if mode == "positional":
                        out = fn(*([junk] * len(argnames)))
                    elif mode == "kw":
                        out = fn(**{a: junk for a in argnames})
                    else:
                        out = voting.build(proto, method,
                                           **{a: junk for a in argnames})
                except Exception as exc:  # noqa: BLE001
                    check(False, "%s %s junk=%r raised %r"
                          % (method, mode, junk, exc))
                    continue
                check(isinstance(out, bytes),
                      "%s %s junk=%r -> bytes" % (method, mode, junk))
                check(out.startswith(block),
                      "%s %s junk=%r keeps the name block"
                      % (method, mode, junk))

    # junk must never leak a traceback through build either
    for junk in JUNK:
        check(voting.build(junk, junk, junk=junk) is None,
              "build(garbage proto/method) -> None")


# --------------------------------------------------------------------------
# 6. purity
# --------------------------------------------------------------------------

def test_purity():
    print("\n-- test_purity")
    ev = {"a": 1, "b": 2, "name": "E", "start": 3, "running": True,
          "f": 4, "g": 5, "visited": False}
    infos = [{"id": 1, "votesA": 2}, {"id": 2, "votes": {}, "f9": 9}]
    stats = {"listA": [{"name": "a", "a": 1}], "listB": [], "c0": 2}
    snapshot = copy.deepcopy((ev, infos, stats))

    first = (voting.get_latest_world_event(ev),
             voting.get_voting_infos(infos),
             voting.get_voting_stats(infos),
             voting.get_stats(stats))
    second = (voting.get_latest_world_event(ev),
              voting.get_voting_infos(infos),
              voting.get_voting_stats(infos),
              voting.get_stats(stats))
    eq(first, second, "builders are deterministic")
    eq((ev, infos, stats), snapshot, "builders do not mutate their inputs")

    # unicode degrades to ASCII instead of raising (Quazal strings are ascii)
    out = voting.vote_entry({"voter": "\u00dcn\u00ef"})
    check(isinstance(out, bytes) and out.isascii(),
          "non-ascii voter is transliterated to ascii")


# --------------------------------------------------------------------------
# 7. coverage of the 20 documented methods
# --------------------------------------------------------------------------

DOCUMENTED_VOTING = [
    "GetVotingInfos_V1", "Vote_V1", "GetVoteCount_V1", "GetLastVoters_V1",
    "GetVotingStats_V1", "ClearVotingTemplates_V1", "UploadVotingTemplate_V1",
    "UploadVoteMultipliers_V1",
]
DOCUMENTED_WORLD_EVENT = [
    "Register_V1", "Unregister_V1", "GetLatestWorldEvent_V1",
    "CompleteMission_V1", "GetOwnCompletedMissions_V1",
    "GetLastCompletedMissions_V1", "SetVisited_V1",
    "ClearWorldEventTemplates_V1", "UploadWorldEventTemplate_V1",
    "AreReputationPointsMissing_V1", "SetReputationPoints_V1", "GetStats_V1",
]


def test_coverage():
    print("\n-- test_coverage")
    documented = DOCUMENTED_VOTING + DOCUMENTED_WORLD_EVENT
    eq(len(documented), 20, "report lists 20 methods")
    eq(len(DOCUMENTED_VOTING), 8, "report lists 8 voting methods")
    eq(len(DOCUMENTED_WORLD_EVENT), 12, "report lists 12 world-event methods")

    known = set(voting.METHOD_NAMES)
    unknown = set(voting.UNKNOWN_METHODS)
    eq(sorted(known | unknown), sorted(documented),
       "METHOD_NAMES + UNKNOWN_METHODS == the 20 documented methods")
    eq(known & unknown, set(), "implemented and unknown sets are disjoint")
    eq(set(voting.METHOD_PROTOCOLS), known | unknown,
       "METHOD_PROTOCOLS covers every method exactly once")
    eq(len(known), 20, "all 20 implemented methods")
    eq(len(unknown), 0, "nothing left unknown")
    eq(sorted(voting.UNKNOWN_METHODS), [], "UNKNOWN_METHODS is empty")

    # the two former unknowns are real builders now
    for method in ("GetVoteCount_V1", "UploadWorldEventTemplate_V1"):
        check(method in known, "%s is implemented" % method)
        out = voting.build(voting.METHOD_PROTOCOLS[method], method)
        check(isinstance(out, bytes) and out.startswith(
            ref_name_block(voting.METHOD_PROTOCOLS[method], method)),
            "%s builds a payload via build()" % method)

    # and every documented method builds through the public entry point
    for method in documented:
        out = voting.build(voting.METHOD_PROTOCOLS[method], method)
        check(isinstance(out, bytes),
              "build() implements %s" % method)

    for method in DOCUMENTED_VOTING:
        check(voting.METHOD_PROTOCOLS[method] == VOTING,
              "%s belongs to VotingServiceProtocol" % method)
    for method in DOCUMENTED_WORLD_EVENT:
        check(voting.METHOD_PROTOCOLS[method] == WE,
              "%s belongs to WorldEventServiceProtocol" % method)

    # build() dispatch
    eq(voting.build(VOTING, "GetVotingInfos_V1", infos=[{"id": 1}]),
       voting.get_voting_infos([{"id": 1}]), "build dispatch (voting)")
    eq(voting.build(WE, "GetStats_V1", stats={"c0": 1}),
       voting.get_stats({"c0": 1}), "build dispatch (world event)")
    eq(voting.build(WE, "GetOwnCompletedMissions_V1"),
       voting.get_own_completed_missions(), "build default-arg dispatch")
    check(voting.build(VOTING, "GetOwnCompletedMissions_V1") is None,
          "build refuses a mismatched protocol")
    check(voting.build(VOTING, "Nope_V1") is None,
          "build returns None for an undocumented method")
    check(isinstance(voting.build(WE, "GetStats_V1", bogus=1), bytes),
          "build tolerates unexpected kwargs")


# --------------------------------------------------------------------------
# 8. module hygiene
# --------------------------------------------------------------------------

def test_hygiene():
    print("\n-- test_hygiene")
    path = os.path.join(ROOT, "server", "rv_shapes", "voting.py")
    with open(path, "r", encoding="utf-8") as handle:
        src = handle.read()

    imports = [ln.strip() for ln in src.splitlines()
               if ln.strip().startswith(("import ", "from "))]
    check(not any("mimic_server" in ln for ln in imports),
          "no mimic_server import (%r)" % (imports,))
    check(all(ln.startswith(("from collections.abc", "from .", "from rv_shapes"))
              for ln in imports), "only rv_shapes/collections imports: %r"
          % (imports,))
    for banned in ("import os", "import socket", "import json", "open("):
        check(banned not in src, "no %r in voting.py" % banned)
    for helper in ("name_block", "qstr", "u8", "u32", "u64"):
        check(helper in src, "uses rv_shapes.%s" % helper)
    check("global " not in src, "no global statement")
    for name in VALUE_FUNCS:
        check(callable(getattr(voting, name, None)),
              "value type %s is exported" % name)


def main():
    for test in (test_name_blocks, test_value_types, test_containers,
                 test_method_payloads, test_totality, test_purity,
                 test_coverage, test_hygiene):
        test()

    print("\n%d checks, %d failing" % (CHECKS, len(FAILURES)))
    print("implemented: %d   unknown: %d   total: %d"
          % (len(voting.METHOD_NAMES), len(voting.UNKNOWN_METHODS),
             len(voting.METHOD_NAMES) + len(voting.UNKNOWN_METHODS)))
    if FAILURES:
        print("FAILED")
        return 1
    print("ALL VOTING / WORLD-EVENT SHAPES OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
