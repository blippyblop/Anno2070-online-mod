"""Wire shapes for ``VotingServiceProtocol`` and ``WorldEventServiceProtocol``.

Pure functions only: no I/O, no mutable module state, no ``mimic_server``
imports.  Every per-method builder returns a *complete reply payload* -- the
method name block (``name_block(proto, method)``, i.e. the ``u16`` length that
includes the NUL plus ``"Proto::Method*"`` and its NUL) followed by the reply
fields.  The RMC envelope is added by the caller.

Evidence: ``decomp/batch/step3_voting_events/VOTING_EVENTS.md`` (SS2 field
tables, SS3 serialiser sketch, SS4 gaps, SS5 unknowns) and the .c files next to
it.  Encodings use the little-endian primitives from ``rv_shapes/__init__.py``
(``qstr`` = ``u16`` length *excluding* the NUL, then the bytes).

Value types (wire order, from the client-side readers/writers):

  VoteEntry                  String, u32, u32              (ddl_ser.c:3727)
  VotingInfo                 u32 id, u32, u32, bool, u64, u32, u64, u64, u32, u32
  VotingStats                u32 key, u32 f1, map<u32,u32> (ddl_ser.c:3676)
  VoteMultiplier             u32, u32, u32                 (ddl_ser.c:3521)
  WorldEvent                 u32, u32, String, u64, bool, u32, u32, bool
  WorldEventStatsEntry       String, u32, u32, u32
  WorldEventStats            list<Entry>, list<Entry>, u32, u32, u32
  WorldEventCompletedMission String, u32, String
  WorldEventMissionTemplate  u32, u32
  WorldEventTemplate         u32, String, 12*u32,
                             map<u32,Mission{u32,u32}>, vector<u32>,
                             map<u32,u32>                 (writer 0x140e09bb0)

``WorldEventTemplate`` only ever travels client -> server
(``UploadWorldEventTemplate_V1``); it is encoded here so the request record has
one reviewed definition and can be exercised by the tests.

``GetVoteCount_V1`` replies with a bare ``map<u32,u32>`` -- the reader
``FUN_140de9460`` (0x140de9460) is the *same* function the client uses for
``VotingStats.votes``.

``map<K,V>`` and every list are ``u32 count`` followed by the items; maps are
emitted in ascending key order (the engine stores them ordered).

Inputs are *tolerant*: each value type accepts a mapping (preferred, keyed by
the names used in ``rmc_services.py``/the report), a positional tuple/list, or
nothing at all.  Any junk value degrades to the documented default instead of
raising, so a malformed store can never take the session down.
"""

from collections.abc import Mapping

try:  # imported as part of the package (the normal path)
    from . import name_block, qstr, u8, u32, u64
except ImportError:  # pragma: no cover - direct import with server/ on sys.path
    from rv_shapes import name_block, qstr, u8, u32, u64


VOTING_PROTO = "VotingServiceProtocol"
WORLD_EVENT_PROTO = "WorldEventServiceProtocol"


# --------------------------------------------------------------------------
# scalar helpers (total: never raise, never return non-bytes)
# --------------------------------------------------------------------------

def _field(t, index, *names, **kwargs):
    """Read one logical field of a record.

    ``t`` may be a mapping (looked up by ``names``, first non-None wins), a
    list/tuple (looked up positionally at ``index``) or anything else.  Missing
    or unusable input yields ``kwargs["default"]`` (default ``0``).
    """
    default = kwargs.get("default", 0)
    if isinstance(t, Mapping):
        for name in names:
            if name in t and t[name] is not None:
                return t[name]
        return default
    if isinstance(t, (list, tuple)):
        if 0 <= index < len(t) or -len(t) <= index < 0:
            if t:
                return t[index]
        return default
    return default


def _num(value, default=0):
    """Coerce to int; anything unparseable becomes ``default``."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return default
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bool(value):
    """1-byte boolean by Python truthiness (junk -> 0)."""
    try:
        return u8(1 if value else 0)
    except Exception:  # pragma: no cover - defensive
        return b"\x00"


def _text(value, default=""):
    """Coerce to an ASCII-safe ``str`` (the wire is a Quazal String)."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    elif not isinstance(value, str):
        return default
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        value = value.encode("ascii", "replace").decode("ascii")
    return value


def _norm_items(items):
    """Normalise a list-ish argument into a python list of items."""
    if items is None:
        return []
    if isinstance(items, Mapping):
        return [items]  # a single record passed where a list was expected
    if isinstance(items, (bytes, str)):
        return []       # never iterate a string into characters
    try:
        return list(items)
    except TypeError:
        return []


# --------------------------------------------------------------------------
# containers
# --------------------------------------------------------------------------

def list_of(fn, items=None):
    """``u32 count`` + ``fn(item)`` for each item (empty list => count 0)."""
    values = _norm_items(items)
    if not callable(fn):
        return u32(0)
    out = [u32(len(values))]
    for item in values:
        try:
            chunk = fn(item)
        except Exception:  # total: one bad record must not kill the reply
            chunk = b""
        out.append(chunk if isinstance(chunk, bytes) else b"")
    return b"".join(out)


def _pairs(d):
    if d is None:
        return []
    if isinstance(d, Mapping):
        raw = list(d.items())
    else:
        raw = []
        try:
            for entry in d:
                try:
                    key, value = entry
                except (TypeError, ValueError):
                    continue
                raw.append((key, value))
        except TypeError:
            return []
    return [(_num(k), _num(v)) for k, v in raw]


def map_u32_u32(d=None):
    """``u32 count`` + ascending ``u32 key, u32 value`` pairs."""
    pairs = _pairs(d)
    pairs.sort(key=lambda kv: kv[0])
    return u32(len(pairs)) + b"".join(u32(k) + u32(v) for k, v in pairs)


# --------------------------------------------------------------------------
# value types
# --------------------------------------------------------------------------

def vote_entry(t=None):
    """VoteEntry: ``String voter, u32 a, u32 b``.

    Reader ``FUN_140df57c0`` @ 0x140df57c0 (decomp/ddl_ser.c:3727-3786):
    ``FUN_141371620`` (Quazal String) then two 4-byte raw reads.  Object is
    0x18 bytes = vptr + String(8) + 2 x u32.
    """
    return (qstr(_text(_field(t, 0, "voter", "name", "player", default="")))
            + u32(_num(_field(t, 1, "a", "choice")))
            + u32(_num(_field(t, 2, "b", "ts", "time"))))


def voting_info(t=None):
    """VotingInfo: ``u32 id, u32, u32, bool, u64, u32, u64, u64, u32, u32``.

    Reader ``FUN_140df5960`` @ 0x140df5960 (decomp/ddl_ser.c:3849-3905):

        u32 key (:3859)   u32 (:3860)   u32 (:3861)   bool FUN_14135e2a0 (:3862)
        u64 (:3863)       u32 (:3865)   u64 (:3866)   u64 (:3867)
        u32 (:3868)       u32 (:3869)

    The 0x40-byte object size (``FUN_140df5d90`` guard) is *not* the wire size:
    the object starts with an 8-byte vtable pointer (stored at node+0x28) and
    the fields occupy node+0x30..+0x68, so the wire value is 49 bytes.  Field
    *names* past the id are still the ones ``rmc_services.py`` used.
    """
    return (u32(_num(_field(t, 0, "id")))
            + u32(_num(_field(t, 1, "f2")))
            + u32(_num(_field(t, 2, "f3")))
            + _bool(_field(t, 3, "running", "active", default=False))
            + u64(_num(_field(t, 4, "start", "startTime")))
            + u32(_num(_field(t, 5, "f6")))
            + u64(_num(_field(t, 6, "votesA")))
            + u64(_num(_field(t, 7, "votesB")))
            + u32(_num(_field(t, 8, "f9")))
            + u32(_num(_field(t, 9, "f10"))))


def voting_stats(a=None, b=0, votes=None):
    """VotingStats: ``u32 key, u32 f1, map<u32,u32> votes``.

    Reader ``FUN_140df5670`` @ 0x140df5670 (decomp/ddl_ser.c:3676-3726): a
    4-byte key, a 4-byte value, then ``FUN_140de9460`` for the map.  Object is
    0x50 = vptr + key + u32 + std::map(0x38); wire = 8 + (4 + 4*len).

    Call either positionally -- ``voting_stats(3, 7, {1: 2})`` -- or with a
    single record: ``voting_stats({"id": 3, "f1": 7, "votes": {1: 2}})``.
    """
    if isinstance(a, (Mapping, list, tuple)):
        t = a
        key = _field(t, 0, "id", "key")
        f1 = _field(t, 1, "f1")
        votes = _field(t, 2, "votes", default=None)
    else:
        key = a
        f1 = b
    return u32(_num(key)) + u32(_num(f1)) + map_u32_u32(votes)


def vote_multiplier(t=None):
    """VoteMultiplier: ``u32, u32, u32`` (obj 0x18, 0xc wire bytes).

    Request writer decomp/ddl_ser.c:3514-3523: ``u32 count``, then three raw
    4-byte writes per element starting at element+8.
    """
    return (u32(_num(_field(t, 0, "a", "id", "multiplier")))
            + u32(_num(_field(t, 1, "b", "mult", "value")))
            + u32(_num(_field(t, 2, "c", "count"))))


def world_event(t=None):
    """WorldEvent: ``u32, u32, String, u64, bool, u32, u32, bool``.

    Reader ``FUN_140dfcba0`` @ 0x140dfcba0 (decomp/ddl_ser.c:8129-8146)::

        u32 (obj+8)   u32 (+0xc)   String (+0x10, FUN_141371620)
        u64 (+0x18)   bool (+0x20, FUN_14135e2a0)
        u32 (+0x24)   u32 (+0x28)  bool (+0x2c, FUN_14135e2a0)

    Object size 0x30.  ``get_latest_world_event`` sends this bare (no count).
    """
    return (u32(_num(_field(t, 0, "a", "id")))
            + u32(_num(_field(t, 1, "b", "eventId")))
            + qstr(_text(_field(t, 2, "name", default="")))
            + u64(_num(_field(t, 3, "start", "startTime")))
            + _bool(_field(t, 4, "running", "active", default=False))
            + u32(_num(_field(t, 5, "f", "f5")))
            + u32(_num(_field(t, 6, "g", "f6")))
            + _bool(_field(t, 7, "visited", default=False)))


def we_stats_entry(t=None):
    """WorldEventStatsEntry: ``String name, u32, u32, u32`` (obj 0x20).

    Reader ``FUN_140e09ed0`` @ 0x140e09ed0 (decomp/batch/step4_readers/
    worldevent.c:570-644): ``FUN_141371620`` then three 4-byte raw reads
    (local_48, local_48+4, local_40), stored at +0x10/+0x14/+0x18.
    """
    return (qstr(_text(_field(t, 0, "name", "text", default="")))
            + u32(_num(_field(t, 1, "a")))
            + u32(_num(_field(t, 2, "b")))
            + u32(_num(_field(t, 3, "c"))))


def we_stats(list_a=None, list_b=None, c0=0, c1=0, c2=0):
    """WorldEventStats: ``list<Entry> A, list<Entry> B, u32, u32, u32``.

    Reply reader (decomp/ddl_ser.c:8060-8085) calls ``FUN_140e09ed0`` twice --
    once for the vector at object+8, once for the vector at object+0x28 --
    then reads 4 bytes into +0x48, +0x4c and +0x50.  Object size 0x58.  A
    single record mapping (``{"listA": [...], "listB": [...], "c0": ..}``) is
    also accepted.
    """
    if isinstance(list_a, Mapping):
        t = list_a
        list_a = _field(t, 0, "listA", "A", default=None)
        list_b = _field(t, 1, "listB", "B", default=None)
        c0 = _field(t, 2, "c0", default=0)
        c1 = _field(t, 3, "c1", default=0)
        c2 = _field(t, 4, "c2", default=0)
    return (list_of(we_stats_entry, list_a)
            + list_of(we_stats_entry, list_b)
            + u32(_num(c0)) + u32(_num(c1)) + u32(_num(c2)))


def completed_mission(t=None):
    """WorldEventCompletedMission: ``String, u32 mission id, String``.

    Reader ``FUN_140dfcc60`` (decomp/ddl_ser.c:8149-8176): after the ``u32``
    count it does ``FUN_141371620`` (String), a 4-byte raw read, then
    ``FUN_141371620`` again.
    """
    return (qstr(_text(_field(t, 0, "a", "name", default="")))
            + u32(_num(_field(t, 1, "id", "missionId")))
            + qstr(_text(_field(t, 2, "b", "title", default=""))))


# --------------------------------------------------------------------------
# WorldEventTemplate (client -> server request record only)
# --------------------------------------------------------------------------

#: number of consecutive ``u32`` between the name and the mission map
WE_TEMPLATE_U32_COUNT = 12


def u32_list(values=None):
    """``vector<u32>``: ``u32 count`` + the items.

    Writer ``FUN_140e09bb0`` (0x140e09bb0,
    decomp/batch/step4_readers/worldevent.c:524-533): the count is
    ``(end - begin) >> 2`` of the vector at object+0x60, then one 4-byte raw
    write per element.
    """
    items = _norm_items(values)
    return u32(len(items)) + b"".join(u32(_num(v)) for v in items)


def world_event_mission_template(t=None):
    """WorldEventMissionTemplate: two ``u32``.

    The mapped value of the template's mission map.  Its object is a vtable
    pointer at +0 and the two fields at +8/+0xc; the writer emits
    node+0x30 and node+0x34 (0x140e09bb0, worldevent.c:500-501), i.e. the two
    members in declaration order and nothing else.  The map node is 0x38 bytes
    (= 0x20 header + key u32 + 0x18 for the value object), which is what pins
    the layout; the mail-service builder
    (decomp/__AVCMailServiceImpl_mgwa_rd__.c:1560-1570) fills the same two slots.
    """
    return (u32(_num(_field(t, 0, "a", "first", "count", "amount")))
            + u32(_num(_field(t, 1, "b", "second", "value", "limit"))))


def map_u32_mission_template(d=None):
    """``map<u32, WorldEventMissionTemplate>``: ``u32 count`` + ascending
    ``u32 key, u32, u32`` entries.

    Writer 0x140e09bb0 (worldevent.c:475-503): the count is the map size at
    object+0x50 (head at +0x48), then an in-order walk of the MSVC red-black
    tree emits key (node+0x20) and the two value u32 (node+0x30/+0x34).  An
    in-order walk == ascending keys, matching ``map_u32_u32``.
    """
    if d is None:
        return u32(0)
    if isinstance(d, Mapping):
        raw = list(d.items())
    else:
        raw = []
        try:
            for entry in d:
                try:
                    key, value = entry
                except (TypeError, ValueError):
                    continue
                raw.append((key, value))
        except TypeError:
            return u32(0)
    pairs = sorted(((_num(k), v) for k, v in raw), key=lambda kv: kv[0])
    return u32(len(pairs)) + b"".join(
        u32(k) + world_event_mission_template(v) for k, v in pairs)


def _template_fields(t):
    """The ``WE_TEMPLATE_U32_COUNT`` u32 at object+0x18..+0x44.

    Writer 0x140e09bb0 (worldevent.c:482-493): twelve consecutive 4-byte raw
    writes from +0x18 to +0x44.  The template object is 0x98 bytes and the
    builder copies exactly twelve u32 out of the source stencil
    (decomp/__AVCMailServiceImpl_mgwa_rd__.c:1517-1530), so the count is 12.
    """
    if isinstance(t, Mapping):
        for name in ("fields", "u32s"):
            if name in t and t[name] is not None:
                chosen = _norm_items(t[name])
                return [u32(_num(_field(chosen, i, "u%d" % (i + 1))))
                        for i in range(WE_TEMPLATE_U32_COUNT)]
        return [u32(_num(_field(t, 2 + i, "u%d" % (i + 1), "f%d" % (i + 2))))
                for i in range(WE_TEMPLATE_U32_COUNT)]
    if isinstance(t, (list, tuple)):
        return [u32(_num(_field(t, 2 + i, "u%d" % (i + 1))))
                for i in range(WE_TEMPLATE_U32_COUNT)]
    return [u32(0)] * WE_TEMPLATE_U32_COUNT


def world_event_template(t=None):
    """WorldEventTemplate record -- writer ``FUN_140e09bb0`` @ 0x140e09bb0.

    ``decomp/batch/step4_readers/worldevent.c:466-535``, in this exact order::

        +0x08  u32                                      (line 480)
        +0x10  String   FUN_141370f90                   (line 481)
        +0x18  12 x u32                                 (lines 482-493)
        +0x48  map<u32,Mission{u32,u32}>  count @+0x50  (lines 475, 494-503)
        +0x60  vector<u32>                count@size/4  (lines 524-533)
        +0x80  map<u32,u32>  FUN_140de9630              (line 534)

    ``FUN_140de9630`` is the map<u32,u32> writer (profile_readers.c:147-190):
    ``u32 size`` then ``u32 key, u32 value`` in ascending key order.

    Accepted input: a mapping keyed ``id``, ``name``, ``u1``..``u12``,
    ``missions``, ``values``, ``scores``, or a 17-element sequence in that
    order (index 0 id, 1 name, 2..13 the twelve u32, 14/15/16 the containers).
    """
    return (u32(_num(_field(t, 0, "id", "f1")))
            + qstr(_text(_field(t, 1, "name", default="")))
            + b"".join(_template_fields(t))
            + map_u32_mission_template(
                _field(t, 14, "missions", "missionTemplates", default=None))
            + u32_list(_field(t, 15, "values", "vec", "missionIds",
                              default=None))
            + map_u32_u32(
                _field(t, 16, "scores", "map", "totals", default=None)))


# --------------------------------------------------------------------------
# VotingServiceProtocol replies
# --------------------------------------------------------------------------

def get_voting_infos(infos=None):
    """``list<VotingInfo>`` (ddl_ser.c:2954-2968).

    Client reader ``FUN_140df5960`` @ 0x140df5960: ``u32 count`` then per entry
    the u32 key followed by the nine VotingInfo slots (a std::map<u32,VotingInfo>
    on the wire, so the entries are ascending by id).
    """
    return (name_block(VOTING_PROTO, "GetVotingInfos_V1")
            + list_of(voting_info, infos))


def vote(vote_id=0, choice=0):
    """``Vote_V1`` -- reply carries **no** body.

    The client's ``Vote_V1`` branch jumps straight to ``LAB_140df5644``
    (decomp/batch/step3_voting_events/__AVVotingServiceProtocolClient_rdv__.c,
    ``FUN_140df51f0``) -- not one read.  ``vote_id``/``choice`` are the request
    fields (two u32, ddl_ser.c:3032-3045); they are accepted so a handler can
    pass them through and are deliberately ignored here.
    """
    return name_block(VOTING_PROTO, "Vote_V1")


def get_last_voters(voters=None):
    """``list<VoteEntry>`` = ``String, u32, u32`` per entry.

    Client reader ``FUN_140df57c0`` @ 0x140df57c0 (decomp/ddl_ser.c:3190-3204):
    ``u32 count``, then ``FUN_141371620`` + 4-byte raw read + 4-byte raw read.
    """
    return (name_block(VOTING_PROTO, "GetLastVoters_V1")
            + list_of(vote_entry, voters))


def get_voting_stats(entries=None):
    """``list<VotingStats>`` = ``u32 key, u32, map<u32,u32>`` per entry.

    Client reader ``FUN_140df5670`` @ 0x140df5670 (decomp/ddl_ser.c:3271-3284):
    ``u32 count``, then a 4-byte key, a 4-byte value and ``FUN_140de9460`` for
    the nested map.  (The request carries a single ``bool``, written with
    ``FUN_14135e370``; it does not change the reply.)
    """
    return (name_block(VOTING_PROTO, "GetVotingStats_V1")
            + list_of(voting_stats, entries))


def get_vote_count(counts=None):
    """``map<u32,u32>`` -- client reader ``FUN_140de9460`` @ 0x140de9460.

    ``decomp/batch/step4_readers/profile_readers.c:62-142`` is the body::

        FUN_14135e5f0(stream, &count, 4, 1)        // u32 count      (:98)
        do {
          FUN_14135e5f0(stream, &key,   4)         // u32 key        (:102)
          FUN_14135e5f0(stream, &value, 4)         // u32 value      (:103)
          ...  insert into std::map<u32,u32>       (node 0x28 = 0x20
               header + key u32 + value u32, :129-136)
        } while (++i < count);

    The same reader backs ``VotingStats.votes``
    (decomp/__AV_DDL_VotingStats_rdv__.c ``FUN_140df5670``), and its writer
    mirror ``FUN_140de9630`` @ 0x140de9630 (profile_readers.c:147-190) emits
    ``u32 size`` + ascending ``u32 key, u32 value`` (profile_readers.c:157-166).
    The client call site is
    the ``GetVoteCount_V1`` branch of ``FUN_140df51f0``
    (decomp/batch/step3_voting_events/__AVVotingServiceProtocolClient_rdv__.c).

    So the reply is exactly ``map_u32_u32(counts)``; no extra u64 (the old
    responder emitted one -- that is what the earlier pass flagged).
    """
    return (name_block(VOTING_PROTO, "GetVoteCount_V1")
            + map_u32_u32(counts))


def clear_voting_templates():
    """``ClearVotingTemplates_V1`` -- no reply body."""
    return name_block(VOTING_PROTO, "ClearVotingTemplates_V1")


def upload_voting_template(template_id=0, level=0):
    """``UploadVotingTemplate_V1`` -- no reply body (request is ``u32,u32``)."""
    return name_block(VOTING_PROTO, "UploadVotingTemplate_V1")


def upload_vote_multipliers(multipliers=None):
    """``UploadVoteMultipliers_V1`` -- no reply body.

    Client request writer (ddl_ser.c:3504-3523) emits ``u32 count`` then
    ``u32, u32, u32`` per 0x18-stride VoteMultiplier (raw writes at +8, +0xc,
    +0x10) -- exactly what ``vote_multiplier`` encodes.  The reply is nothing:
    the client's ``UploadVoteMultipliers_V1`` branch jumps straight to the end
    of the dispatcher (__AVVotingServiceProtocolClient_rdv__.c), so no echo and
    no count.
    """
    return name_block(VOTING_PROTO, "UploadVoteMultipliers_V1")


# --------------------------------------------------------------------------
# WorldEventServiceProtocol replies
# --------------------------------------------------------------------------

def register():
    """``Register_V1`` -- no reply body."""
    return name_block(WORLD_EVENT_PROTO, "Register_V1")


def unregister():
    """``Unregister_V1`` -- no reply body."""
    return name_block(WORLD_EVENT_PROTO, "Unregister_V1")


def get_latest_world_event(event=None):
    """Single ``WorldEvent`` with **no** count prefix.

    Client reader ``FUN_140dfcba0`` @ 0x140dfcba0 (decomp/ddl_ser.c:8129-8146)
    reads, in order: 4 bytes (+8), 4 bytes (+0xc), ``FUN_141371620`` (+0x10),
    8 bytes (+0x18), ``FUN_14135e2a0`` bool (+0x20), 4 bytes (+0x24), 4 bytes
    (+0x28), ``FUN_14135e2a0`` bool (+0x2c).  The call site
    (ddl_ser.c:7968-7981) passes the object *directly* -- there is no list
    reader around it.
    """
    return (name_block(WORLD_EVENT_PROTO, "GetLatestWorldEvent_V1")
            + world_event(event))


def complete_mission(result=0):
    """``u32`` reply (ddl_ser.c:7994); request is ``u32, string``."""
    if isinstance(result, (Mapping, list, tuple)):
        result = _field(result, 0, "result", "status", "id")
    return name_block(WORLD_EVENT_PROTO, "CompleteMission_V1") + u32(_num(result))


def get_own_completed_missions(missions=None):
    """``list<WorldEventCompletedMission>`` (reader ddl_ser.c:8149-8176)."""
    return (name_block(WORLD_EVENT_PROTO, "GetOwnCompletedMissions_V1")
            + list_of(completed_mission, missions))


def get_last_completed_missions(missions=None):
    """``list<WorldEventCompletedMission>`` (request ``u32`` limit)."""
    return (name_block(WORLD_EVENT_PROTO, "GetLastCompletedMissions_V1")
            + list_of(completed_mission, missions))


def set_visited(visited=True):
    """``SetVisited_V1`` -- no reply body."""
    return name_block(WORLD_EVENT_PROTO, "SetVisited_V1")


def clear_world_event_templates():
    """``ClearWorldEventTemplates_V1`` -- no reply body."""
    return name_block(WORLD_EVENT_PROTO, "ClearWorldEventTemplates_V1")


def upload_world_event_template(template=None):
    """``UploadWorldEventTemplate_V1`` -- reply is **void**.

    The client request builder ``FUN_140dfbcb0`` @ 0x140dfbcb0 registers the
    two DDL types ``WorldEventTemplate`` / ``WorldEventMissionTemplate`` and
    calls ``FUN_140e09bb0(stream, template)`` (decomp/ddl_ser.c:7675-7676),
    i.e. one template record and nothing else.  The reply dispatcher has an
    empty body for this name: the ``UploadWorldEventTemplate_V1`` branch jumps
    straight to ``LAB_140dfcb7d`` without a single read
    (decomp/ddl_ser.c:7985-7996), so the payload is the name block only --
    no echo of the template, no status u32.

    ``template`` is accepted so a handler can pass the request through
    unchanged; ``world_event_template()`` encodes it if it is ever needed.
    """
    return name_block(WORLD_EVENT_PROTO, "UploadWorldEventTemplate_V1")


def are_reputation_points_missing(missing=False):
    """``bool`` reply (ddl_ser.c:8090-8095)."""
    return (name_block(WORLD_EVENT_PROTO, "AreReputationPointsMissing_V1")
            + _bool(missing))


def set_reputation_points(points=0, what=0):
    """``SetReputationPoints_V1`` -- no reply body (request is ``u32,u32``)."""
    return name_block(WORLD_EVENT_PROTO, "SetReputationPoints_V1")


def get_stats(stats=None):
    """``WorldEventStats`` reply -- reader ``FUN_140e09ed0``/ddl_ser.c:8060-8085.

    Two ``list<WorldEventStatsEntry>`` (objects at +8 and +0x28, each read by
    ``FUN_140e09ed0``) followed by three u32 (+0x48, +0x4c, +0x50).  Order on
    the wire is list at +8 first, then the list at +0x28.
    """
    if isinstance(stats, (list, tuple)) and not isinstance(stats, Mapping):
        return (name_block(WORLD_EVENT_PROTO, "GetStats_V1")
                + we_stats(stats[0] if len(stats) > 0 else None,
                           stats[1] if len(stats) > 1 else None,
                           stats[2] if len(stats) > 2 else 0,
                           stats[3] if len(stats) > 3 else 0,
                           stats[4] if len(stats) > 4 else 0))
    return name_block(WORLD_EVENT_PROTO, "GetStats_V1") + we_stats(stats)


# --------------------------------------------------------------------------
# dispatch tables
# --------------------------------------------------------------------------

METHOD_NAMES = {
    # VotingServiceProtocol (8)
    "GetVotingInfos_V1": get_voting_infos,
    "Vote_V1": vote,
    "GetVoteCount_V1": get_vote_count,
    "GetLastVoters_V1": get_last_voters,
    "GetVotingStats_V1": get_voting_stats,
    "ClearVotingTemplates_V1": clear_voting_templates,
    "UploadVotingTemplate_V1": upload_voting_template,
    "UploadVoteMultipliers_V1": upload_vote_multipliers,
    # WorldEventServiceProtocol (12)
    "Register_V1": register,
    "Unregister_V1": unregister,
    "GetLatestWorldEvent_V1": get_latest_world_event,
    "CompleteMission_V1": complete_mission,
    "GetOwnCompletedMissions_V1": get_own_completed_missions,
    "GetLastCompletedMissions_V1": get_last_completed_missions,
    "SetVisited_V1": set_visited,
    "ClearWorldEventTemplates_V1": clear_world_event_templates,
    "UploadWorldEventTemplate_V1": upload_world_event_template,
    "AreReputationPointsMissing_V1": are_reputation_points_missing,
    "SetReputationPoints_V1": set_reputation_points,
    "GetStats_V1": get_stats,
}

#: nothing is left unexplained.  ``GetVoteCount_V1`` was settled by the reader
#: body ``FUN_140de9460`` (profile_readers.c:62-140) and
#: ``UploadWorldEventTemplate_V1`` by the writer ``FUN_140e09bb0``
#: (worldevent.c:466-535); see decomp/batch/step4_readers/VOTING-UNKNOWN.md.
UNKNOWN_METHODS = {}

#: protocol that owns each implemented method (names are unique across both
#: protocols, but ``build`` still refuses a mismatched proto).
METHOD_PROTOCOLS = {
    "GetVotingInfos_V1": VOTING_PROTO,
    "Vote_V1": VOTING_PROTO,
    "GetLastVoters_V1": VOTING_PROTO,
    "GetVotingStats_V1": VOTING_PROTO,
    "ClearVotingTemplates_V1": VOTING_PROTO,
    "UploadVotingTemplate_V1": VOTING_PROTO,
    "UploadVoteMultipliers_V1": VOTING_PROTO,
    "Register_V1": WORLD_EVENT_PROTO,
    "Unregister_V1": WORLD_EVENT_PROTO,
    "GetLatestWorldEvent_V1": WORLD_EVENT_PROTO,
    "CompleteMission_V1": WORLD_EVENT_PROTO,
    "GetOwnCompletedMissions_V1": WORLD_EVENT_PROTO,
    "GetLastCompletedMissions_V1": WORLD_EVENT_PROTO,
    "SetVisited_V1": WORLD_EVENT_PROTO,
    "ClearWorldEventTemplates_V1": WORLD_EVENT_PROTO,
    "AreReputationPointsMissing_V1": WORLD_EVENT_PROTO,
    "SetReputationPoints_V1": WORLD_EVENT_PROTO,
    "GetStats_V1": WORLD_EVENT_PROTO,
    "UploadWorldEventTemplate_V1": WORLD_EVENT_PROTO,
    "GetVoteCount_V1": VOTING_PROTO,
}


def build(proto, method, **kwargs):
    """Full reply payload for ``proto::method`` or ``None``.

    ``None`` means "no shape known" -- either the method is listed in
    ``UNKNOWN_METHODS`` or it belongs to a different protocol.  Callers should
    fall back to a default/void reply in that case.
    """
    if not isinstance(method, str):  # junk / unhashable method name
        return None
    fn = METHOD_NAMES.get(method)
    if fn is None:
        return None
    if METHOD_PROTOCOLS.get(method, proto) != proto:
        return None
    try:
        return fn(**kwargs)
    except TypeError:
        return fn()
