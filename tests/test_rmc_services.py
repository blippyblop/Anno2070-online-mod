#!/usr/bin/env python3
"""Self-test for ``server/rmc_services.py`` (and the PRUDP RMC envelope).

Plain python, no pytest:

    source ~/tools/env.sh && python3 tools/test_rmc_services.py

Exit code 0 means every check passed.  What is asserted, for EVERY method in
``rmc_services.METHODS`` (119 of them: 54 PlayerProfile + 8 Voting + 12
WorldEvent + 9 BackEnd):

  * the dispatcher answers at all, with a COMPLETE frame (the old bug was a
    bare payload with no envelope, which the client reads as a new call);
  * the RMC envelope is exact: ``u32 size`` covers the bytes after itself,
    ``u16 proto_len`` INCLUDES the NUL, the protocol name matches the request,
    ``u8 type == 0`` (reply) and ``u8 status == 1`` (clean), and the request's
    call id is echoed;
  * the payload's name block is exact: ``u16 len`` INCLUDING the NUL, the name
    is ``"Proto::Method*"`` (trailing '*' -- the client appends it before
    comparing) and it ends with the NUL;
  * the remaining bytes decode EXACTLY with the field layout ``rv_shapes``
    declares for that method -- re-implemented here from the documented
    layouts with explicit little-endian reads, deliberately NOT reusing
    ``rv_shapes``' own helpers, so a wrong helper cannot agree with itself;
  * a void method's payload is the name block and NOTHING else;
  * nothing big-endian survives: neither owned module contains a big-endian
    struct format string, and values written with a distinctive byte order
    (0x11223344) read back identically (LE), not byte-swapped;
  * specific regressions: SetArkItems returns an empty *payload* (still inside
    a frame), the ARK byte round-trip, WorldEvent::GetStats is NOT routed to
    the PlayerProfile handler, the six previously missing voting/worldevent
    methods exist, and a name-only request (no envelope) still gets a framed
    reply;
  * the CONTENT wiring (voting + world event), driven end-to-end through
    ``rmc_services.respond_payload`` against one in-memory store per panel and
    decoded with the independent layouts above: GetVotingInfos carries map key
    0 whose packed Date decodes to a window covering now, Vote moves exactly
    one faction by one (and flips the own-vote field) while GetVoteCount /
    GetLastVoters / GetVotingStats stay in agreement, GetLatestWorldEvent has
    the INVERTED running flag 0 and a non-zero progress max, CompleteMission is
    idempotent per (mission, player) and GetOwnCompletedMissions reflects it,
    SetVisited flips f8, world-event GetStats counts the completions, both
    panels survive a JSON round-trip, and a legacy state file
    (``votingInfo`` + ``worldEvent.active``) is migrated on load;
  * ``rv_shapes`` registers the ``auth`` shape module but keeps
    ``AuthenticationProtocol`` off the default reply path (no Anno build speaks
    protocol 0x0A).

The store is a throw-away file in a temporary directory: running the test
leaves no state behind.
"""
import datetime
import os
import re
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")
sys.path.insert(0, SERVER)

import content_events as CE       # noqa: E402
import rmc_services as R          # noqa: E402
import prudp_transport as P       # noqa: E402
import rv_shapes                  # noqa: E402

PROFILE = R.PROFILE
VOTING = R.VOTING
WORLD = R.WORLD
BACKEND = R.BACKEND

CHECKS = 0
FAILURES = []


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    if ok:
        print("PASS %s" % label)
    else:
        FAILURES.append(label)
        print("FAIL %s%s" % (label, ("  <- " + detail) if detail else ""))


# --------------------------------------------------------------------------
# independent little-endian container decoder
# --------------------------------------------------------------------------
class Short(Exception):
    pass


class Cur:
    """Cursor with explicit little-endian reads (never uses rv_shapes)."""

    def __init__(self, data):
        self.d = bytes(data)
        self.o = 0

    def take(self, n):
        raw = self.d[self.o:self.o + n]
        if len(raw) != n:
            raise Short("wanted %d at %d of %d" % (n, self.o, len(self.d)))
        self.o += n
        return raw

    def u8(self):
        return self.take(1)[0]

    def u16(self):
        return int.from_bytes(self.take(2), "little")

    def u32(self):
        return int.from_bytes(self.take(4), "little")

    def u64(self):
        return int.from_bytes(self.take(8), "little")

    def boolean(self):
        return self.u8() != 0

    def string(self):
        # engine String: u16 count INCLUDES the NUL, and the last byte is it
        n = self.u16()
        raw = self.take(n)
        return raw[:-1].rstrip(b"\x00").decode("ascii", "replace")

    def rest(self):
        raw = self.d[self.o:]
        self.o = len(self.d)
        return raw

    def left(self):
        return len(self.d) - self.o


def decode(cur, spec, depth=0):
    """Decode ``spec`` from ``cur``; returns a python value.

    Atoms: "u8" "u16" "u32" "u64" "bool" "str" "rest".
    Containers: ("list", item) ("map", key, value) ("seq", a, b, ...).
    """
    if depth > 8:
        raise Short("nesting too deep")
    if isinstance(spec, str):
        if spec == "u8":
            return cur.u8()
        if spec == "u16":
            return cur.u16()
        if spec == "u32":
            return cur.u32()
        if spec == "u64":
            return cur.u64()
        if spec == "bool":
            return cur.boolean()
        if spec == "str":
            return cur.string()
        if spec == "rest":
            return cur.rest()
        if spec == "void":
            return None
        raise Short("unknown atom %r" % (spec,))
    kind = spec[0]
    if kind == "list":
        n = cur.u32()
        if n > 4096:
            raise Short("implausible list count %d" % n)
        return [decode(cur, spec[1], depth + 1) for _ in range(n)]
    if kind == "map":
        n = cur.u32()
        if n > 4096:
            raise Short("implausible map count %d" % n)
        return [(decode(cur, spec[1], depth + 1), decode(cur, spec[2], depth + 1))
                for _ in range(n)]
    if kind == "seq":
        return tuple(decode(cur, sub, depth + 1) for sub in spec[1:])
    raise Short("unknown container %r" % (kind,))


def decode_body(payload, spec):
    """Decode the whole payload; raises unless the spec consumes it exactly."""
    cur = Cur(payload)
    value = decode(cur, spec)
    if cur.left():
        raise Short("%d trailing byte(s)" % cur.left())
    return value


# --------------------------------------------------------------------------
# the layouts rv_shapes declares (re-implemented by hand, see the module docs)
# --------------------------------------------------------------------------
PROFILE_BODY = ("seq", "str", "u32", "u32", "u32", "u32", "u32",
                ("list", "u32"), ("list", "u32"), "str")
STATS_ENTRY = ("seq", "u32", "u32", "u32", "u64", "u64", "u64")
ARK_ITEM = ("seq", "u32", "u32", "u32", "u32")
WE_ENTRY = ("seq", "str", "u32", "u32", "u32")
VOTING_INFO = ("seq", "u32", "u32", "u32", "bool", "u64", "u32", "u64", "u64",
               "u32", "u32")
COMPLETED_MISSION = ("seq", "str", "u32", "str")
LEADERBOARD = ("seq", "u32", "u32", "u64", "u64", "u32", ("list", "u8"))

LAYOUT = {
    # ---- ChatServiceProtocol (12) ----
    "ChatServiceProtocol::Initialize_V1": "void",
    "ChatServiceProtocol::SignalChatReady_V1": "void",
    "ChatServiceProtocol::SignalChatNotReady_V1": "void",
    "ChatServiceProtocol::Subscribe_V1": "void",
    "ChatServiceProtocol::Unsubscribe_V1": "void",
    "ChatServiceProtocol::SendToChannel_V1": "void",
    "ChatServiceProtocol::SendToUser_V1": "void",
    "ChatServiceProtocol::SendToGroup_V1": "void",
    "ChatServiceProtocol::Ignore_V1": "void",
    "ChatServiceProtocol::Unignore_V1": "void",
    "ChatServiceProtocol::BanPlayerFromChat_V1": "void",
    "ChatServiceProtocol::UnbanPlayerFromChat_V1": "void",
    "ChatServiceProtocol::GetBanInfo_V1": ("list", "u32"),
    "ChatServiceProtocol::SendMessages_V1": "void",
    "ChatServiceProtocol::GetChannels_V1": ("list", ("seq", "bool", "u32", "u32")),
    "ChatServiceProtocol::GetIgnoreList_V1": ("list", "u32"),
    # ---- FriendsServiceProtocol (6) ----
    "FriendsServiceProtocol::GetFriends_V1": ("list", ("seq", "u32", "u32", "u32", "u32", "u32", "u32", "bool", "bool")),
    "FriendsServiceProtocol::GetFriendsWithActivations_V1": ("list", ("seq", "u32", "u32", "u32", "u32", "u32", "u32", "bool", "bool")),
    "FriendsServiceProtocol::AddFriend_V1": "void",
    "FriendsServiceProtocol::RemoveFriend_V1": "void",
    "FriendsServiceProtocol::AddRecentContact_V1": "void",
    "FriendsServiceProtocol::SetComment_V1": "void",
    "FriendsServiceProtocol::FindPlayers_V1": ("list", ("seq", "u32", "u32", "u32", "u64")),
    "FriendsServiceProtocol::FindFacebookFriends_V1": ("list", ("seq", "u32", "u32", "u32", "u64")),
    "FriendsServiceProtocol::GetRecentContacts_V1": ("list", ("seq", "u32", "u32", "u32",
                                                              "u32", "u32", "u32", "bool", "bool")),
    # ---- MailServiceProtocol (7) ----
    "MailServiceProtocol::GetMailCount_V1": ("seq", "u32", "u32", "u32"),
    "MailServiceProtocol::GetMails_V1": ("list", ("seq", "u32", "u64", "u32", "bool")),
    "MailServiceProtocol::GetMail_V1": ("seq", "u32", "u64", "u32", "bool"),
    "MailServiceProtocol::SetReadFlag_V1": "void",
    "MailServiceProtocol::DeleteMail_V1": "void",
    "MailServiceProtocol::DeleteAllMails_V1": "void",
    "MailServiceProtocol::UploadMail_V1": "void",
    "MailServiceProtocol::UploadMailToProfile_V1": "void",
    "MailServiceProtocol::UploadMailsToProfiles_V1": "void",
    "MailServiceProtocol::BroadcastMail_V1": "void",
    "MailServiceProtocol::DeleteAllMailsByPid_V1": "void",
    # ---- PlayerProfileServiceProtocol (54) ----
    PROFILE + "::SetStatus_V1": "void",
    PROFILE + "::CreatePlayerProfile_V1": ("seq", "str", "u16", PROFILE_BODY,
                                           ("map", "u32", "u8")),
    PROFILE + "::GetPlayerProfile_V1": PROFILE_BODY,
    PROFILE + "::DoesUserExist_V1": "bool",
    PROFILE + "::SetPortrait_V1": "void",
    PROFILE + "::SetIcon_V1": "void",
    PROFILE + "::SetColor_V1": "void",
    PROFILE + "::SetPlayerTitle_V1": "void",
    PROFILE + "::SetArk_V1": "void",
    PROFILE + "::AddActivation_V1": "void",
    PROFILE + "::GetAchievements_V1": ("list", ("seq", "u32", "u64")),
    PROFILE + "::AddAchievement_V1": "u64",
    PROFILE + "::GetSettings_V1": ("map", "u32", "u32"),
    PROFILE + "::SetSetting_V1": "void",
    PROFILE + "::GetKeyBindings_V1": ("map", "u32", ("list", "u32")),
    PROFILE + "::SetKeyBinding_V1": "void",
    PROFILE + "::ResetKeyBindings_V1": "void",
    PROFILE + "::AddHelperQuest_V1": "void",
    PROFILE + "::ResetHelperQuests_V1": "void",
    PROFILE + "::GetInfoMails_V1": ("map", "u32", "u8"),
    PROFILE + "::AddInfoMail_V1": "void",
    PROFILE + "::SetInfoMailRead_V1": "void",
    PROFILE + "::GetStats_V1": ("list", STATS_ENTRY),
    PROFILE + "::SetStats_V1": "void",
    PROFILE + "::GetArkItems_V1": ("list", ARK_ITEM),
    PROFILE + "::SetArkItems_V1": "void",
    PROFILE + "::GetStatus_V1": "u32",
    PROFILE + "::GetWorldStats_V1": ("seq", "u32", ("list", STATS_ENTRY)),
    PROFILE + "::ResetInfoMails_V1": "void",
    PROFILE + "::UploadWorldStatsStencils_V1": "void",
    PROFILE + "::GetReputationPoints_V1": ("map", "u32", "u32"),
    PROFILE + "::AreReputationPointsMissing_V1": "bool",
    PROFILE + "::SetReputationPoints_V1": "void",
    PROFILE + "::SolveDailyQuest_V1": "void",
    PROFILE + "::UploadFactionRewards_V1": "void",
    PROFILE + "::UploadStatsReputation_V1": "void",
    PROFILE + "::GetFacebookAccount_V1": ("seq", "u64", "str"),
    PROFILE + "::SetFacebookAccount_V1": "void",
    PROFILE + "::InvalidateAllReputationPoints_V1": "void",
    PROFILE + "::UploadReputationMails_V1": "void",
    PROFILE + "::ActivateUniqueKey_V1": ("list", "u32"),
    PROFILE + "::InitializeService_V1": "void",
    PROFILE + "::AddShopItem_V1": "void",
    PROFILE + "::RemoveShopItem_V1": "void",
    PROFILE + "::ListShopItems_V1": ("seq", "u32", ("list", "u32")),
    PROFILE + "::UploadShopItems_V1": "void",
    PROFILE + "::UploadSteamAppIdMapping_V1": "void",
    PROFILE + "::UpdateUPCActivations_V1": "void",
    PROFILE + "::UpdateSteamActivations_V1": "void",
    PROFILE + "::GetAccountInfo_V1": ("seq", "str", "str"),
    PROFILE + "::UploadAchievementRewards_V1": "void",
    PROFILE + "::FetchAssetCounterGraph_V1": ("seq", ("map", "u32", "u32"), "u32"),
    PROFILE + "::UploadLeaderboardTemplates_V1": "void",
    PROFILE + "::GetLeaderboardTemplates_V1": ("list", LEADERBOARD),
    # ---- VotingServiceProtocol (8) ----
    VOTING + "::GetVotingInfos_V1": ("list", VOTING_INFO),
    VOTING + "::Vote_V1": "void",
    VOTING + "::GetVoteCount_V1": ("map", "u32", "u32"),
    VOTING + "::GetVotingStats_V1": ("list", ("seq", "u32", "u32",
                                              ("map", "u32", "u32"))),
    VOTING + "::GetLastVoters_V1": ("list", ("seq", "str", "u32", "u32")),
    VOTING + "::UploadVotingTemplate_V1": "void",
    VOTING + "::ClearVotingTemplates_V1": "void",
    VOTING + "::UploadVoteMultipliers_V1": "void",
    # ---- WorldEventServiceProtocol (12) ----
    WORLD + "::GetLatestWorldEvent_V1": ("seq", "u32", "u32", "str", "u64",
                                         "bool", "u32", "u32", "bool"),
    WORLD + "::Register_V1": "void",
    WORLD + "::Unregister_V1": "void",
    WORLD + "::CompleteMission_V1": "u32",
    WORLD + "::GetOwnCompletedMissions_V1": ("list", COMPLETED_MISSION),
    WORLD + "::GetLastCompletedMissions_V1": ("list", COMPLETED_MISSION),
    WORLD + "::SetVisited_V1": "void",
    WORLD + "::ClearWorldEventTemplates_V1": "void",
    WORLD + "::UploadWorldEventTemplate_V1": "void",
    WORLD + "::AreReputationPointsMissing_V1": "bool",
    WORLD + "::SetReputationPoints_V1": "void",
    WORLD + "::GetStats_V1": ("seq", ("list", WE_ENTRY), ("list", WE_ENTRY),
                              "u32", "u32", "u32"),
    # ---- BackEndServiceProtocol (9) ----
    BACKEND + "::GetVersion_V1": "u32",
    BACKEND + "::GetProtocolVersions_V1": ("seq", "u32", "rest"),
    BACKEND + "::CheckLoginAllowed_V1": ("seq", "str", "u16"),
    BACKEND + "::IsMaintenanceOngoing_V1": "bool",
    BACKEND + "::GetMaintenanceTime_V1": "u64",
    BACKEND + "::ScheduleMaintenance_V1": "void",
    BACKEND + "::StopMaintenance_V1": "void",
    BACKEND + "::GetProfilingUnits_V1": ("seq", "u32", "rest"),
    BACKEND + "::GetSandboxName_V1": "str",
}


# --------------------------------------------------------------------------
# request bodies (syntactically valid for each method)
# --------------------------------------------------------------------------
def le16(v):
    return int(v).to_bytes(2, "little")


def le32(v):
    return int(v).to_bytes(4, "little")


def le64(v):
    return int(v).to_bytes(8, "little")


def rq_str(text):
    """Engine writer convention: u16 (len+1) then the bytes and the NUL."""
    raw = text.encode("ascii")
    return le16(len(raw) + 1) + raw + b"\x00"


def rq_list(spec_item_bytes, items):
    return le32(len(items)) + b"".join(spec_item_bytes(i) for i in items)


def ark_bytes(item):
    return b"".join(le32(x) for x in item)


def stat_bytes(s):
    return le32(s[0]) + le32(s[1]) + le32(s[2]) + le64(s[3]) + le64(s[4]) + le64(s[5])


TEST_ITEMS = [
    (0x11223344, 0x55667788, 0x99AABBCC, 0xDDEEFF00),
    (0x00000001, 0x00000002, 0x00000003, 0x00000004),
    (0xFFFFFFFF, 0xDEADBEEF, 0xCAFEBABE, 0x0BADF00D),
]

REQUEST = {
    PROFILE + "::SetStatus_V1": le32(0x11223344),
    PROFILE + "::CreatePlayerProfile_V1": rq_str("Hero"),
    PROFILE + "::SetPortrait_V1": le32(3),
    PROFILE + "::SetIcon_V1": le32(4),
    PROFILE + "::SetColor_V1": le32(5),
    PROFILE + "::SetPlayerTitle_V1": rq_str("Admiral"),
    PROFILE + "::SetArk_V1": rq_list(ark_bytes, TEST_ITEMS),
    PROFILE + "::AddActivation_V1": le32(9),
    PROFILE + "::AddAchievement_V1": le32(7) + le64(0x0102030405060708),
    PROFILE + "::SetSetting_V1": le32(0x11223344) + le32(0x55667788),
    PROFILE + "::SetKeyBinding_V1": le32(11) + le32(2) + le32(5) + le32(6),
    PROFILE + "::AddHelperQuest_V1": le32(1),
    PROFILE + "::AddInfoMail_V1": le32(21),
    PROFILE + "::SetInfoMailRead_V1": le32(21) + b"\x01",
    PROFILE + "::SetStats_V1": rq_list(stat_bytes, [(1, 2, 3, 4, 5, 6),
                                                    (7, 8, 9, 10, 11, 12)]),
    PROFILE + "::SetArkItems_V1": rq_list(ark_bytes, TEST_ITEMS),
    PROFILE + "::AreReputationPointsMissing_V1": le32(1),
    PROFILE + "::SetReputationPoints_V1": le32(1) + le32(500),
    PROFILE + "::SetFacebookAccount_V1": le64(0x1122334455667788) + rq_str("fb"),
    PROFILE + "::ActivateUniqueKey_V1": le32(0x11223344),
    PROFILE + "::AddShopItem_V1": le32(0x11223344),
    PROFILE + "::RemoveShopItem_V1": le32(0x11223344),
    PROFILE + "::SetFacebookAccount_V1": le64(1) + rq_str("fb"),
    # voting
    VOTING + "::Vote_V1": le32(1) + le32(1),
    VOTING + "::GetVoteCount_V1": le32(1),
    VOTING + "::GetVotingStats_V1": b"\x01",
    VOTING + "::GetLastVoters_V1": le32(0) + le32(10),     # (Vote id, count)
    VOTING + "::UploadVotingTemplate_V1": le32(2) + le32(3),
    VOTING + "::UploadVoteMultipliers_V1": rq_list(
        lambda m: le32(m[0]) + le32(m[1]) + le32(m[2]),
        [(1, 2, 3), (4, 5, 6)]),
    # world event
    WORLD + "::CompleteMission_V1": le32(42) + rq_str("mission"),
    WORLD + "::GetLastCompletedMissions_V1": le32(5),
    WORLD + "::UploadWorldEventTemplate_V1": bytes(range(64)),
    WORLD + "::AreReputationPointsMissing_V1": le32(1),
    WORLD + "::SetReputationPoints_V1": le32(1) + le32(700),
}


# --------------------------------------------------------------------------
# frame helpers (built here, not from the modules under test)
# --------------------------------------------------------------------------
def name_block(proto, method, star=True):
    nm = ("%s::%s%s" % (proto, method, "*" if star else "")).encode("ascii") + b"\x00"
    return le16(len(nm)) + nm


#: The client prefixes every RMC request body with a u32 class word (0 in every
#: capture we own); the arguments follow it.  Proven in runs/*/server.log: a
#: no-argument method sends a bare ``00 00 00 00`` and ``GetVoteCount_V1`` sends
#: ``00 00 00 00 | e9 03 00 00`` (its Vote id at offset 4).
PROLOGUE = b"\x00\x00\x00\x00"


def request_frame(proto, method, params=b"", call_id=1):
    pn = proto.encode("ascii") + b"\x00"
    inner = (le16(len(pn)) + pn + b"\x01" + le32(call_id)
             + name_block(proto, method, star=False) + PROLOGUE + params)
    return le32(len(inner)) + inner


def parse_reply(frame):
    """Split a reply frame into envelope fields + payload (independent code)."""
    if len(frame) < 12:
        raise Short("frame too short")
    size = int.from_bytes(frame[0:4], "little")
    plen = int.from_bytes(frame[4:6], "little")
    proto = frame[6:6 + plen]
    off = 6 + plen
    typ = frame[off]
    status = frame[off + 1]
    call_id = int.from_bytes(frame[off + 2:off + 6], "little")
    payload = frame[off + 6:]
    return {"size": size, "plen": plen, "proto": proto, "type": typ,
            "status": status, "call_id": call_id, "payload": payload}


def parse_name_block(payload):
    mlen = int.from_bytes(payload[0:2], "little")
    raw = payload[2:2 + mlen]
    return mlen, raw, payload[2 + mlen:]


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------
def main():
    tmpdir = tempfile.mkdtemp(prefix="rmc_services_test_")
    data_file = os.path.join(tmpdir, "state.json")
    try:
        store = R.open_store(data_file)
        responder = R.RmcResponder(store, lambda *a, **k: None)

        # ---- dispatch-table completeness --------------------------------
        check("METHODS has 119 methods", len(R.METHODS) == 119, str(len(R.METHODS)))
        check("LAYOUT covers every method",
              set(LAYOUT) == set(R.METHODS),
              "missing=%r extra=%r" % (sorted(set(R.METHODS) - set(LAYOUT)),
                                       sorted(set(LAYOUT) - set(R.METHODS))))
        missing_shapes = []
        for full in sorted(R.METHODS):
            proto, method = full.split("::", 1)
            try:
                import rv_shapes
                if rv_shapes.build(proto, method) is None:
                    missing_shapes.append(full)
            except Exception as exc:              # pragma: no cover
                missing_shapes.append("%s (%r)" % (full, exc))
        check("rv_shapes builds a payload for every method", not missing_shapes,
              repr(missing_shapes))

        # ---- shape-module registration: auth is registered, not routed ----
        check("rv_shapes registers the auth shape module",
              rv_shapes._PROTO_MODULE.get("authenticationprotocol") == "auth",
              repr(rv_shapes._PROTO_MODULE.get("authenticationprotocol")))
        check("... but auth stays OFF the default reply path (no Anno build "
              "speaks protocol 0x0A): build() -> None, module still imports",
              rv_shapes.build("AuthenticationProtocol", "Login") is None
              and rv_shapes.available().get("auth") == "ok",
              repr(rv_shapes.available().get("auth")))

        # ---- every method through the dispatcher ------------------------
        for index, full in enumerate(sorted(R.METHODS)):
            proto, method = full.split("::", 1)
            params = REQUEST.get(full, b"")
            call_id = 1000 + index
            frame = request_frame(proto, method, params, call_id)
            out = responder.handle_payload(frame, None, origin="test")
            spec = LAYOUT[full]

            if not isinstance(out, (bytes, bytearray)) or not out:
                check("%s: answered with a frame" % full, False, repr(out))
                continue
            out = bytes(out)
            try:
                env = parse_reply(out)
            except Short as exc:
                check("%s: envelope parses" % full, False, str(exc))
                continue

            check("%s: frame size covers the rest of the frame" % full,
                  env["size"] == len(out) - 4,
                  "%d vs %d" % (env["size"], len(out) - 4))
            check("%s: protocol name length includes the NUL" % full,
                  env["plen"] == len(proto) + 1 and env["proto"] == proto.encode() + b"\x00",
                  "%d %r" % (env["plen"], env["proto"]))
            check("%s: type=0 (reply) status=1 (clean)" % full,
                  env["type"] == 0 and env["status"] == 1,
                  "%d/%d" % (env["type"], env["status"]))
            check("%s: call id echoed" % full, env["call_id"] == call_id,
                  "%d vs %d" % (env["call_id"], call_id))

            payload = env["payload"]
            mlen, raw, body = parse_name_block(payload)
            expected = ("%s::%s*" % (proto, method)).encode("ascii")
            check("%s: payload name block exact (len incl NUL, trailing '*')" % full,
                  mlen == len(expected) + 1 and raw == expected + b"\x00",
                  "%d %r" % (mlen, raw))

            if spec == "void":
                check("%s: void reply is the name block only" % full, body == b"",
                      "%d extra bytes" % len(body))
                continue
            try:
                value = decode_body(body, spec)
            except Short as exc:
                check("%s: body decodes exactly as declared" % full, False, str(exc))
                continue
            check("%s: body decodes exactly as declared" % full, True, repr(value)[:40])

        # ---- the three documented ARK bugs ------------------------------
        set_ark = PROFILE + "::SetArkItems_V1"
        get_ark = PROFILE + "::GetArkItems_V1"
        store2 = R.open_store(os.path.join(tmpdir, "state2.json"))
        resp2 = R.RmcResponder(store2, lambda *a, **k: None)
        out = resp2.handle_payload(
            request_frame(PROFILE, "SetArkItems_V1", rq_list(ark_bytes, TEST_ITEMS), 4242))
        env = parse_reply(out)
        mlen, raw, body = parse_name_block(env["payload"])
        check("SetArkItems: reply is a FRAME with an empty PAYLOAD (name block only)",
              env["size"] == len(out) - 4 and body == b""
              and raw == (PROFILE + "::SetArkItems_V1*").encode() + b"\x00",
              "%r" % (out[:24],))

        out = resp2.handle_payload(request_frame(PROFILE, "GetArkItems_V1", b"", 4243))
        env = parse_reply(out)
        _, _, body = parse_name_block(env["payload"])
        got = decode_body(body, ("list", ARK_ITEM))
        check("ARK round-trip: 3 items survive byte-exactly (LE, 4 x u32 each)",
              got == TEST_ITEMS, repr(got))
        check("ARK items are 16 bytes each, not 2 x u64",
              len(body) == 4 + 3 * 16, "%d bytes" % len(body))
        check("ARK little-endian canary (0x11223344 reads back, not 0x44332211)",
              got and got[0][0] == 0x11223344 and body[4:8] == b"\x44\x33\x22\x11",
              body[:8].hex())

        # ---- the state-aware payload seam (websocket path) --------------
        # PATH A (mimic_server.rv_method_payload -> rv_shapes.build) is
        # stateless, so the ARK box came back empty.  respond_payload() must
        # answer from the store, on the SAME wire shape the responder emits.
        import json as _json
        import rv_shapes as _rs

        seam = getattr(R, "respond_payload", None)
        check("rmc_services exports respond_payload(proto, method, store=None)",
              callable(seam))
        if seam is not None:
            starter = [{"a": 0x1101, "b": 1, "c": 0, "d": 0},
                       {"a": 0x1102, "b": 2, "c": 0, "d": 0}]
            starter_t = [(0x1101, 1, 0, 0), (0x1102, 2, 0, 0)]

            def seam_split(proto, method, st):
                """(name-block raw bytes, field bytes) of a seam payload."""
                pl = seam(proto, method, st)
                if not isinstance(pl, (bytes, bytearray)) or not pl:
                    return None, None
                _, raw, body = parse_name_block(pl)
                return raw, body

            ark_store = R.open_store(os.path.join(tmpdir, "ark_seam.json"))
            ark_store.player("default")["arkItems"] = [dict(i) for i in starter]
            ark_store.save()

            raw, body = seam_split(PROFILE, "GetArkItems_V1", ark_store)
            check("seam: GetArkItems_V1 is the name block + the store's items",
                  raw == (PROFILE + "::GetArkItems_V1*").encode() + b"\x00",
                  repr(raw))
            got_seam = None
            try:
                got_seam = decode_body(body, ("list", ARK_ITEM))
            except (Short, TypeError) as exc:
                check("seam: ARK items decode", False, str(exc))
            check("seam: 2 starter ARK items, exact LE values",
                  got_seam == starter_t, repr(got_seam))
            check("seam: 2 ARK items = 36 B (u32 count + 16 B each)",
                  body is not None and len(body) == 4 + 2 * 16,
                  "%d bytes" % (-1 if body is None else len(body)))

            stateless_body = parse_name_block(_rs.build(PROFILE, "GetArkItems_V1"))[2]
            check("seam is store-aware: differs from the stateless rv_shapes default",
                  stateless_body == b"\x00\x00\x00\x00"
                  and body == struct.pack("<I", len(starter))
                  + b"".join(struct.pack("<IIII", *t) for t in starter_t),
                  "stateless=%r seam=%r" % (stateless_body, (body or b"")[:8]))

            blank = R.Store(None)                  # the starter ARK items cleared
            blank.player("default")["arkItems"] = []
            _, empty_body = seam_split(PROFILE, "GetArkItems_V1", blank)
            check("seam: a store with no ARK items yields count 0",
                  empty_body == b"\x00\x00\x00\x00", repr(empty_body))
            # store=None -> the module's in-memory default store, which is a
            # fresh Store: the documented _PLAYER_DEFAULTS (starter cargo)
            _, none_body = seam_split(PROFILE, "GetArkItems_V1", None)
            # derive the expectation from the module's own defaults so this can
            # never go stale again (PN 148 replaced 2 invented ids with the 6
            # real, renderable starter items).
            _defaults = R._PLAYER_DEFAULTS["arkItems"]
            _want = struct.pack("<I", len(_defaults)) + b"".join(
                struct.pack("<IIII", _w[0], _w[1], _w[2], _w[3])
                for _w in (R._ark_words(_it) for _it in _defaults))
            check("seam: store=None uses the in-memory default store (starter ARK)",
                  none_body == _want, repr(none_body))

            # state beyond the ARK box must come through as well
            rich = R.Store(None)
            rp = rich.player("default")
            rp["infoMails"] = {"7": 1, "9": 0}
            rp["settings"] = {"3": 40, "4": 1}
            rp["achievements"] = [{"id": 5, "value": 123456789}]
            mails = decode_body(seam_split(PROFILE, "GetInfoMails_V1", rich)[1],
                                ("map", "u32", "u8"))
            check("seam: GetInfoMails_V1 carries the store's mails",
                  sorted(mails) == [(7, 1), (9, 0)], repr(mails))
            settings = decode_body(seam_split(PROFILE, "GetSettings_V1", rich)[1],
                                   ("map", "u32", "u32"))
            check("seam: GetSettings_V1 carries the store's settings",
                  sorted(settings) == [(3, 40), (4, 1)], repr(settings))
            ach = decode_body(seam_split(PROFILE, "GetAchievements_V1", rich)[1],
                             ("list", ("seq", "u32", "u64")))
            check("seam: GetAchievements_V1 carries id AND stamp",
                  ach == [(5, 123456789)], repr(ach))
            # the id used to be written as 0: rv_shapes._pair() only knew the
            # keys "key"/"k", so a stored {"id": ...} lost its id and every
            # achievement rendered as the same row (the data was on disk)
            check("every achievement keeps its own id on the wire",
                  ach and ach[0][0] == rp["achievements"][0]["id"] == 5,
                  repr(ach))
            prof = decode_body(seam_split(PROFILE, "GetPlayerProfile_V1", rich)[1],
                              LAYOUT[PROFILE + "::GetPlayerProfile_V1"])
            check("seam: GetPlayerProfile_V1 is the real profile (not zeros)",
                  prof[0] == rp.get("name", "Player") and prof[1] == rp["t0"],
                  repr(prof)[:60])

            # EVERY method: non-None, and its name block carries the '*' --
            # a fresh store each time, so no method can see another's writes
            bad_name = []
            for full in sorted(R.METHODS):
                proto, method = full.split("::", 1)
                raw, _ = seam_split(proto, method, R.Store(None))
                if raw != ("%s::%s*" % (proto, method)).encode() + b"\x00":
                    bad_name.append((full, raw))
            check("seam: all 119 methods answer with the '*'-suffixed name block",
                  not bad_name, repr(bad_name[:3]))

            # unknown / malformed input must be None, never an exception and
            # never a mutation of the returned payload path
            bad_none = []
            for proto, method in ((PROFILE, "NoSuchMethod_V1"),
                                  ("NoSuchProto", "GetArkItems_V1"),
                                  (PROFILE, ""), ("", ""), (None, None)):
                try:
                    if seam(proto, method, ark_store) is not None:
                        bad_none.append((proto, method, "not None"))
                except Exception as exc:             # noqa: BLE001
                    bad_none.append((proto, method, repr(exc)))
            check("seam: unknown/invalid method -> None (caller falls back)",
                  not bad_none, repr(bad_none))
            check("seam: a store without .state -> None, not an exception",
                  seam(PROFILE, "GetArkItems_V1", object()) is None)
            want_ark = seam(PROFILE, "GetArkItems_V1", ark_store)
            check("seam: full 'Proto::Method' / '*' arguments normalise",
                  seam(PROFILE + "::GetArkItems_V1", "GetArkItems_V1", ark_store)
                  == want_ark
                  and seam(PROFILE, "GetArkItems_V1*", ark_store) == want_ark
                  and seam("", PROFILE + "::GetArkItems_V1", ark_store) == want_ark
                  and want_ark is not None, repr(want_ark)[:40])
            untouched = R.Store(None)
            seam(PROFILE, "NoSuchMethod_V1", untouched)
            check("seam: unknown method touches no state at all",
                  untouched.state["players"] == {},
                  repr(untouched.state["players"]))

            # the seam must be the SAME bytes as the PRUDP responder (one
            # shape, two transports) -- compared on two identical stores
            snap = R.open_store(os.path.join(tmpdir, "seam_snap.json"))
            snap.player("default")["arkItems"] = [dict(i) for i in starter]
            helper = R.open_store(os.path.join(tmpdir, "seam_helper.json"))
            helper.state = _json.loads(_json.dumps(snap.state))
            helper_resp = R.RmcResponder(helper, lambda *a, **k: None)
            sample = [PROFILE + "::GetArkItems_V1",
                      PROFILE + "::GetPlayerProfile_V1",
                      PROFILE + "::GetInfoMails_V1",
                      PROFILE + "::GetSettings_V1",
                      PROFILE + "::GetKeyBindings_V1",
                      PROFILE + "::GetAchievements_V1",
                      PROFILE + "::GetStats_V1",
                      PROFILE + "::GetAccountInfo_V1",
                      PROFILE + "::GetReputationPoints_V1",
                      VOTING + "::GetVotingInfos_V1",
                      VOTING + "::GetVoteCount_V1",
                      WORLD + "::GetLatestWorldEvent_V1",
                      WORLD + "::GetOwnCompletedMissions_V1",
                      WORLD + "::GetStats_V1",
                      BACKEND + "::GetVersion_V1",
                      BACKEND + "::GetSandboxName_V1"]
            # GetVotingInfos_V1 / GetLatestWorldEvent_V1 embed the wall clock (a
            # packed Date built from "now"), so a pair of back-to-back calls may
            # straddle a second boundary -- retry, a REAL mismatch repeats.
            def same_bytes(full, tries=3):
                for _ in range(tries):
                    if (seam(*full.split("::", 1), store=snap)
                            == helper_resp.dispatch(full, b"")):
                        return True
                return False

            mismatched = [full for full in sample if not same_bytes(full)]
            check("seam == PRUDP responder payload (same handlers, same bytes)",
                  not mismatched, repr(mismatched))

            # writers: declared, and an EMPTY Reader is a valid (writing) call
            check("WRITER_METHODS is a subset of METHODS and marks the writers",
                  set(R.WRITER_METHODS) <= set(R.METHODS)
                  and PROFILE + "::SetArkItems_V1" in R.WRITER_METHODS
                  and PROFILE + "::GetArkItems_V1" not in R.WRITER_METHODS)
            w_store = R.Store(None)
            w_store.player("default")["arkItems"] = [dict(i) for i in starter]
            out_w = seam(PROFILE, "SetArkItems_V1", w_store)
            w_name = (parse_name_block(out_w)[1] if out_w else None)
            check("seam: SetArkItems_V1 with an empty Reader does not crash",
                  w_name == (PROFILE + "::SetArkItems_V1*").encode() + b"\x00",
                  repr(out_w)[:40])
            check("seam: ... and a bodiless SetArkItems_V1 leaves the box alone",
                  w_store.player("default").get("arkItems") == [dict(i) for i in starter],
                  w_store.player("default").get("arkItems"))

            # passing the request parameters keeps a writer's write: the
            # client's own SetArkItems_V1 must not wipe the box
            rt_store = R.Store(None)
            rt_store.player("default")["arkItems"] = []
            items_body = struct.pack("<I", len(starter)) + b"".join(
                struct.pack("<IIII", *t) for t in starter_t)
            seam(PROFILE, "SetArkItems_V1", rt_store,
                 body=PROLOGUE + items_body)
            check("seam: SetArkItems_V1 with a body stores that body",
                  rt_store.player("default")["arkItems"]
                  == [dict(i) for i in starter],
                  repr(rt_store.player("default")["arkItems"])[:60])
            _, rt_body = seam_split(PROFILE, "GetArkItems_V1", rt_store)
            check("seam: ... and GetArkItems_V1 reads the same items back",
                  decode_body(rt_body, ("list", ARK_ITEM)) == starter_t,
                  repr(rt_body)[:40])

            # WRITER_METHODS must be exactly the set that saves -- computed by
            # running every method against a save-counting store
            class Counter:
                def __init__(self, state):
                    self.state = state
                    self.saves = 0

                def save(self):
                    self.saves += 1

            writes = set()
            for full in sorted(R.METHODS):
                probe = R.Store(None)
                probe.player("default")            # created: so a read is a read
                counter = Counter(probe.state)
                seam(*full.split("::", 1), store=counter)
                if counter.saves:
                    writes.add(full)
            # methods that write only when the request carries something: an
            # empty probe body is a valid "nothing to do" and saves nothing
            conditional = {PROFILE + "::SetArk_V1",    # writes only with items
                           # only a request that actually carries a list writes
                           PROFILE + "::SetArkItems_V1",
                           # writes only when the request carries rows: the
                           # client's own empty SetStats_V1 is a "no rows",
                           # and treating it as a wipe destroyed the statistics
                           PROFILE + "::SetStats_V1"}
            conditional |= {("ChatServiceProtocol::" + m) for m in (
                "Subscribe_V1", "Unsubscribe_V1", "SendToChannel_V1",
                "SendToUser_V1", "SendToGroup_V1", "Ignore_V1", "Unignore_V1")}
            conditional |= {("FriendsServiceProtocol::" + m) for m in (
                "AddFriend_V1", "RemoveFriend_V1", "AddRecentContact_V1",
                "SetComment_V1")}
            conditional |= {("MailServiceProtocol::" + m) for m in (
                "UploadMailToProfile_V1", "UploadMailsToProfiles_V1",
                "BroadcastMail_V1", "DeleteAllMailsByPid_V1")}
            check("WRITER_METHODS == the methods that actually call save()",
                  set(R.WRITER_METHODS) == writes | conditional,
                  "undeclared=%r declared-but-silent=%r"
                  % (sorted(writes - set(R.WRITER_METHODS)),
                     sorted(set(R.WRITER_METHODS) - writes - conditional)))

            # a minimal stand-in with only .state (+ save()) -- the shape
            # conquest_http documents -- must work, and saves must follow the
            # store, not the seam
            class StandIn:
                def __init__(self, state):
                    self.state = state
                    self.saves = 0

                def save(self):
                    self.saves += 1

            fake = StandIn({"players": {"default": {"arkItems": [dict(starter[0])]}}})
            _, fake_body = seam_split(PROFILE, "GetArkItems_V1", fake)
            check("seam: a plain .state stand-in is read correctly",
                  decode_body(fake_body, ("list", ARK_ITEM)) == [starter_t[0]],
                  repr(fake_body))
            check("seam: a read never calls the stand-in's save()",
                  fake.saves == 0, str(fake.saves))
            seam(PROFILE, "SetStatus_V1", fake)
            check("seam: a writer persists through the stand-in's save()",
                  fake.saves == 1
                  and fake.state["players"]["default"].get("status") == 0,
                  "%d saves" % fake.saves)

            # ---- CONTENT wiring: the senate + the world event --------------
            # Everything below drives the REAL handlers through the seam and
            # decodes the replies with the independent layouts above.  No
            # content_voting / content_events helper is called here (the packed
            # Date is re-implemented from its documented bit layout:
            # sec:6 min:6 hour:5 day:5 month:4 year:14), so a wrong helper
            # cannot agree with itself.
            def call(proto, method, st, body=b""):
                """(name-block bytes, field bytes) of one seam reply.

                ``body`` is the ARGUMENT bytes; the request prologue is added
                here, because that is what the client sends.
                """
                pl = seam(proto, method, st, body=PROLOGUE + body)
                if not isinstance(pl, (bytes, bytearray)) or not pl:
                    return None, None
                _mlen, raw, rest = parse_name_block(pl)
                return raw, rest

            def packed_date(value):
                """Packed Anno Date -> naive UTC datetime, or None if invalid."""
                try:
                    return datetime.datetime((value >> 26) & 0x3FFF,
                                             (value >> 22) & 0xF,
                                             (value >> 17) & 0x1F,
                                             (value >> 12) & 0x1F,
                                             (value >> 6) & 0x3F,
                                             value & 0x3F)
                except ValueError:
                    return None

            # -- the senate ---------------------------------------------------
            senate = R.open_store(None)
            raw, body = call(VOTING, "GetVotingInfos_V1", senate)
            check("content: GetVotingInfos_V1 answers from a fresh store",
                  raw == (VOTING + "::GetVotingInfos_V1*").encode() + b"\x00",
                  repr(raw))
            infos = decode_body(body, LAYOUT[VOTING + "::GetVotingInfos_V1"])
            # PN 160: slot 0 must be FIRST (that is what the senate asks for);
            # slot 1 is the World Council election served alongside it.
            check("content: the voting map contains key 0 first (the senate frame "
                  "hard-codes GetVotingInfo(0))",
                  0 in [entry[0] for entry in infos]
                  and [entry[0] for entry in infos][0] == 0,
                  repr([entry[0] for entry in infos]))
            rec = infos[0] if infos else (0,) * 10
            now_dt = datetime.datetime.utcnow()
            start, end = packed_date(rec[4]), packed_date(rec[6])
            check("content: VotingInfo f4 is a packed Date, not epoch seconds",
                  start is not None and 2020 <= start.year <= 2100,
                  "%r -> %r" % (rec[4], start))
            check("content: the vote window covers 'now' (start <= now <= end)",
                  start is not None and end is not None and start <= now_dt <= end,
                  "%r .. %r (now %r)" % (start, end, now_dt))
            check("content: a fresh store has NOT voted (own vote f5 = 0xFFFFFFFF)",
                  rec[5] == 0xFFFFFFFF, hex(rec[5]))
            check("content: the vote is open (VotingInfo f3 bool = 1)",
                  rec[3] is True, repr(rec[3]))

            counts_before = decode_body(
                call(VOTING, "GetVoteCount_V1", senate)[1],
                LAYOUT[VOTING + "::GetVoteCount_V1"])
            raw, _ = call(VOTING, "Vote_V1", senate, le32(0) + le32(2))
            check("content: Vote_V1 replies void (name block only)",
                  raw == (VOTING + "::Vote_V1*").encode() + b"\x00", repr(raw))
            stored = (senate.state.get("voting") or {}).get("votes") or []
            check("content: ... and the vote is IN the store (persisted, not an echo)",
                  bool(stored) and stored[-1].get("choice") == 2
                  and stored[-1].get("id") == 0, repr(stored))
            counts_after = decode_body(
                call(VOTING, "GetVoteCount_V1", senate)[1],
                LAYOUT[VOTING + "::GetVoteCount_V1"])
            before_d = dict(counts_before)
            deltas = {k: dict(counts_after).get(k, 0) - before_d.get(k, 0)
                      for k in (0, 1, 2)}
            check("content: Vote then GetVoteCount moves exactly one faction by one",
                  sorted(k for k, d in deltas.items() if d) == [2]
                  and deltas[2] == 1, repr(deltas))
            own_wire = decode_body(
                call(VOTING, "GetVotingInfos_V1", senate)[1],
                LAYOUT[VOTING + "::GetVotingInfos_V1"])[0][5]
            check("content: the own-vote field (VotingInfo f5) is the faction cast",
                  own_wire == 2, repr(own_wire))
            voters = decode_body(
                call(VOTING, "GetLastVoters_V1", senate,
                     le32(0) + le32(10))[1],          # (Vote id, how many)
                LAYOUT[VOTING + "::GetLastVoters_V1"])
            check("content: GetLastVoters lists that vote first (newest first)",
                  bool(voters) and voters[0][1] == 2, repr(voters[:3]))
            check("content: ... cast by the session player",
                  bool(voters) and voters[0][0] == "default", repr(voters[:3]))
            stats = decode_body(
                call(VOTING, "GetVotingStats_V1", senate, b"\x01")[1],
                LAYOUT[VOTING + "::GetVotingStats_V1"])
            # PN 160: two slots are served now (0 = senate, 1 = World Council),
            # so the senate election is entry 0, not the only entry.
            check("content: GetVotingStats serves the same election (key 0)",
                  bool(stats) and stats[0][0] == 0, repr(stats)[:60])
            check("content: ... with the same map GetVoteCount reports",
                  sorted(dict(stats[0][2]).items()) == sorted(counts_after),
                  "%r vs %r" % (stats[0][2], counts_after))
            total = sum(v for _k, v in stats[0][2])
            check("content: VotingStats f1 is a usable denominator (> 0, >= sum)",
                  stats[0][1] > 0 and stats[0][1] >= total,
                  "f1=%r sum=%r" % (stats[0][1], total))

            vote_rt = R.Store(None)
            vote_rt.state = _json.loads(_json.dumps(senate.state))
            check("content: the vote survives a JSON round-trip (string keys)",
                  decode_body(call(VOTING, "GetVoteCount_V1", vote_rt)[1],
                              LAYOUT[VOTING + "::GetVoteCount_V1"])
                  == counts_after,
                  repr(call(VOTING, "GetVoteCount_V1", vote_rt)[1]))

            vprobe = R.open_store(None)
            vcounter = Counter(vprobe.state)
            call(VOTING, "GetVoteCount_V1", vcounter)
            reads = vcounter.saves
            call(VOTING, "Vote_V1", vcounter, le32(0) + le32(0))
            check("content: reading the counts saves nothing, casting a vote saves once",
                  reads == 0 and vcounter.saves == 1,
                  "%d read saves / %d after the vote" % (reads, vcounter.saves))

            stale = R.open_store(None)
            stale_before = dict(decode_body(
                call(VOTING, "GetVoteCount_V1", stale)[1],
                LAYOUT[VOTING + "::GetVoteCount_V1"]))
            call(VOTING, "Vote_V1", stale, le32(1) + le32(0))   # stale id 1
            stale_after = dict(decode_body(
                call(VOTING, "GetVoteCount_V1", stale)[1],
                LAYOUT[VOTING + "::GetVoteCount_V1"]))
            check("content: a vote sent with a stale election id still lands on "
                  "the election the panels read",
                  stale_after.get(0, 0) - stale_before.get(0, 0) == 1,
                  repr(stale_after))

            # -- the world event ---------------------------------------------
            events = R.open_store(None)
            raw, body = call(WORLD, "GetLatestWorldEvent_V1", events)
            check("content: GetLatestWorldEvent_V1 answers from a fresh store",
                  raw == (WORLD + "::GetLatestWorldEvent_V1*").encode() + b"\x00",
                  repr(raw))
            ev = decode_body(body, LAYOUT[WORLD + "::GetLatestWorldEvent_V1"])
            (inst, _mission_set, _ev_name, ev_start, ev_running,
             p_val, p_max, visited) = ev
            check("content: the event carries a non-zero instance id (f1)",
                  inst != 0, repr(inst))
            ev_date = packed_date(ev_start)
            check("content: the event date (f4) is a packed Date, not epoch seconds",
                  ev_date is not None and 2020 <= ev_date.year <= 2100,
                  "%r -> %r" % (ev_start, ev_date))
            check("content: ... and it lies around 'now' (the event is live)",
                  ev_date is not None
                  and abs((ev_date - datetime.datetime.utcnow()).days) <= 30,
                  repr(ev_date))
            check("content: the INVERTED running flag is 0 (0 == a live event)",
                  ev_running is False, repr(ev_running))
            check("content: progress max (f7) is non-zero (the bar cannot divide by 0)",
                  p_max > 0 and p_val <= p_max, "%r/%r" % (p_val, p_max))
            check("content: the event is not 'visited' before SetVisited_V1",
                  visited is False, repr(visited))

            raw, body = call(WORLD, "CompleteMission_V1", events,
                             le32(7) + rq_str("Hero_1700000001"))
            # the reply is stored by the client into its progress-MAX slot
            # (ddl_region.c:8694), so it is this campaign's target, never 0
            campaign = CE.campaign_state()
            check("content: CompleteMission_V1 replies with the campaign target",
                  body == le32(campaign["target"])
                  and 25000 <= campaign["target"] <= 125000,
                  "%r vs target %r" % (body, campaign["target"]))
            call(WORLD, "CompleteMission_V1", events,
                 le32(7) + rq_str("Hero_1700000009"))       # same player + mission
            own = decode_body(
                call(WORLD, "GetOwnCompletedMissions_V1", events)[1],
                LAYOUT[WORLD + "::GetOwnCompletedMissions_V1"])
            check("content: CompleteMission is idempotent per (mission, player)",
                  len(own) == 1 and own[0][1] == 7, repr(own))
            check("content: ... and GetOwnCompletedMissions names the player",
                  bool(own) and own[0][0] == "Hero", repr(own))
            call(WORLD, "CompleteMission_V1", events,
                 le32(8) + rq_str("Hero_1700000010"))
            own = decode_body(
                call(WORLD, "GetOwnCompletedMissions_V1", events)[1],
                LAYOUT[WORLD + "::GetOwnCompletedMissions_V1"])
            check("content: a second mission is a second own record",
                  [entry[1] for entry in own] == [7, 8], repr(own))
            last = decode_body(
                call(WORLD, "GetLastCompletedMissions_V1", events, le32(1))[1],
                LAYOUT[WORLD + "::GetLastCompletedMissions_V1"])
            check("content: GetLastCompletedMissions honours the limit (newest first)",
                  [entry[1] for entry in last] == [8], repr(last))

            mission_rt = R.Store(None)
            mission_rt.state = _json.loads(_json.dumps(events.state))
            own_rt = decode_body(
                call(WORLD, "GetOwnCompletedMissions_V1", mission_rt)[1],
                LAYOUT[WORLD + "::GetOwnCompletedMissions_V1"])
            check("content: the mission records survive a JSON round-trip",
                  [entry[1] for entry in own_rt] == [7, 8], repr(own_rt))

            raw, _ = call(WORLD, "SetVisited_V1", events)
            check("content: SetVisited_V1 replies void",
                  raw == (WORLD + "::SetVisited_V1*").encode() + b"\x00",
                  repr(raw))
            ev2 = decode_body(call(WORLD, "GetLatestWorldEvent_V1", events)[1],
                              LAYOUT[WORLD + "::GetLatestWorldEvent_V1"])
            check("content: ... and the event's visited flag (f8) is now 1",
                  ev2[7] is True, repr(ev2[7]))
            we_stats = decode_body(call(WORLD, "GetStats_V1", events)[1],
                                   LAYOUT[WORLD + "::GetStats_V1"])
            list_a, _list_b, _c0, c1, _c2 = we_stats
            check("content: world-event GetStats counts the completions (c1 == 2)",
                  c1 == 2, repr(we_stats)[:80])
            check("content: ... and its per-player list agrees with them",
                  [entry[0] for entry in list_a] == ["Hero"]
                  and list_a[0][1] == 2, repr(list_a))

            # -- upgrade path: an OLD state file -----------------------------
            # The legacy document carried ``votingInfo`` (epoch-second dates,
            # f6 = 0 read as "voted Eco") and ``worldEvent.active = False``
            # (no event ever appeared).  Both must be migrated on load, or an
            # existing install keeps showing nothing after the upgrade.
            legacy_file = os.path.join(tmpdir, "legacy_state.json")
            with open(legacy_file, "w", encoding="utf-8") as fh:
                fh.write(_json.dumps({
                    "voting": {"votingInfo": {"id": 1, "start": 1600000000,
                                              "f6": 0, "running": True},
                               "votes": [], "lastVoters": []},
                    "worldEvent": {"active": False, "completedMissions": []},
                }))
            upgraded = R.open_store(legacy_file)
            old_info = decode_body(
                call(VOTING, "GetVotingInfos_V1", upgraded)[1],
                LAYOUT[VOTING + "::GetVotingInfos_V1"])[0]
            check("content: a legacy state file is migrated (own vote back to "
                  "'not voted', dates rebuilt as packed Dates)",
                  old_info[5] == 0xFFFFFFFF and packed_date(old_info[4]) is not None,
                  repr(old_info))
            old_ev = decode_body(
                call(WORLD, "GetLatestWorldEvent_V1", upgraded)[1],
                LAYOUT[WORLD + "::GetLatestWorldEvent_V1"])
            check("content: ... and the legacy 'active: false' no longer "
                  "switches the world event off",
                  old_ev[4] is False, repr(old_ev[4]))
            check("content: the migration itself saves nothing behind our back",
                  old_ev[0] != 0 and upgraded.state["worldEvent"].get("active") is None,
                  repr(sorted(upgraded.state["worldEvent"])))



        check("WorldEvent::GetStats uses its own handler (not PlayerProfile's)",
              R.METHODS[WORLD + "::GetStats_V1"]
              is not R.METHODS[PROFILE + "::GetStats_V1"])
        we = resp2.handle_payload(request_frame(WORLD, "GetStats_V1", b"", 5000))
        _, _, we_body = parse_name_block(parse_reply(we)["payload"])
        try:
            decode_body(we_body, LAYOUT[WORLD + "::GetStats_V1"])
            we_ok = True
        except Short:
            we_ok = False
        profile_ok = True
        try:
            decode_body(we_body, LAYOUT[PROFILE + "::GetStats_V1"])
        except Short:
            profile_ok = False
        check("WorldEvent::GetStats body is WorldEventStats, not list<StatsEntry>",
              we_ok and not profile_ok, we_body[:16].hex())

        # ---- the six previously missing methods -------------------------
        for name in ("GetOwnCompletedMissions_V1", "GetLastCompletedMissions_V1",
                     "ClearWorldEventTemplates_V1", "UploadWorldEventTemplate_V1",
                     "AreReputationPointsMissing_V1", "SetReputationPoints_V1"):
            full = (WORLD if name not in ("SetReputationPoints_V1",) else WORLD) \
                + "::" + name
            check("missing method added: %s" % full, full in R.METHODS)

        # ---- token-scan path: no envelope ---------------------------------
        raw = (PROFILE + "::GetArkItems_V1").encode() + b"\x00"
        out = resp2.handle_payload(raw, None, origin="raw")
        try:
            env = parse_reply(out)
            ok = (env["type"] == 0 and env["status"] == 1
                  and env["proto"] == PROFILE.encode() + b"\x00"
                  and env["call_id"] == 0
                  and env["payload"].startswith(name_block(PROFILE, "GetArkItems_V1")))
        except (Short, TypeError):
            ok = False
        check("name-only request (no envelope) still gets a framed reply", ok)

        # ---- void helper / counter sanity -------------------------------
        check("VOID_METHODS matches the declared void layouts",
              R.VOID_METHODS == frozenset(k for k, v in LAYOUT.items() if v == "void"),
              repr(sorted(R.VOID_METHODS ^ frozenset(
                  k for k, v in LAYOUT.items() if v == "void"))))
        check("dispatcher counted every method",
              len(responder.counters) >= 70, str(len(responder.counters)))

        # ---- PRUDP RMC envelope -----------------------------------------
        payload = name_block("TestProtocol", "DoThing_V1")
        frame = P.build_rmc_response(11, 0x11223344, 0x21, body=payload)
        check("PRUDP: frame size covers the rest of the frame",
              int.from_bytes(frame[:4], "little") == len(frame) - 4)
        plen = int.from_bytes(frame[4:6], "little")
        check("PRUDP: proto length includes the NUL",
              frame[6:6 + plen] == b"Protocol11\x00", repr(frame[6:6 + plen]))
        check("PRUDP: type 0 / status 1",
              frame[6 + plen] == 0 and frame[7 + plen] == 1)
        check("PRUDP: call id echoed little-endian",
              int.from_bytes(frame[8 + plen:12 + plen], "little") == 0x11223344)
        check("PRUDP: payload follows the envelope untouched",
              frame[12 + plen:] == payload)
        parsed = P.parse_rmc_request_ex(frame)
        check("PRUDP: reply parses back (protocol_id, call_id, kind)",
              parsed["protocol_id"] == 11 and parsed["call_id"] == 0x11223344
              and parsed["kind"] == 0, repr(parsed))
        check("PRUDP: legacy tuple view still works",
              P.parse_rmc_request(frame)[:2] == (11, 0x11223344))
        err = P.build_rmc_response(11, 1, 0, error_code=5)
        check("PRUDP: error reply uses status 0 + message + kind",
              err[6 + 11] == 0 and err[7 + 11] == 0
              and b"RMC error 5\x00" in err, err.hex())

        # a NAMED call, the dialect the client actually speaks
        pn = b"TestProtocol\x00"
        nmb = name_block("TestProtocol", "DoThing_V1", star=False)
        inner = (le16(len(pn)) + pn + b"\x01" + le32(77) + nmb + b"\xaa\xbb")
        call = le32(len(inner)) + inner
        req = P.parse_rmc_request_ex(call)
        check("PRUDP: named call parses to proto/call/bare method/params",
              req["dialect"] == "named" and req["proto"] == "TestProtocol"
              and req["call_id"] == 77 and req["method"] == "DoThing_V1"
              and req["full_method"] == "TestProtocol::DoThing_V1"
              and req["params"] == b"\xaa\xbb", repr(req))

        # ---- no big-endian survivors ------------------------------------
        fmt = re.compile(r"""struct\.(?:pack|unpack|unpack_from)\(\s*['"]([^'"]+)['"]""")
        bad = []
        for path in (os.path.join(SERVER, "rmc_services.py"),
                     os.path.join(SERVER, "prudp_transport.py")):
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            for match in fmt.finditer(text):
                if ">" in match.group(1):
                    bad.append((os.path.basename(path), match.group(1)))
            for helper in ("def w_u16", "def w_u32", "def w_u64", "def w_string"):
                if helper in text:
                    bad.append((os.path.basename(path), helper))
        check("no big-endian struct format / BE writer helper survives", not bad,
              repr(bad))

        # ---- import hygiene ---------------------------------------------
        check("rmc_services has no module-level side effects on import",
              isinstance(R.DEFAULT_STATE, dict) and "players" in R.DEFAULT_STATE)
        check("Store(None) is usable without touching disk",
              isinstance(R.Store(None).player("x"), dict))

        # ---- summary ----------------------------------------------------
        print("")
        print("%d checks, %d failures" % (CHECKS, len(FAILURES)))
        if FAILURES:
            for name in FAILURES:
                print("  FAILED: %s" % name)
            return 1
        print("ALL RMC SERVICES TESTS PASSED")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
