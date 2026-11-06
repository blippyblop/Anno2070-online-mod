"""Wire-shape builders for ``PlayerProfileServiceProtocol`` replies.

One pure function per method of the RendezVous player-profile service.  Every
builder returns the reply *payload* only -- i.e. the method name block

    u16 len (INCLUDING the trailing NUL) | "PlayerProfileServiceProtocol::<M>*" | 00

followed by the raw little-endian fields decoded from the client's reply reader
``decomp/__AVPlayerProfileServiceProtocolClient_rdv__.c`` (dispatch:
``FUN_140df0640``) and the per-type readers in ``decomp/ddl_ser.c``.  The
envelope (size | proto name | type | status | call id) is added by the caller
(``mimic_server.rv_rmc_reply``) and is *not* produced here.

Reply layout of every method this protocol declares (all 54 names found in the
reader's ``_mbscmp`` chain, in reader order)::

    #   method                          reply payload
    1   SetStatus_V1                    <void>
    2   CreatePlayerProfile_V1          Result(String+u16) + PlayerProfile +
                                        map<u32,u8>   FUN_141371440 / FUN_140df1d50 /
                                                      FUN_140df2cc0
    3   GetPlayerProfile_V1             PlayerProfile       FUN_140df1d50
    4   DoesUserExist_V1                u8 bool            FUN_14135e2a0
    5   SetPortrait_V1                  <void>
    6   SetIcon_V1                      <void>
    7   SetColor_V1                     <void>
    8   SetPlayerTitle_V1               <void>
    9   SetArk_V1                       <void>
    10  AddActivation_V1                <void>
    11  GetAchievements_V1              u32 count + N x Achievement{FUN_140df2b30}
    12  AddAchievement_V1               8 raw bytes (u64)   FUN_14135e5f0(...,8)
    13  GetSettings_V1                  map<u32,u32>        FUN_140df2960
    14  SetSetting_V1                   <void>
    15  GetKeyBindings_V1               map<u32, list<u32>> FUN_140df2710
    16  SetKeyBinding_V1                <void>
    17  ResetKeyBindings_V1             <void>
    18  AddHelperQuest_V1               <void>
    19  ResetHelperQuests_V1            <void>
    20  GetInfoMails_V1                 map<u32, bool>      FUN_140df2cc0
    21  AddInfoMail_V1                  <void>
    22  SetInfoMailRead_V1              <void>
    23  GetStats_V1                     u32 count + N x StatsEntry{FUN_140df2e90}
    24  SetStats_V1                     <void>
    25  GetArkItems_V1                  u32 count + N x ArkItem (16 B)  FUN_140df2490
    26  SetArkItems_V1                  <void>
    27  GetStatus_V1                    u32                 FUN_14135e5f0(...,4)
    28  GetWorldStats_V1                u32 + list<StatsEntry>  FUN_140de9e90
    29  ResetInfoMails_V1               <void>
    30  UploadWorldStatsStencils_V1     <void>
    31  GetReputationPoints_V1          map<u32,u32>        FUN_140de9f50 -> FUN_140de9460
    32  AreReputationPointsMissing_V1   u8 bool             shared FUN_14135e2a0 block
    33  SetReputationPoints_V1          <void>
    34  SolveDailyQuest_V1              <void>
    35  UploadFactionRewards_V1         <void>
    36  UploadStatsReputation_V1        <void>
    37  GetFacebookAccount_V1           u64 + String        FUN_140de9fe0
    38  SetFacebookAccount_V1           <void>
    39  InvalidateAllReputationPoints_V1 <void>
    40  UploadReputationMails_V1        <void>
    41  ActivateUniqueKey_V1            list<u32>           FUN_140dea0a0 -> FUN_140df22e0
    42  InitializeService_V1            <void>
    43  AddShopItem_V1                  <void>
    44  RemoveShopItem_V1               <void>
    45  ListShopItems_V1                u32 + list<u32>     FUN_140dea130
    46  UploadShopItems_V1              <void>
    47  UploadSteamAppIdMapping_V1      <void>
    48  UpdateUPCActivations_V1         <void>
    49  UpdateSteamActivations_V1       <void>
    50  GetAccountInfo_V1               String + String     FUN_140dea1f0
    51  UploadAchievementRewards_V1     <void>
    52  FetchAssetCounterGraph_V1       map<u32,u32> + u32  FUN_140dea2a0
    53  UploadLeaderboardTemplates_V1   <void>
    54  GetLeaderboardTemplates_V1      list<LeaderboardTemplate>  FUN_140dea360
                                        -> FUN_140df1e10

"<void>" means the dispatcher branches straight to ``LAB_140df1d25`` with no
reads at all, so the reply payload stops after the name block.

Element layouts actually decoded (all LITTLE-endian, no padding, no tags)::

    ArkItem      u32 f0 | u32 f1 | u32 f2 | u32 f3               16 B
                 (reader FUN_140df2490 reads four independent u32 into object
                 +8/+0xc/+0x10/+0x14; serializer FUN_140df2620 mirrors it.
                 NOT two u64 -- see decomp/batch/step3_ark/ARK.md section 2.)
    Achievement  u32 id | u64 value                              12 B
    StatsEntry   u32 | u32 | u32 | u64 | u64 | u64               36 B
    LeaderboardTemplate
                 u32 | u32 | u64 | u64 | u32 | u32 n | n x u8     32 + n B
                 (reader FUN_140df1e10 reads object +8,+0xc (u32), +0x10,+0x18
                 (u64), +0x20 (u32), then a u32 count at +0x28 and that many
                 single bytes into a std::vector<u8>)
    PlayerProfile
                 String | u32 x5 | list<u32> | list<u32> | String   0x60 B obj
                 (reader FUN_140df1d50: String->+8, five FUN_14135e5f0(...,4)
                 ->+0x10..+0x20, FUN_140e09980->+0x28 and ->+0x40 (each a
                 std::list<u32>: u32 count + count x u32, see FUN_140e09980 in
                 decomp/batch/step4_readers/worldevent.c:374), String->+0x58.
                 Ctor FUN_140da8640 confirms the 0x60-byte layout.)
    Result       String msg | u16 kind                            variable
                 (FUN_141371440 = FUN_141371620 + u16, then
                 FUN_1411995b0(msg, kind) -- the same Quazal Result the envelope
                 carries; msg="" + kind=0 is the clean value)
    map<K,V>     u32 count | count x (K, V)                      no terminator
    list<T>      u32 count | count x T                           no terminator

``String`` in a layout is the Quazal String (u16 length EXCLUDING the NUL, then
the bytes, ``FUN_141371620``).  ``list<u32>`` is the shape both container
readers produce: FUN_140df22e0 (``std::vector<u32>``, capacity check ``>> 2``)
and FUN_140e09980 (``std::list<u32>``, 4-byte payload per node) -- identical on
the wire, so one helper (:func:`u32_list`) serves both.

Guessed, not proven: the field *meanings* of ArkItem/Achievement/StatsEntry/
LeaderboardTemplate/PlayerProfile.  Only widths and order are visible in the
dump -- see ARK.md section 2 -- so the names used here (``f0..f3``, ``id``,
``value``, ``a..e``) are placeholders.

Rules honoured by every builder:

  * the name block comes from :func:`rv_shapes.name_block` (u16 length that
    INCLUDES the NUL, and the trailing ``*`` the client compares against);
  * integers are little-endian and unpadded (the client ``memcpy``s them);
  * a bool is a single byte, non-zero == true (``FUN_14135e2a0`` writes
    ``*dst = raw != 0``);
  * every function is *total*: no I/O, no module state mutated, no imports from
    ``mimic_server``, and no exceptions -- junk arguments are coerced to their
    documented default because a defaulted reply is far better than a crash.

Every one of the 54 methods now has a decoded reader, so
:data:`UNKNOWN_METHODS` is empty.  The per-method evidence lives in
``decomp/batch/step4_readers/PROFILE-UNKNOWN.md`` (method -> reader table with
the addresses quoted above each builder).
"""
from __future__ import annotations

import struct

try:  # normal path: ``import rv_shapes.profile``
    from . import name_block, qstr, u16, u32, u64, u8
except ImportError:  # pragma: no cover - direct load with server/ on sys.path
    from rv_shapes import name_block, qstr, u16, u32, u64, u8

PROTO = "PlayerProfileServiceProtocol"

#: Every method name found in the client reader, in reader order.
ALL_METHODS = (
    "SetStatus_V1",
    "CreatePlayerProfile_V1",
    "GetPlayerProfile_V1",
    "DoesUserExist_V1",
    "SetPortrait_V1",
    "SetIcon_V1",
    "SetColor_V1",
    "SetPlayerTitle_V1",
    "SetArk_V1",
    "AddActivation_V1",
    "GetAchievements_V1",
    "AddAchievement_V1",
    "GetSettings_V1",
    "SetSetting_V1",
    "GetKeyBindings_V1",
    "SetKeyBinding_V1",
    "ResetKeyBindings_V1",
    "AddHelperQuest_V1",
    "ResetHelperQuests_V1",
    "GetInfoMails_V1",
    "AddInfoMail_V1",
    "SetInfoMailRead_V1",
    "GetStats_V1",
    "SetStats_V1",
    "GetArkItems_V1",
    "SetArkItems_V1",
    "GetStatus_V1",
    "GetWorldStats_V1",
    "ResetInfoMails_V1",
    "UploadWorldStatsStencils_V1",
    "GetReputationPoints_V1",
    "AreReputationPointsMissing_V1",
    "SetReputationPoints_V1",
    "SolveDailyQuest_V1",
    "UploadFactionRewards_V1",
    "UploadStatsReputation_V1",
    "GetFacebookAccount_V1",
    "SetFacebookAccount_V1",
    "InvalidateAllReputationPoints_V1",
    "UploadReputationMails_V1",
    "ActivateUniqueKey_V1",
    "InitializeService_V1",
    "AddShopItem_V1",
    "RemoveShopItem_V1",
    "ListShopItems_V1",
    "UploadShopItems_V1",
    "UploadSteamAppIdMapping_V1",
    "UpdateUPCActivations_V1",
    "UpdateSteamActivations_V1",
    "GetAccountInfo_V1",
    "UploadAchievementRewards_V1",
    "FetchAssetCounterGraph_V1",
    "UploadLeaderboardTemplates_V1",
    "GetLeaderboardTemplates_V1",
)

# --------------------------------------------------------------------------
# total (never-raising) coercion helpers
# --------------------------------------------------------------------------
_M32 = 0xFFFFFFFF
_M64 = 0xFFFFFFFFFFFFFFFF


def _as_u32(v):
    """Best-effort unsigned 32-bit value; junk becomes 0."""
    try:
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, int):
            return v & _M32
        if isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                return 0
            return int(v) & _M32
        if isinstance(v, (bytes, bytearray, memoryview)):
            b = bytes(v)[:4]
            return int.from_bytes(b + b"\x00" * (4 - len(b)), "little")
        if isinstance(v, str):
            try:
                return int(v.strip(), 0) & _M32
            except Exception:
                b = v.encode("utf-8", "replace")[:4]
                return int.from_bytes(b + b"\x00" * (4 - len(b)), "little")
    except Exception:
        return 0
    return 0


def _as_u64(v):
    """Best-effort unsigned 64-bit value; junk becomes 0."""
    try:
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, int):
            return v & _M64
        if isinstance(v, float):
            if v != v or v in (float("inf"), float("-inf")):
                return 0
            return int(v) & _M64
        if isinstance(v, (bytes, bytearray, memoryview)):
            b = bytes(v)[:8]
            return int.from_bytes(b + b"\x00" * (8 - len(b)), "little")
        if isinstance(v, str):
            try:
                return int(v.strip(), 0) & _M64
            except Exception:
                b = v.encode("utf-8", "replace")[:8]
                return int.from_bytes(b + b"\x00" * (8 - len(b)), "little")
    except Exception:
        return 0
    return 0


def _as_bool_byte(v):
    """One-byte Quazal bool (FUN_14135e2a0: ``*dst = raw != 0``)."""
    try:
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, (int, float)):
            return 0 if (v != v or v == 0) else 1
        if isinstance(v, (bytes, bytearray, memoryview)):
            return 1 if any(bytes(v)) else 0
        if isinstance(v, str):
            return 0 if v.strip().lower() in ("", "0", "false", "no", "none") else 1
        return 1 if v is not None else 0
    except Exception:
        return 0


def _seq(v):
    """Sequence view of a value; scalars become one-element lists.

    Dicts are NOT expanded here -- record builders treat a dict as one record
    and map builders expand it themselves.
    """
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    if isinstance(v, (bytes, bytearray, memoryview)):
        return [bytes(v)]
    if isinstance(v, str):
        return []
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    if hasattr(v, "__iter__"):
        try:
            return list(v)
        except Exception:
            return []
    return [v]


def _pairs(v):
    """Sequence of (key, value) pairs from a dict or an iterable of pairs."""
    if v is None:
        return []
    if isinstance(v, dict):
        return list(v.items())
    out = []
    for el in _seq(v):
        out.append(_pair(el))
    return out


def _pair(el):
    """Coerce one element to (key, value)."""
    try:
        if isinstance(el, dict):
            if "key" in el or "value" in el:
                return el.get("key", el.get("k", 0)), el.get("value", el.get("v", 0))
            vals = list(el.values())
            if vals:
                return vals[0], vals[1] if len(vals) > 1 else 0
            return 0, 0
        if isinstance(el, (bytes, bytearray, memoryview)):
            b = bytes(el)
            if len(b) >= 8:
                return int.from_bytes(b[:4], "little"), int.from_bytes(b[4:8], "little")
            return b, 0
        if isinstance(el, (list, tuple)):
            k = el[0] if len(el) > 0 else 0
            v = el[1] if len(el) > 1 else 0
            return k, v
        return el, 0
    except Exception:
        return 0, 0


def _u32_seq(v):
    """Sequence of u32 values; bytes are split into 4-byte words."""
    if v is None:
        return []
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return [
            int.from_bytes(b[i:i + 4].ljust(4, b"\x00"), "little")
            for i in range(0, len(b), 4)
        ]
    if isinstance(v, (int, float)):
        return [v]
    if isinstance(v, str):
        return []
    if isinstance(v, dict):
        return list(v.values())
    return _seq(v)


# --------------------------------------------------------------------------
# ARK item box (headline feature)
# --------------------------------------------------------------------------
def ark_item(a=0, b=0, c=0, d=0):
    """One 16-byte ``ArkItem``: four independent u32 LE fields.

    Reader: ``FUN_140df2490`` (ddl_ser.c:1742) -- four ``FUN_14135e5f0(...,4)``
    calls, no nesting, no strings.  Serializer: ``FUN_140df2620``.
    """
    return u32(_as_u32(a)) + u32(_as_u32(b)) + u32(_as_u32(c)) + u32(_as_u32(d))


def _ark_record(el):
    """Coerce one ArkItem-ish value to exactly 16 bytes."""
    try:
        if isinstance(el, (bytes, bytearray, memoryview)):
            raw = bytes(el)[:16]
            return raw + b"\x00" * (16 - len(raw))
        if isinstance(el, dict):
            for names in (("f0", "f1", "f2", "f3"), ("a", "b", "c", "d")):
                if any(n in el for n in names):
                    return b"".join(u32(_as_u32(el.get(n, 0))) for n in names)
            vals = list(el.values())[:4]
            vals += [0] * (4 - len(vals))
            return b"".join(u32(_as_u32(x)) for x in vals)
        if isinstance(el, (list, tuple)):
            fields = [_as_u32(x) for x in list(el)[:4]]
            fields += [0] * (4 - len(fields))
            return b"".join(u32(x) for x in fields)
    except Exception:
        return b"\x00" * 16
    return ark_item(el)


def _ark_records(v):
    """Sequence of ArkItems; raw bytes are chunked into 16-byte records."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        n = len(b) // 16
        return [b[i * 16:(i + 1) * 16] for i in range(n)]
    return _seq(v)


def ark_items(items=()):
    """``list<ArkItem>``: u32 count + count x 16 bytes (no tags, no NUL).

    Accepts ints (-> f0), 4-tuples/lists, dicts (``a..d`` / ``f0..f3``), raw
    16-byte blobs, or a whole 16-byte-aligned blob of records.
    """
    seq = _ark_records(items)
    return u32(len(seq)) + b"".join(_ark_record(it) for it in seq)


def get_ark_items(items=()):
    """Reply to ``GetArkItems_V1`` (request is empty): name block + list."""
    return name_block(PROTO, "GetArkItems_V1") + ark_items(items)


def set_ark_items(items=()):
    """Reply to ``SetArkItems_V1``: empty payload (the list is in the request).

    ``items`` is accepted and ignored -- the client reads no result fields
    (dispatcher jumps straight to ``LAB_140df1d25``).
    """
    return name_block(PROTO, "SetArkItems_V1")


def set_ark(*_args, **_kwargs):
    """Reply to ``SetArk_V1``: empty payload (request layout not in the dump)."""
    return name_block(PROTO, "SetArk_V1")


# --------------------------------------------------------------------------
# other decoded record / map bodies
# --------------------------------------------------------------------------
def achievements(items=()):
    """``list<Achievement>``: u32 count + count x (u32 id, u64 value) = 12 B."""
    seq = _seq(items)
    out = [u32(len(seq))]
    for el in seq:
        try:
            if isinstance(el, (bytes, bytearray, memoryview)) and len(el) >= 12:
                b = bytes(el)
                out.append(u32(int.from_bytes(b[:4], "little")))
                out.append(u64(int.from_bytes(b[4:12], "little")))
                continue
            k, v = _pair(el)
        except Exception:
            k, v = 0, 0
        out.append(u32(_as_u32(k)))
        out.append(u64(_as_u64(v)))
    return b"".join(out)


def stats_entries(items=()):
    """``list<StatsEntry>``: 36 B each -- u32,u32,u32,u64,u64,u64."""
    seq = _seq(items)
    out = [u32(len(seq))]
    for el in seq:
        try:
            if isinstance(el, (bytes, bytearray, memoryview)) and len(el) >= 36:
                out.append(bytes(el)[:36])
                continue
            if isinstance(el, dict):
                vals = list(el.values())
            elif isinstance(el, (list, tuple)):
                vals = list(el)
            else:
                vals = [el]
            vals = list(vals)[:6] + [0] * max(0, 6 - len(vals))
            out.append(
                u32(_as_u32(vals[0]))
                + u32(_as_u32(vals[1]))
                + u32(_as_u32(vals[2]))
                + u64(_as_u64(vals[3]))
                + u64(_as_u64(vals[4]))
                + u64(_as_u64(vals[5]))
            )
        except Exception:
            out.append(b"\x00" * 36)
    return b"".join(out)


def settings_map(pairs=()):
    """``map<u32,u32>``: u32 count + count x (u32 key, u32 value)."""
    seq = _pairs(pairs)
    return u32(len(seq)) + b"".join(
        u32(_as_u32(k)) + u32(_as_u32(v)) for k, v in seq
    )


def keybindings_map(pairs=()):
    """``map<u32, list<u32>>``: count + count x (u32 key, u32 n, n x u32)."""
    seq = _pairs(pairs)
    out = [u32(len(seq))]
    for k, v in seq:
        vals = _u32_seq(v)
        out.append(u32(_as_u32(k)))
        out.append(u32(len(vals)))
        out.extend(u32(_as_u32(x)) for x in vals)
    return b"".join(out)


def infomails_map(pairs=()):
    """``map<u32, bool>``: u32 count + count x (u32 key, u8 bool)."""
    seq = _pairs(pairs)
    return u32(len(seq)) + b"".join(
        u32(_as_u32(k)) + u8(_as_bool_byte(v)) for k, v in seq
    )


# --------------------------------------------------------------------------
# Quazal String field and the remaining containers
# --------------------------------------------------------------------------
_STR_MAX = 0xFFFF  # a Quazal String length is a u16 (FUN_14135e680/620)


def _text(v, default=""):
    """Best-effort byte string for a Quazal String field; never raises."""
    try:
        if isinstance(v, str):
            return v.encode("ascii", "replace")
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v)
        if isinstance(v, bool):
            return b"1" if v else b"0"
        if isinstance(v, (int, float)):
            return str(v).encode("ascii", "replace")
    except Exception:
        pass
    return default.encode("ascii", "replace") if isinstance(default, str) else b""


def qstr_field(text=""):
    """Quazal String field: u16 length EXCLUDING NUL + bytes.

    Reader ``FUN_141371620`` (storm_api.c:38775).  The length is clipped to a
    u16 so a pathological input can never produce an unwritable header.
    """
    return qstr(_text(text)[:_STR_MAX])


def u32_list(values=()):
    """``list<u32>``: u32 count + count x u32, no terminator.

    Two identical wire shapes are produced by two different readers:
    ``FUN_140df22e0`` (std::vector<u32>: capacity check ``>> 2``, 4-byte
    elements) and ``FUN_140e09980`` (std::list<u32>: 4-byte value per 0x18-byte
    node).  Accepts ints, bytes (chunked into 4-byte words) or any iterable.
    """
    vals = _u32_seq(values)
    return u32(len(vals)) + b"".join(u32(_as_u32(v)) for v in vals)


def result_value(msg="", kind=0):
    """Embedded Quazal ``Result``: String msg + u16 kind.

    Reader ``FUN_141371440`` (storm_api.c:38703): ``FUN_141371620`` then a
    2-byte read, assembled by ``FUN_1411995b0(msg, kind)``.  ``msg=""`` with
    ``kind=0`` is the "clean" value (see decomp/_adhoc/rmcsuccess.md).
    """
    return qstr_field(msg) + u16(_as_u32(kind))


def _lb_template(el):
    """Coerce one ``LeaderboardTemplate`` to its wire record (32 + n bytes).

    Reader ``FUN_140df1e10`` (ddl_ser.c:1493): per element a u32, a u32, a u64,
    a u64, a u32, then a u32 count at object +0x28 followed by that many single
    bytes (std::vector<u8>).
    """
    a = b = c = d = e = 0
    data = b""
    try:
        if isinstance(el, (bytes, bytearray, memoryview)):
            raw = bytes(el)
            if len(raw) >= 32:
                fa, fb, fc, fd, fe, n = struct.unpack_from("<IIQQII", raw, 0)
                a, b, c, d, e = fa, fb, fc, fd, fe
                data = raw[32:32 + n]
            else:
                data = raw
        elif isinstance(el, dict):
            a = el.get("a", el.get("f0", 0))
            b = el.get("b", el.get("f1", 0))
            c = el.get("c", el.get("f2", 0))
            d = el.get("d", el.get("f3", 0))
            e = el.get("e", el.get("f4", 0))
            data = el.get("data", el.get("blob", b""))
        else:
            seq = list(el) if isinstance(el, (list, tuple)) else []
            seq = list(seq) + [None] * (6 - len(seq))
            a, b, c, d, e, data = seq[:6]
    except Exception:
        a = b = c = d = e = 0
        data = b""
    blob = _text(data)
    return (
        u32(_as_u32(a))
        + u32(_as_u32(b))
        + u64(_as_u64(c))
        + u64(_as_u64(d))
        + u32(_as_u32(e))
        + u32(len(blob))
        + blob
    )


def leaderboard_templates(items=()):
    """``list<LeaderboardTemplate>``: u32 count + count x (32 + n bytes)."""
    seq = _seq(items)
    if isinstance(items, (bytes, bytearray, memoryview)):
        seq = []  # a bare blob is not length-delimited; emit an empty list
    return u32(len(seq)) + b"".join(_lb_template(it) for it in seq)


def _profile_parts(name="", values=(), list_a=(), list_b=(), tail=""):
    """(name bytes, 5 x u32, list_a, list_b, tail bytes) for a PlayerProfile."""
    try:
        vals = [_as_u32(v) for v in _u32_seq(values)][:5]
    except Exception:
        vals = []
    vals += [0] * (5 - len(vals))
    return _text(name), vals, _u32_seq(list_a), _u32_seq(list_b), _text(tail)


def player_profile(name="", values=(), list_a=(), list_b=(), tail=""):
    """One ``PlayerProfile`` object body (0x60 bytes of payload).

    Reader ``FUN_140df1d50`` (ddl_ser.c:1473), in order::

        +8    String                  FUN_141371620
        +0x10 u32                     FUN_14135e5f0(...,4)
        +0x14 u32                     ...
        +0x18 u32
        +0x1c u32
        +0x20 u32
        +0x28 list<u32>               FUN_140e09980 (u32 count + count x u32)
        +0x40 list<u32>               FUN_140e09980
        +0x58 String                  FUN_141371620

    The 0x60-byte object layout is confirmed by the constructor
    ``FUN_140da8640`` (.?AV_DDL_PlayerProfile@rdv@@), which zeroes exactly those
    members.  ``values`` holds the five u32 (shorter/longer input is padded /
    clipped).
    """
    nm, vals, a, b, tl = _profile_parts(name, values, list_a, list_b, tail)
    return (
        qstr_field(nm)
        + b"".join(u32(v) for v in vals)
        + u32_list(a)
        + u32_list(b)
        + qstr_field(tl)
    )


def world_stats(version=0, entries=()):
    """``GetWorldStats_V1`` body: u32 + ``list<StatsEntry>``.

    Reader ``FUN_140de9e90`` (profile_readers.c:556): init a 0x30-byte
    StatsEntry vector, then ``FUN_14135e5f0(...,4)`` into a *separate* stack
    slot, then ``FUN_140df2e90`` (the 36-byte StatsEntry list reader).
    """
    return u32(_as_u32(version)) + stats_entries(entries)


def reputation_points(pairs=()):
    """``GetReputationPoints_V1`` body: ``map<u32,u32>``.

    Reader ``FUN_140de9f50`` (profile_readers.c:580) -> ``FUN_140de9460``
    (profile_readers.c:64): u32 count, then per entry a u32 key (stored at tree
    node +0x1c) and a u32 value (node +0x20) -- the same ``map<u32,u32>`` shape
    as GetSettings, but read with ``FUN_14135e5f0(...,4,1)`` (the "required"
    variant of the count read).
    """
    return settings_map(pairs)


def facebook_account(account_id=0, name=""):
    """``GetFacebookAccount_V1`` body: 8 raw bytes (u64) + String.

    Reader ``FUN_140de9fe0`` (profile_readers.c:600):
    ``FUN_14135e5f0(stream, dst, 8)`` then ``FUN_141371620`` (String).  The u64
    is an opaque 8-byte account id -- no width substructure is visible.
    """
    return u64(_as_u64(account_id)) + qstr_field(name)


def unique_key(keys=()):
    """``ActivateUniqueKey_V1`` body: ``list<u32>``.

    Reader ``FUN_140dea0a0`` (profile_readers.c:626) -> ``FUN_140df22e0``
    (ddl_ser.c:1678): clear the vector, read a u32 count, then count x u32 into
    4-byte elements (``param_2[2] - *param_2 >> 2`` capacity check).  No extra
    scalar precedes it.
    """
    return u32_list(keys)


def shop_items(status=0, items=()):
    """``ListShopItems_V1`` body: u32 + ``list<u32>``.

    Reader ``FUN_140dea130`` (profile_readers.c:647):
    ``FUN_14135e5f0(stream, dst, 4)`` then ``FUN_140df22e0`` (list<u32>).  Both
    reads are unconditional (unlike ActivateUniqueKey, which is one read).
    """
    return u32(_as_u32(status)) + u32_list(items)


def account_info(name="", account=""):
    """``GetAccountInfo_V1`` body: String + String.

    Reader ``FUN_140dea1f0`` (profile_readers.c:675): two ``FUN_141371620``
    calls writing object +8 and +0x10 of a 0x18-byte object (ctor
    ``FUN_140da8910``).  No integers are read.
    """
    return qstr_field(name) + qstr_field(account)


def asset_counter_graph(pairs=(), suffix=0):
    """``FetchAssetCounterGraph_V1`` body: ``map<u32,u32>`` + u32.

    Reader ``FUN_140dea2a0`` (profile_readers.c:700): first ``FUN_140df2960``
    (the map<u32,u32> reader shared with GetSettings_V1: u32 count + count x
    (u32 key @node+0x1c, u32 value @node+0x20)), then an unconditional
    ``FUN_14135e5f0(...,4)`` -- so the trailing u32 is always on the wire, even
    when the map is empty.
    """
    return settings_map(pairs) + u32(_as_u32(suffix))


def create_player_profile(msg="", kind=0, profile=None, mails=(),
                          name="", values=(), list_a=(), list_b=(), tail=""):
    """``CreatePlayerProfile_V1`` body: Result + PlayerProfile + map<u32,u8>.

    Dispatcher ``FUN_140df0640`` (decomp/__AVPlayerProfileServiceProtocolClient_rdv__.c:44-71)
    reads three result args in this order:

        0  FUN_141371440  Result         = String msg + u16 kind
        1  FUN_140df1d50  PlayerProfile  = see :func:`player_profile` (0x60 obj)
        2  FUN_140df2cc0  map<u32,bool>  = u32 count + count x (u32, u8)

    ``profile`` may be a dict (``name``/``values``/``list_a``/``list_b``/
    ``tail``) or a 5-tuple in that order; when it is neither, the flat keyword
    arguments are used.  The map is the same reader GetInfoMails_V1 uses.
    """
    if isinstance(profile, dict):
        name = profile.get("name", profile.get("f0", ""))
        values = profile.get("values", profile.get("u32s", ()))
        list_a = profile.get("list_a", profile.get("ids_a", ()))
        list_b = profile.get("list_b", profile.get("ids_b", ()))
        tail = profile.get("tail", profile.get("last", ""))
    elif isinstance(profile, (list, tuple)):
        seq = list(profile) + [None] * (5 - len(profile))
        name = seq[0] if seq[0] is not None else ""
        values = seq[1] if seq[1] is not None else ()
        list_a = seq[2] if seq[2] is not None else ()
        list_b = seq[3] if seq[3] is not None else ()
        tail = seq[4] if seq[4] is not None else ""
    return (
        result_value(msg, kind)
        + player_profile(name, values, list_a, list_b, tail)
        + infomails_map(mails)
    )


# --------------------------------------------------------------------------
# method bodies: ``_b_*`` return fields only (used by build())
# --------------------------------------------------------------------------
def _b_void(**_kw):
    return b""


def _b_get_ark_items(items=(), **_kw):
    return ark_items(items)


def _b_set_ark_items(items=(), **_kw):
    return b""


def _b_get_achievements(items=(), **_kw):
    return achievements(items)


def _b_add_achievement(value=0, **_kw):
    # 8 raw bytes: FUN_14135e5f0(param_2, &value, 8) -- one u64 wide field.
    return u64(_as_u64(_kw.get("id", value)))


def _b_get_settings(settings=(), **_kw):
    return settings_map(_kw.get("pairs", settings))


def _b_get_key_bindings(bindings=(), **_kw):
    return keybindings_map(_kw.get("pairs", bindings))


def _b_get_info_mails(mails=(), **_kw):
    return infomails_map(_kw.get("pairs", mails))


def _b_get_stats(items=(), **_kw):
    return stats_entries(_kw.get("stats", items))


def _b_get_status(status=0, **_kw):
    return u32(_as_u32(_kw.get("value", status)))


def _b_does_user_exist(exists=0, **_kw):
    return u8(_as_bool_byte(_kw.get("value", exists)))


def _b_reputation_missing(missing=0, **_kw):
    return u8(_as_bool_byte(_kw.get("value", missing)))


# --- bodies for the 10 methods that were UNKNOWN until 2026-11-05 -----------
def _b_get_world_stats(**_kw):
    return world_stats(_kw.get("version", _kw.get("status", 0)),
                       _kw.get("entries", _kw.get("stats", _kw.get("items", ()))))


def _b_get_reputation_points(**_kw):
    return reputation_points(_kw.get("pairs", _kw.get("points", _kw.get("items", ()))))


def _b_get_facebook_account(**_kw):
    return facebook_account(
        _kw.get("account_id", _kw.get("id", _kw.get("value", 0))),
        _kw.get("name", _kw.get("account", "")),
    )


def _b_activate_unique_key(**_kw):
    return unique_key(_kw.get("keys", _kw.get("items", ())))


def _b_list_shop_items(**_kw):
    return shop_items(_kw.get("status", _kw.get("version", _kw.get("value", 0))),
                      _kw.get("items", ()))


def _b_get_account_info(**_kw):
    return account_info(_kw.get("name", ""),
                        _kw.get("account", _kw.get("account_id", "")))


def _b_fetch_asset_counter_graph(**_kw):
    return asset_counter_graph(
        _kw.get("pairs", _kw.get("counters", _kw.get("items", ()))),
        _kw.get("suffix", _kw.get("value", _kw.get("status", 0))),
    )


def _b_get_leaderboard_templates(**_kw):
    return leaderboard_templates(_kw.get("items", _kw.get("templates", ())))


def _b_get_player_profile(**_kw):
    return player_profile(
        _kw.get("name", ""),
        _kw.get("values", ()),
        _kw.get("list_a", ()),
        _kw.get("list_b", ()),
        _kw.get("tail", ""),
    )


def _b_create_player_profile(**_kw):
    return create_player_profile(
        msg=_kw.get("msg", ""),
        kind=_kw.get("kind", 0),
        profile=_kw.get("profile"),
        mails=_kw.get("mails", _kw.get("pairs", ())),
        name=_kw.get("name", ""),
        values=_kw.get("values", ()),
        list_a=_kw.get("list_a", ()),
        list_b=_kw.get("list_b", ()),
        tail=_kw.get("tail", ""),
    )


# --------------------------------------------------------------------------
# public per-method builders (name block included, PROTO baked in)
# --------------------------------------------------------------------------
def m_setstatus_v1(*_a, **_k):
    return name_block(PROTO, "SetStatus_V1")


def m_createplayerprofile_v1(*_a, **_k):
    return name_block(PROTO, "CreatePlayerProfile_V1") + _b_create_player_profile(**_k)


def m_getplayerprofile_v1(*_a, **_k):
    return name_block(PROTO, "GetPlayerProfile_V1") + _b_get_player_profile(**_k)


def m_doesuserexist_v1(exists=0, **_k):
    return name_block(PROTO, "DoesUserExist_V1") + _b_does_user_exist(exists, **_k)


def m_setportrait_v1(*_a, **_k):
    return name_block(PROTO, "SetPortrait_V1")


def m_seticon_v1(*_a, **_k):
    return name_block(PROTO, "SetIcon_V1")


def m_setcolor_v1(*_a, **_k):
    return name_block(PROTO, "SetColor_V1")


def m_setplayertitle_v1(*_a, **_k):
    return name_block(PROTO, "SetPlayerTitle_V1")


def m_addactivation_v1(*_a, **_k):
    return name_block(PROTO, "AddActivation_V1")


def m_getachievements_v1(items=(), **_k):
    return name_block(PROTO, "GetAchievements_V1") + achievements(items)


def m_addachievement_v1(value=0, **_k):
    return name_block(PROTO, "AddAchievement_V1") + _b_add_achievement(value, **_k)


def m_getsettings_v1(settings=(), **_k):
    return name_block(PROTO, "GetSettings_V1") + _b_get_settings(settings, **_k)


def m_setsetting_v1(*_a, **_k):
    return name_block(PROTO, "SetSetting_V1")


def m_getkeybindings_v1(bindings=(), **_k):
    return name_block(PROTO, "GetKeyBindings_V1") + _b_get_key_bindings(bindings, **_k)


def m_setkeybinding_v1(*_a, **_k):
    return name_block(PROTO, "SetKeyBinding_V1")


def m_resetkeybindings_v1(*_a, **_k):
    return name_block(PROTO, "ResetKeyBindings_V1")


def m_addhelperquest_v1(*_a, **_k):
    return name_block(PROTO, "AddHelperQuest_V1")


def m_resethelperquests_v1(*_a, **_k):
    return name_block(PROTO, "ResetHelperQuests_V1")


def m_getinfomails_v1(mails=(), **_k):
    return name_block(PROTO, "GetInfoMails_V1") + _b_get_info_mails(mails, **_k)


def m_addinfomail_v1(*_a, **_k):
    return name_block(PROTO, "AddInfoMail_V1")


def m_setinfomailread_v1(*_a, **_k):
    return name_block(PROTO, "SetInfoMailRead_V1")


def m_getstats_v1(items=(), **_k):
    return name_block(PROTO, "GetStats_V1") + _b_get_stats(items, **_k)


def m_setstats_v1(*_a, **_k):
    return name_block(PROTO, "SetStats_V1")


def m_getarkitems_v1(items=(), **_k):
    return get_ark_items(items)


def m_setarkitems_v1(items=(), **_k):
    return set_ark_items(items)


def m_setark_v1(*_a, **_k):
    return set_ark()


def m_getstatus_v1(status=0, **_k):
    return name_block(PROTO, "GetStatus_V1") + _b_get_status(status, **_k)


def m_getworldstats_v1(*_a, **_k):
    return name_block(PROTO, "GetWorldStats_V1") + _b_get_world_stats(**_k)


def m_resetinfomails_v1(*_a, **_k):
    return name_block(PROTO, "ResetInfoMails_V1")


def m_uploadworldstatsstencils_v1(*_a, **_k):
    return name_block(PROTO, "UploadWorldStatsStencils_V1")


def m_arereputationpointsmissing_v1(missing=0, **_k):
    return name_block(PROTO, "AreReputationPointsMissing_V1") + _b_reputation_missing(
        missing, **_k
    )


def m_setreputationpoints_v1(*_a, **_k):
    return name_block(PROTO, "SetReputationPoints_V1")


def m_getreputationpoints_v1(*_a, **_k):
    return name_block(PROTO, "GetReputationPoints_V1") + _b_get_reputation_points(**_k)


def m_solvedailyquest_v1(*_a, **_k):
    return name_block(PROTO, "SolveDailyQuest_V1")


def m_uploadfactionrewards_v1(*_a, **_k):
    return name_block(PROTO, "UploadFactionRewards_V1")


def m_uploadstatsreputation_v1(*_a, **_k):
    return name_block(PROTO, "UploadStatsReputation_V1")


def m_setfacebookaccount_v1(*_a, **_k):
    return name_block(PROTO, "SetFacebookAccount_V1")


def m_invalidateallreputationpoints_v1(*_a, **_k):
    return name_block(PROTO, "InvalidateAllReputationPoints_V1")


def m_uploadreputationmails_v1(*_a, **_k):
    return name_block(PROTO, "UploadReputationMails_V1")


def m_initializeservice_v1(*_a, **_k):
    return name_block(PROTO, "InitializeService_V1")


def m_addshopitem_v1(*_a, **_k):
    return name_block(PROTO, "AddShopItem_V1")


def m_removeshopitem_v1(*_a, **_k):
    return name_block(PROTO, "RemoveShopItem_V1")


def m_uploadshopitems_v1(*_a, **_k):
    return name_block(PROTO, "UploadShopItems_V1")


def m_uploadsteamappidmapping_v1(*_a, **_k):
    return name_block(PROTO, "UploadSteamAppIdMapping_V1")


def m_updateupcactivations_v1(*_a, **_k):
    return name_block(PROTO, "UpdateUPCActivations_V1")


def m_updatesteamactivations_v1(*_a, **_k):
    return name_block(PROTO, "UpdateSteamActivations_V1")


def m_uploadachievementrewards_v1(*_a, **_k):
    return name_block(PROTO, "UploadAchievementRewards_V1")


def m_uploadleaderboardtemplates_v1(*_a, **_k):
    return name_block(PROTO, "UploadLeaderboardTemplates_V1")


# --- builders for the methods that were UNKNOWN until 2026-11-05 -----------
def m_getfacebookaccount_v1(*_a, **_k):
    return name_block(PROTO, "GetFacebookAccount_V1") + _b_get_facebook_account(**_k)


def m_activateuniquekey_v1(*_a, **_k):
    return name_block(PROTO, "ActivateUniqueKey_V1") + _b_activate_unique_key(**_k)


def m_listshopitems_v1(*_a, **_k):
    return name_block(PROTO, "ListShopItems_V1") + _b_list_shop_items(**_k)


def m_getaccountinfo_v1(*_a, **_k):
    return name_block(PROTO, "GetAccountInfo_V1") + _b_get_account_info(**_k)


def m_fetchassetcountergraph_v1(*_a, **_k):
    return name_block(PROTO, "FetchAssetCounterGraph_V1") + _b_fetch_asset_counter_graph(
        **_k
    )


def m_getleaderboardtemplates_v1(*_a, **_k):
    return name_block(PROTO, "GetLeaderboardTemplates_V1") + _b_get_leaderboard_templates(
        **_k
    )


# --------------------------------------------------------------------------
# registries
# --------------------------------------------------------------------------
#: implemented method -> reply layout (documentation for humans and tests).
METHOD_NAMES = {
    "SetStatus_V1": "void",
    "CreatePlayerProfile_V1": "String msg + u16 kind + PlayerProfile + u32 count + N x (u32,u8)",
    "GetPlayerProfile_V1": "PlayerProfile: String + 5 x u32 + 2 x (u32 count + N x u32) + String",
    "DoesUserExist_V1": "u8 bool",
    "SetPortrait_V1": "void",
    "SetIcon_V1": "void",
    "SetColor_V1": "void",
    "SetPlayerTitle_V1": "void",
    "SetArk_V1": "void",
    "AddActivation_V1": "void",
    "GetAchievements_V1": "u32 count + N x (u32 id, u64 value)",
    "AddAchievement_V1": "u64",
    "GetSettings_V1": "u32 count + N x (u32 key, u32 value)",
    "SetSetting_V1": "void",
    "GetKeyBindings_V1": "u32 count + N x (u32 key, u32 n, n x u32)",
    "SetKeyBinding_V1": "void",
    "ResetKeyBindings_V1": "void",
    "AddHelperQuest_V1": "void",
    "ResetHelperQuests_V1": "void",
    "GetInfoMails_V1": "u32 count + N x (u32 key, u8 bool)",
    "AddInfoMail_V1": "void",
    "SetInfoMailRead_V1": "void",
    "GetStats_V1": "u32 count + N x (u32,u32,u32,u64,u64,u64)",
    "SetStats_V1": "void",
    "GetArkItems_V1": "u32 count + N x ArkItem (16 B: u32 f0,f1,f2,f3)",
    "SetArkItems_V1": "void",
    "GetStatus_V1": "u32",
    "GetWorldStats_V1": "u32 + u32 count + N x (u32,u32,u32,u64,u64,u64)",
    "ResetInfoMails_V1": "void",
    "UploadWorldStatsStencils_V1": "void",
    "GetReputationPoints_V1": "u32 count + N x (u32 key, u32 value)",
    "AreReputationPointsMissing_V1": "u8 bool",
    "SetReputationPoints_V1": "void",
    "SolveDailyQuest_V1": "void",
    "UploadFactionRewards_V1": "void",
    "UploadStatsReputation_V1": "void",
    "GetFacebookAccount_V1": "u64 + String",
    "SetFacebookAccount_V1": "void",
    "InvalidateAllReputationPoints_V1": "void",
    "UploadReputationMails_V1": "void",
    "ActivateUniqueKey_V1": "u32 count + N x u32",
    "InitializeService_V1": "void",
    "AddShopItem_V1": "void",
    "RemoveShopItem_V1": "void",
    "ListShopItems_V1": "u32 + u32 count + N x u32",
    "UploadShopItems_V1": "void",
    "UploadSteamAppIdMapping_V1": "void",
    "UpdateUPCActivations_V1": "void",
    "UpdateSteamActivations_V1": "void",
    "GetAccountInfo_V1": "String + String",
    "UploadAchievementRewards_V1": "void",
    "FetchAssetCounterGraph_V1": "u32 count + N x (u32,u32) + u32",
    "UploadLeaderboardTemplates_V1": "void",
    "GetLeaderboardTemplates_V1": "u32 count + N x (u32,u32,u64,u64,u32,u32 n, n x u8)",
}

#: method -> what is missing from the dump (deliberately not guessed).
#: Empty since 2026-11-05: all 54 readers are decompiled and implemented --
#: see decomp/batch/step4_readers/PROFILE-UNKNOWN.md for the mapping table.
UNKNOWN_METHODS = {}

_BUILDERS = {
    "SetStatus_V1": _b_void,
    "CreatePlayerProfile_V1": _b_create_player_profile,
    "GetPlayerProfile_V1": _b_get_player_profile,
    "DoesUserExist_V1": _b_does_user_exist,
    "SetPortrait_V1": _b_void,
    "SetIcon_V1": _b_void,
    "SetColor_V1": _b_void,
    "SetPlayerTitle_V1": _b_void,
    "SetArk_V1": _b_void,
    "AddActivation_V1": _b_void,
    "GetAchievements_V1": _b_get_achievements,
    "AddAchievement_V1": _b_add_achievement,
    "GetSettings_V1": _b_get_settings,
    "SetSetting_V1": _b_void,
    "GetKeyBindings_V1": _b_get_key_bindings,
    "SetKeyBinding_V1": _b_void,
    "ResetKeyBindings_V1": _b_void,
    "AddHelperQuest_V1": _b_void,
    "ResetHelperQuests_V1": _b_void,
    "GetInfoMails_V1": _b_get_info_mails,
    "AddInfoMail_V1": _b_void,
    "SetInfoMailRead_V1": _b_void,
    "GetStats_V1": _b_get_stats,
    "SetStats_V1": _b_void,
    "GetArkItems_V1": _b_get_ark_items,
    "SetArkItems_V1": _b_set_ark_items,
    "GetStatus_V1": _b_get_status,
    "GetWorldStats_V1": _b_get_world_stats,
    "ResetInfoMails_V1": _b_void,
    "UploadWorldStatsStencils_V1": _b_void,
    "GetReputationPoints_V1": _b_get_reputation_points,
    "AreReputationPointsMissing_V1": _b_reputation_missing,
    "SetReputationPoints_V1": _b_void,
    "SolveDailyQuest_V1": _b_void,
    "UploadFactionRewards_V1": _b_void,
    "UploadStatsReputation_V1": _b_void,
    "GetFacebookAccount_V1": _b_get_facebook_account,
    "SetFacebookAccount_V1": _b_void,
    "InvalidateAllReputationPoints_V1": _b_void,
    "UploadReputationMails_V1": _b_void,
    "ActivateUniqueKey_V1": _b_activate_unique_key,
    "InitializeService_V1": _b_void,
    "AddShopItem_V1": _b_void,
    "RemoveShopItem_V1": _b_void,
    "ListShopItems_V1": _b_list_shop_items,
    "UploadShopItems_V1": _b_void,
    "UploadSteamAppIdMapping_V1": _b_void,
    "UpdateUPCActivations_V1": _b_void,
    "UpdateSteamActivations_V1": _b_void,
    "GetAccountInfo_V1": _b_get_account_info,
    "UploadAchievementRewards_V1": _b_void,
    "FetchAssetCounterGraph_V1": _b_fetch_asset_counter_graph,
    "UploadLeaderboardTemplates_V1": _b_void,
    "GetLeaderboardTemplates_V1": _b_get_leaderboard_templates,
}


def canonical(method):
    """Normalise ``Proto::Method*`` / ``Method*`` / bytes to ``Method``."""
    try:
        if isinstance(method, (bytes, bytearray, memoryview)):
            method = bytes(method).decode("ascii", "replace")
        if not isinstance(method, str):
            return ""
        name = method.split("::")[-1]
        if name.endswith("*"):
            name = name[:-1]
        return name
    except Exception:
        return ""


def build(proto, method, **kw):
    """Full reply payload for ``proto``/``method``, or ``None`` if unknown.

    ``method`` may carry the ``Proto::`` prefix and/or the trailing ``*``.
    Extra keyword arguments are ignored, junk values are coerced: this never
    raises and never returns a partial payload for an unknown method.
    """
    try:
        name = canonical(method)
        fn = _BUILDERS.get(name)
        if fn is None:
            return None
        body = fn(**kw)
        if not isinstance(body, (bytes, bytearray)):
            return None
        return name_block(proto, name) + bytes(body)
    except Exception:
        return None
