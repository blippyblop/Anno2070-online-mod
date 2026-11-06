#!/usr/bin/env python3
"""Self-test for ``rv_shapes.profile`` (PlayerProfileServiceProtocol shapes).

Plain python, no pytest: run ``python3 tools/test_rv_shapes_profile.py``.
Exits 0 on success, 1 with the failing check printed otherwise.
"""
import os
import re
import struct
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
READER = os.path.join(
    os.path.dirname(HERE),
    "decomp",
    "__AVPlayerProfileServiceProtocolClient_rdv__.c",
)
sys.path.insert(0, SERVER)

from rv_shapes import name_block, qstr, u8, u16, u32, u64  # noqa: E402
from rv_shapes import profile  # noqa: E402

PROTO = "PlayerProfileServiceProtocol"
CHECKS = [0]
FAILED = []


def check(cond, msg):
    CHECKS[0] += 1
    if not cond:
        FAILED.append(msg)
        print("FAIL:", msg)
    return bool(cond)


def eq(got, want, msg):
    return check(got == want, "%s\n      got  %r\n      want %r" % (msg, got, want))


def nb(method, proto=PROTO):
    """Expected name block, built independently of profile.py."""
    nm = ("%s::%s*" % (proto, method)).encode("ascii") + b"\x00"
    return struct.pack("<H", len(nm)) + nm


def body(payload, method, proto=PROTO):
    """Strip the name block; return None when it does not match."""
    head = nb(method, proto)
    if payload[: len(head)] != head:
        return None
    return payload[len(head):]


JUNK = [
    None, "", "x", "12", b"", b"\x01\x02", bytearray(b"abcd"), memoryview(b"zyxw"),
    0, 1, -1, 2 ** 40, 2 ** 70, -2 ** 40, 1.5, float("nan"), float("inf"), True,
    False, [], (), {}, set(), object(), [None], [{}], [object()], {"a": None},
    {"a": "b", "b": object()}, (None, "x"), [[1, 2, 3, 4, 5]], {"key": None},
    [1, "a", None, b"\x00"], range(3), iter([1, 2]),
]


# ---------------------------------------------------------------------------
# 1. the reader's method list (parse the dump, fall back to a frozen copy)
# ---------------------------------------------------------------------------
FALLBACK = [
    "SetStatus_V1", "CreatePlayerProfile_V1", "GetPlayerProfile_V1",
    "DoesUserExist_V1", "SetPortrait_V1", "SetIcon_V1", "SetColor_V1",
    "SetPlayerTitle_V1", "SetArk_V1", "AddActivation_V1", "GetAchievements_V1",
    "AddAchievement_V1", "GetSettings_V1", "SetSetting_V1", "GetKeyBindings_V1",
    "SetKeyBinding_V1", "ResetKeyBindings_V1", "AddHelperQuest_V1",
    "ResetHelperQuests_V1", "GetInfoMails_V1", "AddInfoMail_V1",
    "SetInfoMailRead_V1", "GetStats_V1", "SetStats_V1", "GetArkItems_V1",
    "SetArkItems_V1", "GetStatus_V1", "GetWorldStats_V1", "ResetInfoMails_V1",
    "UploadWorldStatsStencils_V1", "GetReputationPoints_V1",
    "AreReputationPointsMissing_V1", "SetReputationPoints_V1",
    "SolveDailyQuest_V1", "UploadFactionRewards_V1", "UploadStatsReputation_V1",
    "GetFacebookAccount_V1", "SetFacebookAccount_V1",
    "InvalidateAllReputationPoints_V1", "UploadReputationMails_V1",
    "ActivateUniqueKey_V1", "InitializeService_V1", "AddShopItem_V1",
    "RemoveShopItem_V1", "ListShopItems_V1", "UploadShopItems_V1",
    "UploadSteamAppIdMapping_V1", "UpdateUPCActivations_V1",
    "UpdateSteamActivations_V1", "GetAccountInfo_V1",
    "UploadAchievementRewards_V1", "FetchAssetCounterGraph_V1",
    "UploadLeaderboardTemplates_V1", "GetLeaderboardTemplates_V1",
]

reader_methods = []
if os.path.exists(READER):
    with open(READER, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    seen = []
    for m in re.finditer(r'"PlayerProfileServiceProtocol::([A-Za-z0-9_]+)"', text):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    reader_methods = seen
    eq(reader_methods, FALLBACK, "reader method list matches the frozen copy")
else:  # pragma: no cover - the dump is normally present
    reader_methods = list(FALLBACK)
    print("note: reader dump not found, using frozen method list")

eq(len(reader_methods), 54, "reader declares 54 methods")
eq(list(profile.ALL_METHODS), reader_methods, "ALL_METHODS == reader order")

# coverage: implemented + unknown == every reader method, no overlap
covered = set(profile.METHOD_NAMES) | set(profile.UNKNOWN_METHODS)
eq(sorted(covered), sorted(reader_methods), "METHOD_NAMES + UNKNOWN_METHODS cover reader")
eq(sorted(set(profile.METHOD_NAMES) & set(profile.UNKNOWN_METHODS)), [],
   "METHOD_NAMES and UNKNOWN_METHODS are disjoint")
check(len(profile.METHOD_NAMES) == 54,
      "54 methods implemented, got %d" % len(profile.METHOD_NAMES))
check(len(profile.UNKNOWN_METHODS) == 0,
      "every reader is implemented (UNKNOWN_METHODS empty), got %d"
      % len(profile.UNKNOWN_METHODS))
for name, why in profile.UNKNOWN_METHODS.items():
    check(isinstance(why, str) and len(why) > 10,
          "UNKNOWN_METHODS[%r] explains what is missing" % name)
    check(profile.build(PROTO, name) is None,
          "build() returns None for unknown %r" % name)
check(all(profile.build(PROTO, m) is not None for m in reader_methods),
      "build() returns a payload for every method the client declares")

# ---------------------------------------------------------------------------
# 2. exact name blocks for every implemented method
# ---------------------------------------------------------------------------
for name in profile.METHOD_NAMES:
    payload = profile.build(PROTO, name)
    check(isinstance(payload, bytes), "build(%r) returns bytes" % name)
    if not isinstance(payload, bytes):
        continue
    eq(payload[: len(nb(name))], nb(name), "build(%r) name block exact" % name)
    eq(payload[: len(nb(name))][-2:], b"*\x00",
       "build(%r) name ends with '*' NUL" % name)
    # u16 length includes the NUL and matches the bytes that follow
    (declared,) = struct.unpack_from("<H", payload, 0)
    eq(declared, len(nb(name)) - 2, "build(%r) u16 name length includes NUL" % name)
    # every public m_<lowername> exists and agrees with build()
    fn_name = "m_" + name.lower()
    check(hasattr(profile, fn_name), "public builder %s exists" % fn_name)
    if hasattr(profile, fn_name):
        eq(getattr(profile, fn_name)(), payload, "%s() == build(%r)" % (fn_name, name))

# build() accepts Prefixed / starred spellings and a different proto
eq(profile.build(PROTO, "GetArkItems_V1*"), profile.build(PROTO, "GetArkItems_V1"),
   "trailing '*' accepted by build()")
eq(profile.build(PROTO, "PlayerProfileServiceProtocol::GetArkItems_V1"),
   profile.build(PROTO, "GetArkItems_V1"), "proto prefix accepted by build()")
eq(profile.build(PROTO, b"GetStatus_V1"), profile.build(PROTO, "GetStatus_V1"),
   "bytes method name accepted by build()")
alt = profile.build("OtherProto", "GetStatus_V1")
eq(alt[: len(nb("GetStatus_V1", "OtherProto"))], nb("GetStatus_V1", "OtherProto"),
   "build() honours the supplied protocol name")

# ---------------------------------------------------------------------------
# 3. the 16-byte ArkItem record and the list codec
# ---------------------------------------------------------------------------
item = profile.ark_item(7001, 1, 0x11111111, 0x22222222)
eq(len(item), 16, "ark_item is 16 bytes")
eq(item, struct.pack("<IIII", 7001, 1, 0x11111111, 0x22222222),
   "ark_item = 4 x u32 LE")
eq(item, bytes.fromhex("591b0000" "01000000" "11111111" "22222222"),
   "ark_item byte order is little-endian")
eq(profile.ark_item(), b"\x00" * 16, "ark_item() defaults to 16 zero bytes")
eq(profile.ark_item(b=5), struct.pack("<IIII", 0, 5, 0, 0),
   "ark_item kwargs map to f0..f3")
eq(profile.ark_item(-1, 2 ** 32 + 3, 2 ** 64, 1.9),
   struct.pack("<IIII", 0xFFFFFFFF, 3, 0, 1), "ark_item masks/coerces fields")
# four independent u32, NOT two u64: high words must be separate fields
eq(profile.ark_item(1, 2, 3, 4), struct.pack("<IIII", 1, 2, 3, 4),
   "ark_item keeps f1/f3 as independent words (not u64 high halves)")

# count prefix for 0 / 1 / 3 items
eq(profile.ark_items(()), struct.pack("<I", 0), "0 items -> u32 0 only")
eq(profile.ark_items([]), b"\x00\x00\x00\x00", "empty list -> 4-byte count 0")
one = profile.ark_items([(7001, 1, 0, 0)])
eq(len(one), 4 + 16, "1 item -> 4 + 16 bytes")
eq(one, struct.pack("<I", 1) + profile.ark_item(7001, 1, 0, 0), "1 item count+record")
three = profile.ark_items([(7001, 1, 0, 0), (7002, 2, 0, 0), (7003, 3, 0, 0)])
eq(len(three), 4 + 48, "3 items -> 4 + 48 bytes")
eq(struct.unpack_from("<I", three, 0)[0], 3, "3 item count prefix")
eq(struct.unpack_from("<IIII", three, 4), (7001, 1, 0, 0), "3 item #0")
eq(struct.unpack_from("<IIII", three, 4 + 32), (7003, 3, 0, 0), "3 item #2")

# weight-4-count check: a count that is not < 2**24 must survive (the old
# server bug sliced the NUL into the body, which only worked for small counts)
big = profile.ark_items([(i, i, i, i) for i in range(300)])
eq(len(big), 4 + 300 * 16, "300-item list length")
eq(struct.unpack_from("<I", big, 0)[0], 300, "300-item count prefix")
eq(big[4:8], struct.pack("<I", 0), "first item of a 300-item list")

# accepted element spellings
eq(profile.ark_items([5]), profile.ark_items([(5, 0, 0, 0)]), "int element -> f0")
eq(profile.ark_items({"a": 1, "b": 2, "c": 3, "d": 4}),
   profile.ark_items([(1, 2, 3, 4)]), "dict element a..d")
eq(profile.ark_items({"f0": 1, "f1": 2, "f2": 3, "f3": 4}),
   profile.ark_items([(1, 2, 3, 4)]), "dict element f0..f3")
eq(profile.ark_items([profile.ark_item(1, 2, 3, 4)]),
   profile.ark_items([(1, 2, 3, 4)]), "raw 16-byte element passthrough")
eq(profile.ark_items(profile.ark_item(1, 2, 3, 4)),
   profile.ark_items([(1, 2, 3, 4)]), "whole blob -> one item")
eq(profile.ark_items(profile.ark_item(1, 2, 3, 4) * 2),
   profile.ark_items([(1, 2, 3, 4), (1, 2, 3, 4)]), "blob chunks into records")
eq(profile.ark_items([(1, 2)]), profile.ark_items([(1, 2, 0, 0)]), "short tuple padded")

# ARK method payloads
eq(profile.get_ark_items(), nb("GetArkItems_V1") + b"\x00\x00\x00\x00",
   "GetArkItems_V1 with no items -> empty list")
eq(profile.get_ark_items([(7001, 1, 0, 0), (7002, 2, 0, 0)]),
   nb("GetArkItems_V1") + profile.ark_items([(7001, 1, 0, 0), (7002, 2, 0, 0)]),
   "GetArkItems_V1 = name block + list")
eq(profile.m_getarkitems_v1([(1, 2, 3, 4)]), profile.get_ark_items([(1, 2, 3, 4)]),
   "m_getarkitems_v1 aliases get_ark_items")
eq(profile.set_ark_items([(1, 2, 3, 4)]), nb("SetArkItems_V1"),
   "SetArkItems_V1 reply is the bare name block")
eq(profile.set_ark(), nb("SetArk_V1"), "SetArk_V1 reply is the bare name block")
eq(profile.m_setark_v1(1, 2, 3, junk=object()), nb("SetArk_V1"),
   "m_setark_v1 is void and total")
eq(body(profile.build(PROTO, "SetArkItems_V1", items=[(9, 9, 9, 9)]), "SetArkItems_V1"),
   b"", "build(SetArkItems_V1) has no result fields")
eq(body(profile.build(PROTO, "SetArk_V1"), "SetArk_V1"), b"",
   "build(SetArk_V1) has no result fields")

# ---------------------------------------------------------------------------
# 4. the other decoded layouts
# ---------------------------------------------------------------------------
eq(body(profile.build(PROTO, "GetStatus_V1", status=7), "GetStatus_V1"),
   struct.pack("<I", 7), "GetStatus_V1 -> u32")
eq(body(profile.build(PROTO, "DoesUserExist_V1", exists=True), "DoesUserExist_V1"),
   b"\x01", "DoesUserExist_V1 -> u8 bool")
eq(body(profile.build(PROTO, "DoesUserExist_V1", exists=False), "DoesUserExist_V1"),
   b"\x00", "DoesUserExist_V1 false -> 0x00")
eq(body(profile.build(PROTO, "AreReputationPointsMissing_V1", missing=1),
        "AreReputationPointsMissing_V1"), b"\x01", "AreReputationPointsMissing -> u8")
eq(body(profile.build(PROTO, "AddAchievement_V1", value=0x1122334455667788),
        "AddAchievement_V1"), struct.pack("<Q", 0x1122334455667788),
   "AddAchievement_V1 -> 8 raw bytes (u64 LE)")

ach = body(profile.build(PROTO, "GetAchievements_V1", items=[(11, 22), (33, 44)]),
           "GetAchievements_V1")
eq(len(ach), 4 + 2 * 12, "Achievement list: count + 12-byte records")
eq(struct.unpack_from("<I", ach, 0)[0], 2, "Achievement count")
eq(struct.unpack_from("<IQ", ach, 4), (11, 22), "Achievement record 0")
eq(struct.unpack_from("<IQ", ach, 4 + 12), (33, 44), "Achievement record 1")
eq(profile.m_getachievements_v1([]), nb("GetAchievements_V1") + b"\x00\x00\x00\x00",
   "empty Achievement list")

st = body(profile.build(PROTO, "GetStats_V1", items=[(1, 2, 3, 4, 5, 6)]), "GetStats_V1")
eq(len(st), 4 + 36, "StatsEntry list: count + 36-byte record")
eq(struct.unpack_from("<IIIQQQ", st, 4), (1, 2, 3, 4, 5, 6), "StatsEntry widths")

kn = body(profile.build(PROTO, "GetKeyBindings_V1",
                        bindings=[(10, [1, 2, 3]), (20, [])]), "GetKeyBindings_V1")
eq(len(kn), 4 + (4 + 4 + 12) + (4 + 4), "KeyBinding map length")
eq(struct.unpack_from("<I", kn, 0)[0], 2, "KeyBinding count")
eq(struct.unpack_from("<II", kn, 4), (10, 3), "KeyBinding key + inner count")
eq(struct.unpack_from("<III", kn, 12), (1, 2, 3), "KeyBinding inner values")
eq(struct.unpack_from("<II", kn, 24), (20, 0), "KeyBinding empty inner list")
eq(body(profile.build(PROTO, "GetKeyBindings_V1", bindings={7: [4]}),
        "GetKeyBindings_V1"),
   struct.pack("<IIII", 1, 7, 1, 4), "KeyBinding dict input")

se = body(profile.build(PROTO, "GetSettings_V1", settings={5: 6, 7: 8}), "GetSettings_V1")
eq(se, struct.pack("<IIIII", 2, 5, 6, 7, 8), "GetSettings map<u32,u32>")
im = body(profile.build(PROTO, "GetInfoMails_V1", mails=[(9, True), (10, False)]),
          "GetInfoMails_V1")
eq(im, struct.pack("<II", 2, 9) + b"\x01" + struct.pack("<I", 10) + b"\x00",
   "GetInfoMails map<u32,bool> is 5 bytes per entry")

# void methods: payload is exactly the name block, nothing else
for name in profile.METHOD_NAMES:
    if profile.METHOD_NAMES[name] == "void":
        p = profile.build(PROTO, name)
        eq(len(p), len(nb(name)), "%r void reply is name block only" % name)

# ---------------------------------------------------------------------------
# 4b. the 10 methods that used to be UNKNOWN (readers decoded 2026-11-05)
# ---------------------------------------------------------------------------
#: dispatcher FUN_140df0640 -> payload reader, from the dispositive call sites
#: (decomp/__AVPlayerProfileServiceProtocolClient_rdv__.c:39..582).  The whole
#: table is in decomp/batch/step4_readers/PROFILE-UNKNOWN.md.
DEDICATED_READERS = {
    "GetWorldStats_V1": "FUN_140de9e90",
    "GetReputationPoints_V1": "FUN_140de9f50",
    "GetFacebookAccount_V1": "FUN_140de9fe0",
    "ActivateUniqueKey_V1": "FUN_140dea0a0",
    "ListShopItems_V1": "FUN_140dea130",
    "GetAccountInfo_V1": "FUN_140dea1f0",
    "FetchAssetCounterGraph_V1": "FUN_140dea2a0",
    "GetLeaderboardTemplates_V1": "FUN_140dea360",
}

# the dispatcher dump is the dispositive evidence: re-derive the mapping from
# it so the table above cannot silently rot.  The chain is a nest of
# `if (cVar1 == '\0') { <compare name> <fields> } else { <reader>(param_2,param_3); }`
# blocks, so a brace-stack walk (method name attached to the `if` it guards)
# attributes each reader call to the method that was just compared.
if os.path.exists(READER):
    DISPATCH_TOKEN = re.compile(
        r'(?P<name>"PlayerProfileServiceProtocol::(?P<m>[A-Za-z0-9_]+)")'
        r"|(?P<if>if \(cVar1 == '\\0'\) \{)"
        r"|(?P<iffalse>if \(cVar1 != '\\0'\) \{)"
        r"|(?P<else>else \{)"
        r"|(?P<open>\{)"
        r"|(?P<close>\})"
        r"|(?P<call>(?P<fn>FUN_140d[0-9a-f]+)\(\s*param_2\s*,)"
    )
    stack, last_name, last_closed, found = [], None, None, {}
    for tok in DISPATCH_TOKEN.finditer(text):
        if tok.group("name") is not None:
            last_name = tok.group("m")
        elif tok.group("if") is not None:
            stack.append(("if", last_name))
        elif tok.group("iffalse") is not None:
            # `if (cVar1 != '\0') { <reader>(...); }` -- the straight-line
            # branch that owns a dedicated reader.  Named but never an
            # else-target, so `last_closed` stays reserved for the `== 0` form.
            stack.append(("block", last_name))
        elif tok.group("else") is not None:
            stack.append(("else", last_closed))
        elif tok.group("open") is not None:
            stack.append(("block", None))
        elif tok.group("close") is not None:
            popped = stack.pop() if stack else None
            if popped and popped[0] == "if":
                last_closed = popped[1]
        elif tok.group("call") is not None:
            owner = None
            for kind, name in reversed(stack):
                if name is not None:
                    owner = name
                    break
            if owner is not None:
                found.setdefault(owner, tok.group("fn"))
    for name, reader in DEDICATED_READERS.items():
        eq(found.get(name), reader, "dispatcher calls %s for %s" % (reader, name))
    eq(found.get("GetPlayerProfile_V1"), "FUN_140df1d50",
       "dispatcher calls FUN_140df1d50 for GetPlayerProfile_V1")
else:  # pragma: no cover - the dump is normally present
    print("note: reader dump not found, skipping the dispatcher mapping check")


class Rd(object):
    """Minimal re-implementation of one reply reader, for structural checks."""

    def __init__(self, blob):
        self.b = blob or b""
        self.i = 0

    def raw(self, n):
        out = self.b[self.i:self.i + n]
        assert len(out) == n, "truncated at %d (%d wanted)" % (self.i, n)
        self.i += n
        return out

    def u8(self):
        return struct.unpack("<B", self.raw(1))[0]

    def u16(self):
        return struct.unpack("<H", self.raw(2))[0]

    def u32(self):
        return struct.unpack("<I", self.raw(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.raw(8))[0]

    def string(self):
        return self.raw(self.u16())

    def done(self):
        return self.i == len(self.b)


def dec_list(r, item):
    return [item(r) for _ in range(r.u32())]


def dec_stats_entry(r):
    return (r.u32(), r.u32(), r.u32(), r.u64(), r.u64(), r.u64())


def dec_profile(r):
    """FUN_140df1d50: String, 5 x u32, 2 x list<u32>, String."""
    return (r.string(), [r.u32() for _ in range(5)],
            dec_list(r, lambda x: x.u32()), dec_list(r, lambda x: x.u32()),
            r.string())


def dec_lb_template(r):
    """FUN_140df1e10: u32, u32, u64, u64, u32, u32 n, n bytes."""
    return (r.u32(), r.u32(), r.u64(), r.u64(), r.u32(),
            r.raw(r.u32()))


NEW_DECODERS = {
    "GetWorldStats_V1": lambda r: (r.u32(), dec_list(r, dec_stats_entry)),
    "GetReputationPoints_V1": lambda r: dec_list(r, lambda x: (x.u32(), x.u32())),
    "GetFacebookAccount_V1": lambda r: (r.u64(), r.string()),
    "ActivateUniqueKey_V1": lambda r: dec_list(r, lambda x: x.u32()),
    "ListShopItems_V1": lambda r: (r.u32(), dec_list(r, lambda x: x.u32())),
    "GetAccountInfo_V1": lambda r: (r.string(), r.string()),
    "FetchAssetCounterGraph_V1":
        lambda r: (dec_list(r, lambda x: (x.u32(), x.u32())), r.u32()),
    "GetLeaderboardTemplates_V1": lambda r: dec_list(r, dec_lb_template),
    "GetPlayerProfile_V1": dec_profile,
    "CreatePlayerProfile_V1":
        lambda r: (r.string(), r.u16(), dec_profile(r),
                   dec_list(r, lambda x: (x.u32(), x.u8()))),
}

for name, decoder in NEW_DECODERS.items():
    payload = profile.build(PROTO, name)
    check(payload is not None, "build(%r) implemented" % name)
    if not payload:
        continue
    blob = body(payload, name)
    if blob is None:
        check(False, "%r name block missing" % name)
        continue
    r = Rd(blob)
    try:
        decoder(r)
    except AssertionError as exc:
        check(False, "%r default body does not decode: %s" % (name, exc))
    else:
        check(r.done(), "%r default body is exactly consumed (%d B)" % (name, len(blob)))

# --- exact byte layouts -----------------------------------------------------
ws = body(profile.build(PROTO, "GetWorldStats_V1",
                        version=3, entries=[(1, 2, 3, 4, 5, 6)]), "GetWorldStats_V1")
eq(ws, struct.pack("<II", 3, 1) + struct.pack("<IIIQQQ", 1, 2, 3, 4, 5, 6),
   "GetWorldStats_V1 = u32 + list<StatsEntry>")
eq(body(profile.build(PROTO, "GetWorldStats_V1", entries=()), "GetWorldStats_V1"),
   b"\x00" * 8, "GetWorldStats_V1 empty = u32 + count 0 (8 bytes)")

rp = body(profile.build(PROTO, "GetReputationPoints_V1", pairs={5: 6, 7: 8}),
          "GetReputationPoints_V1")
eq(rp, struct.pack("<IIIII", 2, 5, 6, 7, 8), "GetReputationPoints_V1 = map<u32,u32>")
eq(body(profile.build(PROTO, "GetReputationPoints_V1"), "GetReputationPoints_V1"),
   b"\x00\x00\x00\x00", "GetReputationPoints_V1 empty = 4-byte count only")

fb = body(profile.build(PROTO, "GetFacebookAccount_V1",
                        account_id=0x1122334455667788, name="bob"),
          "GetFacebookAccount_V1")
eq(fb, struct.pack("<Q", 0x1122334455667788) + b"\x04\x00bob\x00",
   "GetFacebookAccount_V1 = u64 + String")
eq(len(body(profile.build(PROTO, "GetFacebookAccount_V1"), "GetFacebookAccount_V1")),
   11, "GetFacebookAccount_V1 empty = 8 bytes + u16 1 + NUL")

uk = body(profile.build(PROTO, "ActivateUniqueKey_V1", keys=[1, 2, 3]),
          "ActivateUniqueKey_V1")
eq(uk, struct.pack("<IIII", 3, 1, 2, 3), "ActivateUniqueKey_V1 = list<u32>")

sh = body(profile.build(PROTO, "ListShopItems_V1", status=1, items=[7, 8]),
          "ListShopItems_V1")
eq(sh, struct.pack("<IIII", 1, 2, 7, 8),
   "ListShopItems_V1 = u32 + list<u32>")
eq(body(profile.build(PROTO, "ListShopItems_V1"), "ListShopItems_V1"), b"\x00" * 8,
   "ListShopItems_V1 empty = u32 + count 0")

ai = body(profile.build(PROTO, "GetAccountInfo_V1", name="a", account="bc"),
          "GetAccountInfo_V1")
eq(ai, b"\x02\x00a\x00" + b"\x03\x00bc\x00", "GetAccountInfo_V1 = String + String")
eq(body(profile.build(PROTO, "GetAccountInfo_V1"), "GetAccountInfo_V1"),
   b"\x01\x00\x00" * 2, "GetAccountInfo_V1 empty = two empty Strings")

ag = body(profile.build(PROTO, "FetchAssetCounterGraph_V1", pairs={1: 2}, suffix=9),
          "FetchAssetCounterGraph_V1")
eq(ag, struct.pack("<IIII", 1, 1, 2, 9),
   "FetchAssetCounterGraph_V1 = map<u32,u32> + u32")
eq(body(profile.build(PROTO, "FetchAssetCounterGraph_V1"), "FetchAssetCounterGraph_V1"),
   b"\x00" * 8, "FetchAssetCounterGraph_V1 empty = count 0 + u32")

lt = body(profile.build(PROTO, "GetLeaderboardTemplates_V1",
                        items=[(1, 2, 3, 4, 5, b"\xaa\xbb")]), "GetLeaderboardTemplates_V1")
eq(lt, struct.pack("<I", 1) + struct.pack("<IIQQII", 1, 2, 3, 4, 5, 2) + b"\xaa\xbb",
   "GetLeaderboardTemplates_V1 = u32 count + (u32,u32,u64,u64,u32,u32 n,n x u8)")
eq(len(lt), 4 + 34, "one LeaderboardTemplate = 32 + 2 bytes")

pp = body(profile.build(PROTO, "GetPlayerProfile_V1", name="n", values=[1, 2, 3],
                        list_a=[9], list_b=[], tail="t"), "GetPlayerProfile_V1")
eq(pp, b"\x02\x00n\x00" + struct.pack("<IIIII", 1, 2, 3, 0, 0)
   + struct.pack("<II", 1, 9) + struct.pack("<I", 0) + b"\x02\x00t\x00",
   "GetPlayerProfile_V1 = String + 5 x u32 + 2 x list<u32> + String")
eq(len(pp), 4 + 20 + 8 + 4 + 4, "GetPlayerProfile_V1 layout is 40 bytes here")
eq(len(body(profile.build(PROTO, "GetPlayerProfile_V1"), "GetPlayerProfile_V1")), 34,
   "GetPlayerProfile_V1 empty = 3 + 20 + 4 + 4 + 3 bytes")

cp = body(profile.build(PROTO, "CreatePlayerProfile_V1", msg="E", kind=5,
                        profile={"name": "n", "values": [1], "list_a": [2],
                                 "list_b": [], "tail": "t"},
                        mails={4: True}), "CreatePlayerProfile_V1")
want = (b"\x02\x00E\x00" + struct.pack("<H", 5)
        + b"\x02\x00n\x00" + struct.pack("<IIIII", 1, 0, 0, 0, 0)
        + struct.pack("<II", 1, 2) + struct.pack("<I", 0) + b"\x02\x00t\x00"
        + struct.pack("<I", 1) + struct.pack("<I", 4) + b"\x01")
eq(cp, want, "CreatePlayerProfile_V1 = Result + PlayerProfile + map<u32,u8>")
eq(body(profile.build(PROTO, "CreatePlayerProfile_V1", msg="", kind=0,
                      profile=None), "CreatePlayerProfile_V1"),
   b"\x01\x00\x00" + b"\x00\x00" + b"\x01\x00\x00" + b"\x00" * 20
   + b"\x00" * 8 + b"\x01\x00\x00" + b"\x00\x00\x00\x00",
   "CreatePlayerProfile_V1 defaults = empty Result + empty profile + empty map")
# flat keyword spelling agrees with the dict spelling
eq(body(profile.build(PROTO, "CreatePlayerProfile_V1", msg="E", kind=5, name="n",
                      values=[1], list_a=[2], tail="t", mails=[(4, True)]),
        "CreatePlayerProfile_V1"),
   want, "CreatePlayerProfile_V1 flat kwargs == profile dict")

# helper-level shapes
eq(profile.u32_list([1, 2]), struct.pack("<III", 2, 1, 2), "u32_list = count + N x u32")
eq(profile.u32_list(b"\x01\x00\x00\x00\x02\x00\x00\x00"),
   struct.pack("<III", 2, 1, 2), "u32_list accepts a byte blob")
eq(profile.result_value("x", 7), b"\x02\x00x\x00" + struct.pack("<H", 7),
   "result_value = String + u16")
eq(profile.qstr_field(None), b"\x01\x00\x00", "qstr_field(None) = empty String")
eq(profile.account_info(), b"\x01\x00\x00" * 2, "account_info empty")
eq(profile.shop_items(), b"\x00" * 8, "shop_items empty")
eq(profile.unique_key(), b"\x00\x00\x00\x00", "unique_key empty")
eq(profile.facebook_account(), b"\x00" * 8 + b"\x01\x00\x00",
   "facebook_account empty")
eq(profile.asset_counter_graph(), b"\x00" * 8, "asset_counter_graph empty")
eq(profile.reputation_points(), b"\x00\x00\x00\x00", "reputation_points empty")
eq(profile.world_stats(), b"\x00" * 8, "world_stats empty")
eq(profile.player_profile(), b"\x01\x00\x00" + b"\x00" * 20 + b"\x00" * 8
   + b"\x01\x00\x00", "player_profile empty (0x60-byte object)")
eq(profile.leaderboard_templates(), b"\x00\x00\x00\x00", "leaderboard_templates empty")
eq(profile.create_player_profile(), b"\x01\x00\x00\x00\x00" + b"\x01\x00\x00"
   + b"\x00" * 20 + b"\x00" * 8 + b"\x01\x00\x00" + b"\x00\x00\x00\x00",
   "create_player_profile empty")

# ---------------------------------------------------------------------------
# 5. totality on junk input
# ---------------------------------------------------------------------------
pub = sorted(n for n in dir(profile) if n.startswith("m_"))
check(len(pub) >= 54, "public m_* builders present (%d)" % len(pub))
for fn_name in pub:
    fn = getattr(profile, fn_name)
    for junk in JUNK:
        try:
            out = fn(junk)
        except Exception:
            check(False, "%s(%r) raised\n%s" % (fn_name, junk, traceback.format_exc()))
            continue
        check(isinstance(out, bytes), "%s(%r) returns bytes" % (fn_name, junk))
    try:
        out = fn()
    except Exception:
        check(False, "%s() raised" % fn_name)
    else:
        check(isinstance(out, bytes), "%s() returns bytes" % fn_name)
    # keyword junk too
    for kw in ({"items": object()}, {"value": object()}, {"pairs": object()},
               {"settings": object()}, {"bindings": object()}, {"mails": object()},
               {"status": object()}, {"exists": object()}, {"missing": object()},
               {"junk_kw": object()}):
        try:
            out = fn(**kw)
        except Exception:
            check(False, "%s(**%r) raised\n%s" % (fn_name, kw, traceback.format_exc()))
        else:
            check(isinstance(out, bytes), "%s(**%r) returns bytes" % (fn_name, kw))

# module-level helpers
for fn_name in ("ark_item", "ark_items", "get_ark_items", "set_ark_items", "set_ark",
                "achievements", "stats_entries", "settings_map", "keybindings_map",
                "infomails_map", "canonical",
                "u32_list", "qstr_field", "result_value", "leaderboard_templates",
                "player_profile", "world_stats", "reputation_points",
                "facebook_account", "unique_key", "shop_items", "account_info",
                "asset_counter_graph", "create_player_profile"):
    fn = getattr(profile, fn_name)
    for junk in JUNK:
        try:
            fn(junk)
        except TypeError:
            # single-arg helper that needs more args (none here) -- still total
            check(True, "%s(%r) arity" % (fn_name, junk))
        except Exception:
            check(False, "%s(%r) raised\n%s" % (fn_name, junk, traceback.format_exc()))
        else:
            check(True, "%s(%r) total" % (fn_name, junk))

# ark list codec is total and always well-formed
for junk in JUNK:
    try:
        blob = profile.ark_items(junk)
    except Exception:
        check(False, "ark_items(%r) raised" % (junk,))
        continue
    check(isinstance(blob, bytes) and len(blob) >= 4, "ark_items(%r) yields a count" % (junk,))
    if isinstance(blob, bytes) and len(blob) >= 4:
        n = struct.unpack_from("<I", blob, 0)[0]
        check(len(blob) == 4 + 16 * n,
              "ark_items(%r) is count + 16*n bytes" % (junk,))

# build() totality: unknown names, junk methods, junk kwargs
for method in (None, 0, object(), "", "Nope_V1", "GetArkItems_V1X", b"\xff", [], {}):
    try:
        out = profile.build(PROTO, method)
    except Exception:
        check(False, "build(%r) raised" % (method,))
    else:
        check(out is None or isinstance(out, bytes), "build(%r) total" % (method,))
eq(profile.build(PROTO, "Nope_V1"), None, "build() -> None for unknown method")
for name in profile.METHOD_NAMES:
    for junk in JUNK:
        try:
            out = profile.build(PROTO, name, items=junk, value=junk, status=junk,
                                pairs=junk, settings=junk, bindings=junk, mails=junk,
                                exists=junk, missing=junk, junk=junk)
        except Exception:
            check(False, "build(%r, junk) raised\n%s" % (name, traceback.format_exc()))
            continue
        check(isinstance(out, bytes) and out.startswith(nb(name)),
              "build(%r, junk) still has an exact name block" % name)
        # a junk payload must still be structurally valid for its layout
        b = body(out, name)
        if profile.METHOD_NAMES[name].startswith("u32 count"):
            n = struct.unpack_from("<I", b, 0)[0]
            check(n < 1000, "build(%r, junk) count is sane (%d)" % (name, n))

# helpers from rv_shapes still behave (sanity: we did not touch them)
eq(name_block(PROTO, "SetArk_V1")[:2], struct.pack("<H", len(nb("SetArk_V1")) - 2),
   "name_block u16 length includes NUL")
eq(u32(1), b"\x01\x00\x00\x00", "u32 is little-endian")
eq(u64(1), b"\x01" + b"\x00" * 7, "u64 is little-endian")
eq(qstr("ab"), b"\x03\x00ab\x00", "qstr includes the NUL (engine convention)")
eq(u8(255), b"\xff", "u8")
eq(u16(1), b"\x01\x00", "u16 is little-endian")

# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
print()
print("checks run: %d" % CHECKS[0])
print("methods implemented: %d   unknown: %d   total in reader: %d"
      % (len(profile.METHOD_NAMES), len(profile.UNKNOWN_METHODS),
         len(reader_methods)))
print("void replies: %d   typed replies: %d"
      % (sum(1 for v in profile.METHOD_NAMES.values() if v == "void"),
         sum(1 for v in profile.METHOD_NAMES.values() if v != "void")))
if FAILED:
    print("FAILED: %d check(s)" % len(FAILED))
    for f in FAILED[:20]:
        print(" -", f.splitlines()[0])
    sys.exit(1)
print("OK")
sys.exit(0)
