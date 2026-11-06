"""Wire shapes for ``MatchMakingServiceProtocol`` (A5 lobby / matchmaking).

Pure functions only: no I/O, no mutable module state, no ``mimic_server``
imports.  Every ``m_*`` builder returns a *complete reply payload* -- the method
name block (``name_block(PROTO, method)``: a little-endian ``u16`` length that
**includes** the NUL, then ``"MatchMakingServiceProtocol::Method*"`` and the
NUL) followed by the reply fields.  The RMC envelope is added by the caller.

Evidence (all addresses are **RVAs** of ``Anno5.exe`` 3.0.8051, image base
``0x140000000``; VA = ``0x140000000 + RVA``):

* The protocol has exactly **30** methods.  The names sit in three static
  string blocks that the client's own tables point at --
  ``0x1667CD8``..``0x16686C8`` (29 + ``GetSessions_V1``), ``0x166B518``..
  ``0x166BB00`` (30) and ``0x166D350``..``0x166D928`` (30) -- and the reply
  dispatcher checks *all 30* names against the incoming call
  (``FUN_140E04110``, RVA ``0xE04110``, extents ``0xE04110``-``0xE05040``).
  There is no numeric protocol/method id on the wire; the client routes by
  name (see ``decomp/_adhoc/auth-protocol.md`` SS1.3).
* The dispatcher was read twice, independently: decompiled
  (``decomp/ddl_ser.c:12613``, ``decomp/__AVA5_GameSession_rdv__.c:16``) and
  disassembled byte-exactly (capstone on the image, ``0xE04110``-``0xE05040``).
  The disassembly is the authority for the *branch* directions, because Ghidra
  renders a "matched -> ``goto`` end" branch as a braceless ``if`` and that is
  easy to mis-attribute.  Branch table (``je``/``jne`` to ``0xE0502F`` = the
  single function exit, i.e. "reply has no body"):

  ====================== ========= ====================================
  method                  check RVA branch
  ====================== ========= ====================================
  GetSessions_V1          0xE0414B  je 0xE041D9   (body at 0xE04187+)
  CreateSession_V1        0xE041ED  je 0xE042C6
  SetSessionParameters_V1 0xE042DA  je 0xE0437F
  PreJoinSession_V1       0xE04393  je 0xE044A3
  JoinSession_V1          0xE044B7  je 0xE045ED
  LeaveSession_V1         0xE04601  je 0xE04698   (body jumps *back* to
                                                  0xE04320 = the shared
                                                  "one Variant" tail)
  SetReadyFlag_V1         0xE046AC  jne end       -> VOID
  StartSession_V1         0xE046FB  jne end       -> VOID
  GetHostStationUrls_V1   0xE0474A  je 0xE047D8
  GetGameSessionSlots_V1  0xE047EC  je 0xE048A9
  GetUnassignedPlayers_V1 0xE048BD  je 0xE0493D
  SetAiSlot_V1            0xE04951  jne end       -> VOID
  SetHumanSlot_V1         0xE049A0  jne end       -> VOID
  SetSubSlot_V1           0xE049EF  jne end       -> VOID
  UpdateSlotColor_V1      0xE04A3E  jne end       -> VOID
  UpdateSlotFaction_V1    0xE04A8D  jne end       -> VOID
  ClearSlot_V1            0xE04ADC  jne end       -> VOID
  ClearSubSlot_V1         0xE04B2B  jne end       -> VOID
  MoveSlot_V1             0xE04B7A  jne end       -> VOID
  MoveSubSlot_V1          0xE04BC9  jne end       -> VOID
  SwapSubSlots_V1         0xE04C18  jne end       -> VOID
  KickPlayer_V1           0xE04C67  jne end       -> VOID
  SetHostDescriptor_V1    0xE04CB6  jne end       -> VOID
  GetGameSettings_V1      0xE04D05  je 0xE04DAA
  SetGameSettings_V1      0xE04DBE  jne end       -> VOID
  SetGameSetting_V1       0xE04E0D  jne end       -> VOID
  GetGameSessionPlayers_V1 0xE04E5C je 0xE04EEA
  GetGameSessionPlayer_V1 0xE04EFE  je 0xE04F45
  SetOwnPlayerProperty_V1 0xE04F59  jne end       -> VOID
  GetOwnStationUrls_V1    0xE04FA8  je 0xE04FEC
  ====================== ========= ====================================

  A name that matches *no* entry builds an error ``Variant`` (type ``2``,
  empty string) and reports it through ``FUN_141371D40`` (RVA ``0x1371D40``);
  that is the only "unknown method" path and it has no reply bytes.

Readers (each is the function the dispatcher calls with
``(ByteStream, destination)``), wire order top-to-bottom:

* ``FUN_14135E5F0(stream, ptr, 4)`` (RVA ``0x135E5F0``) -- raw ``u32``
  (``n = size*count`` byte memcpy out of the stream; little-endian because it
  is a plain copy and the client's own request envelope is LE -- PN 135).
* ``FUN_14135E2A0`` (RVA ``0x135E2A0``) -- ``bool``, 1 byte.
* ``FUN_141371620`` (RVA ``0x1371620``) -- Quazal ``String``: ``u16`` length
  **excluding** the NUL, then that many bytes (it resizes the target to
  ``len+1`` locally, it does *not* read a NUL).
* ``FUN_141371440`` (RVA ``0x1371440``) -- generic **Variant**: a Quazal
  ``String`` followed by a ``u16`` type tag.  Disassembly of ``0x1371440``:
  ``call 0x141371620`` (String) then ``r8d=2`` ``call 0x14135e5f0`` (2 bytes)
  then ``FUN_1411995B0(dst, string, tag)`` -- i.e. ``qstr(text) + u16(tag)``,
  type tag *after* the text.  ``FUN_141199740`` (RVA ``0x1199740``) is the
  client's own default Variant: type ``0x15``, empty string.
* ``FUN_140DE9460`` (RVA ``0xDE9460``) -- ``map<u32,u32>``: ``u32`` count then
  ``count x (u32 key, u32 value)`` (the same reader ``rv_shapes.voting`` uses
  for ``GetVoteCount_V1``/``VotingStats``).
* ``FUN_140DF8B00`` (RVA ``0xDF8B00``) -- ``list<String>``: ``u32`` count then
  ``count x String`` (list vs vector is invisible on the wire).
* ``FUN_140E05680`` (RVA ``0xE05680``) -- ``vector<A5_GameSession>``.
* ``FUN_140E05290`` (RVA ``0xE05290``) -- one ``A5_GameSessionPlayer``.
* ``FUN_140E05050`` (RVA ``0xE05050``) -- ``vector<A5_GameSessionPlayer>``.
* ``FUN_140E05360`` (RVA ``0xE05360``) -- ``vector<A5_GameSessionSlot>``.
* ``FUN_140E0A4A0`` (RVA ``0xE0A4A0``) -- ``vector<A5_GameSessionSubSlot>``.
* ``FUN_140E05510`` (RVA ``0xE05510``) -- ``vector<A5_StationUrls>``; each
  element is ``String``, ``u32``, ``list<String>`` (``FUN_140DF8A10``).
* ``FUN_140E00210`` (RVA ``0xE00210``) -- single player (wraps ``0xE05290``),
  ``FUN_140E002B0`` (RVA ``0xE002B0``) -- string list (wraps ``0xDF8B00``).

Per-method reply bodies (``-`` = nothing, i.e. name block only):

  GetSessions_V1             vector<A5_GameSession>
  CreateSession_V1           Variant, u32
  SetSessionParameters_V1    Variant
  PreJoinSession_V1          Variant, vector<A5_StationUrls>
  JoinSession_V1             Variant, String, bool
  LeaveSession_V1            Variant
  GetHostStationUrls_V1      list<String>
  GetGameSessionSlots_V1     u32, vector<A5_GameSessionSlot>
  GetUnassignedPlayers_V1    vector<A5_GameSessionPlayer>
  GetGameSettings_V1         map<u32,u32>
  GetGameSessionPlayers_V1   vector<A5_GameSessionPlayer>
  GetGameSessionPlayer_V1    A5_GameSessionPlayer
  GetOwnStationUrls_V1       list<String>
  (17 setters)               -

Struct wire shapes (order proven; *names* are [G]):

  A5_GameSession          u32, String, String, u32, u32, map<u32,u32>
  A5_GameSessionPlayer    String, 6 x u32, bool, map<u32,u32>, bool
  A5_GameSessionSlot      u32, bool, 5 x u32, vector<A5_GameSessionSubSlot>
  A5_GameSessionSubSlot   u32, String
  A5_StationUrls          String, u32, list<String>

Certainty ledger:
  [C] every method name, every branch direction, every field *type* and
      *order* above (disassembly of ``0xE04110``-``0xE05040`` plus the reader
      bodies), the name-block and String conventions.
  [G] the semantic meaning of the unnamed ``u32`` fields, and the *content* of
      the ``Variant`` arguments (layout [C], payload [G] -- see
      ``PROVISIONAL_METHODS``).  Nothing in the image says what the Ubisoft
      server put in the Variant's string; we emit the client's own default
      (empty text, tag ``0x15``) which the reader marks "absent", and let the
      caller override it per call (``text=``/``tag=``).
"""

from collections.abc import Mapping

try:  # imported as part of the package (the normal path)
    from . import name_block, qstr, u8, u16, u32
except ImportError:  # pragma: no cover - direct import with server/ on sys.path
    from rv_shapes import name_block, qstr, u8, u16, u32


PRESENT_IN_ANNO = True
PROTO = "MatchMakingServiceProtocol"

#: Type tag the client itself uses for an "absent"/default Variant
#: (``FUN_141199740`` RVA ``0x1199740``: ``*variant = 0x15``, empty string).
DEFAULT_VARIANT_TAG = 0x15

#: Tag the client uses for the "no such method" error Variant
#: (``FUN_141199620`` RVA ``0x1199620`` called with ``(.., 2, 1)`` at
#: ``0xE05005``/``0xE05012``).
ERROR_VARIANT_TAG = 2


# --------------------------------------------------------------------------
# scalar helpers (total: never raise, never return non-bytes)
# --------------------------------------------------------------------------

def _field(t, index, *names, **kwargs):
    """One logical field of a record (mapping by name, sequence, or default)."""
    default = kwargs.get("default", 0)
    if isinstance(t, Mapping):
        for name in names:
            if name in t and t[name] is not None:
                return t[name]
        return default
    if isinstance(t, (list, tuple)):
        if -len(t) <= index < len(t):
            return t[index]
        return default
    return default


def _num(value, default=0):
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


def _flag(value):
    """1-byte boolean by truthiness (junk -> 0)."""
    try:
        return u8(1 if value else 0)
    except Exception:  # pragma: no cover - defensive
        return b"\x00"


def _text(value, default=""):
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
    if items is None:
        return []
    if isinstance(items, Mapping):
        return [items]          # one record where a list was expected
    if isinstance(items, (bytes, str)):
        return []               # never iterate a string into characters
    try:
        return list(items)
    except TypeError:
        return []


def seq(fn, items=None):
    """``u32 count`` + ``fn(item)`` per item (empty -> ``00 00 00 00``)."""
    values = _norm_items(items)
    if not callable(fn):
        return u32(0)
    out = [u32(len(values))]
    for item in values:
        try:
            chunk = fn(item)
        except Exception:       # one bad record must not kill the reply
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
    """``u32 count`` + ascending ``u32 key, u32 value`` pairs (RVA 0xDE9460)."""
    pairs = _pairs(d)
    pairs.sort(key=lambda kv: kv[0])
    return u32(len(pairs)) + b"".join(u32(k) + u32(v) for k, v in pairs)


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

def variant(text="", tag=DEFAULT_VARIANT_TAG):
    """Generic Variant: ``String`` then ``u16`` type tag (RVA 0x1371440).

    The reader is ``FUN_141371440``: ``qstr`` first, then two raw bytes, then
    ``FUN_1411995B0(dst, text, tag)``.  The *tag* is a DDL type id; the client
    stores it in the Variant and treats "empty text + tag 0" as *absent*.
    What the reference server put here is **not** recoverable from the image
    -- ``text``/``tag`` are exposed so a handler can tune them once a capture
    exists.  Default: the client's own default Variant (empty text, 0x15).
    """
    return qstr(_text(text)) + u16(_num(tag) & 0xFFFF)


def string_list(items=None):
    """``list<String>``: ``u32 count`` + Strings (reader RVA 0xDF8B00)."""
    return seq(lambda s: qstr(_text(s)), items)


# --------------------------------------------------------------------------
# value types (A5 game-session DDL)
# --------------------------------------------------------------------------

def game_session(t=None):
    """``A5_GameSession``: ``u32, String, String, u32, u32, map<u32,u32>``.

    Reader ``FUN_140E05680`` (RVA ``0xE05680``): the loop head reads the four
    scalars / two strings in exactly this order (disassembly at
    ``0xE05680``+ : ``r8d=4`` u32 -> String -> String -> u32 -> u32 ->
    ``FUN_140DE9460`` map).  The object is 0x50 bytes in memory but only these
    six values travel.  Field *names*: only ``id`` is a guess; the two strings
    look like name/description and the trailing pair like player counts [G].
    """
    return (u32(_num(_field(t, 0, "id", "session_id", "sessionId"), default=1))
            + qstr(_text(_field(t, 1, "name", "session_name", default="")))
            + qstr(_text(_field(t, 2, "description", "host", "desc", default="")))
            + u32(_num(_field(t, 3, "players", "player_count", "f4"), default=1))
            + u32(_num(_field(t, 4, "max_players", "maxPlayers", "f5"), default=8))
            + map_u32_u32(_field(t, 5, "settings", "game_settings", "f6", default=None)))


def game_session_list(items=None):
    """``vector<A5_GameSession>``: ``u32 count`` + records (RVA 0xE05680)."""
    return seq(game_session, items)


def game_session_player(t=None):
    """``A5_GameSessionPlayer``: ``String, 6 x u32, bool, map, bool``.

    Reader ``FUN_140E05290`` (RVA ``0xE05290``), straight-line: String at
    +8, ``u32`` at +0x10/+0x14/+0x18/+0x1c/+0x20/+0x24, ``bool`` at +0x28,
    ``map<u32,u32>`` at +0x30 (``FUN_140DE9460``), ``bool`` at +0x48.  Field
    names past ``name`` are [G] (a player record: id/faction/colour/slot/...).
    """
    return (qstr(_text(_field(t, 0, "name", "player", "player_name", default="")))
            + u32(_num(_field(t, 1, "player_id", "playerId", "f2")))
            + u32(_num(_field(t, 2, "faction", "team", "f3")))
            + u32(_num(_field(t, 3, "color", "colour", "f4")))
            + u32(_num(_field(t, 4, "slot", "slot_id", "f5")))
            + u32(_num(_field(t, 5, "sub_slot", "subSlot", "f6")))
            + u32(_num(_field(t, 6, "f7", "ready_state")))
            + _flag(_field(t, 7, "ready", "is_ready", default=False))
            + map_u32_u32(_field(t, 8, "properties", "props", default=None))
            + _flag(_field(t, 9, "host", "is_host", default=False)))


def game_session_player_list(items=None):
    """``vector<A5_GameSessionPlayer>`` (RVA ``0xE05050``)."""
    return seq(game_session_player, items)


def game_session_sub_slot(t=None):
    """``A5_GameSessionSubSlot``: ``u32, String`` (reader RVA ``0xE0A4A0``)."""
    return (u32(_num(_field(t, 0, "id", "sub_slot_id")))
            + qstr(_text(_field(t, 1, "name", "player_name", default=""))))


def game_session_slot(t=None):
    """``A5_GameSessionSlot``: ``u32, bool, 5 x u32, vector<SubSlot>``.

    Reader ``FUN_140E05360`` (RVA ``0xE05360``) reads, in order: ``u32``,
    ``bool`` (``FUN_14135E2A0``), ``u32``, ``u32``, ``u32``, ``u32``, ``u32``,
    then ``FUN_140E0A4A0`` = ``vector<A5_GameSessionSubSlot>``.
    """
    return (u32(_num(_field(t, 0, "index", "slot_id", "id")))
            + _flag(_field(t, 1, "locked", "closed", "is_empty", default=False))
            + u32(_num(_field(t, 2, "faction", "team", "f3")))
            + u32(_num(_field(t, 3, "color", "colour", "f4")))
            + u32(_num(_field(t, 4, "player_id", "playerId", "f5")))
            + u32(_num(_field(t, 5, "ai", "ai_type", "f6")))
            + u32(_num(_field(t, 6, "f7")))
            + seq(game_session_sub_slot, _field(t, 7, "sub_slots", "subSlots",
                                                "sub", default=None)))


def game_session_slot_list(items=None):
    """``vector<A5_GameSessionSlot>`` (RVA ``0xE05360``)."""
    return seq(game_session_slot, items)


def station_urls(t=None):
    """``A5_StationUrls``: ``String, u32, list<String>`` (RVA ``0xE05510``).

    Per element the reader does ``FUN_141371620`` (String), ``u32`` and
    ``FUN_140DF8A10`` (``list<String>``, RVA ``0xDF8A10``: ``u32`` count +
    Strings).
    """
    return (qstr(_text(_field(t, 0, "name", "host_name", default="")))
            + u32(_num(_field(t, 1, "f2", "kind")))
            + string_list(_field(t, 2, "urls", "stations", default=None)))


def station_urls_list(items=None):
    """``vector<A5_StationUrls>`` (RVA ``0xE05510``)."""
    return seq(station_urls, items)


# --------------------------------------------------------------------------
# defaults (one session, one participant = the local player)
# --------------------------------------------------------------------------

DEFAULT_SESSION = {
    "id": 1,
    "name": "Anno 2070",
    "description": "",
    "players": 1,
    "max_players": 8,
    "settings": {},
}

DEFAULT_PLAYER = {
    "name": "Player",
    "player_id": 1,
    "faction": 0,
    "color": 0,
    "slot": 0,
    "sub_slot": 0,
    "f7": 0,
    "ready": True,
    "properties": {},
    "host": True,
}

DEFAULT_SLOT = {
    "index": 0,
    "locked": False,
    "faction": 0,
    "color": 0,
    "player_id": 1,
    "ai": 0,
    "f7": 0,
    "sub_slots": (),
}

DEFAULT_URL = {"name": "127.0.0.1:21020", "f2": 0, "urls": ()}

#: a session id GUID is *not* on the wire for this protocol (the session key
#: is the plain ``u32`` in ``A5_GameSession``); kept for callers that want one
DEFAULT_SESSION_GUID = "00000000-0000-0000-0000-000000000001"


def _one(value, default):
    """``value`` or ``default`` -- but a *list* means "take the first item"."""
    if value is None:
        return default
    if isinstance(value, (list, tuple)) and not isinstance(value, Mapping):
        return value[0] if value else default
    return value


def _many(value, default):
    """``value`` as a list; ``None`` -> the one-item default list."""
    if value is None:
        return [default]
    items = _norm_items(value)
    return items


# --------------------------------------------------------------------------
# per-method builders (name block included)
# --------------------------------------------------------------------------

def m_get_sessions_v1(sessions=None, **_kw):
    """``vector<A5_GameSession>`` -- reader ``0xE05680`` (body at 0xE04187)."""
    return (name_block(PROTO, "GetSessions_V1")
            + game_session_list(_many(sessions, DEFAULT_SESSION)))


def m_create_session_v1(session=None, result=0, text="",
                        tag=DEFAULT_VARIANT_TAG, **_kw):
    """``Variant, u32`` -- body at 0xE0422D: Variant then 4 raw bytes."""
    return (name_block(PROTO, "CreateSession_V1")
            + variant(text, tag)
            + u32(_num(_field(session, 0, "id", "session_id"), default=result)))


def m_set_session_parameters_v1(text="", tag=DEFAULT_VARIANT_TAG, **_kw):
    """``Variant`` only -- body at 0xE04316 (shared with LeaveSession)."""
    return name_block(PROTO, "SetSessionParameters_V1") + variant(text, tag)


def m_pre_join_session_v1(stations=None, text="", tag=DEFAULT_VARIANT_TAG, **_kw):
    """``Variant, vector<A5_StationUrls>`` -- body at 0xE043D3/0xE0443A."""
    return (name_block(PROTO, "PreJoinSession_V1")
            + variant(text, tag)
            + station_urls_list(_many(stations, DEFAULT_URL)))


def m_join_session_v1(session=None, name=None, joined=True, text="",
                      tag=DEFAULT_VARIANT_TAG, **_kw):
    """``Variant, String, bool`` -- body at 0xE044F7/0xE0455E/0xE045C5."""
    return (name_block(PROTO, "JoinSession_V1")
            + variant(text, tag)
            + qstr(_text(session if name is None else name))
            + _flag(joined))


def m_leave_session_v1(text="", tag=DEFAULT_VARIANT_TAG, **_kw):
    """``Variant`` only -- body at 0xE04638 (``jne 0xE04320`` = shared tail)."""
    return name_block(PROTO, "LeaveSession_V1") + variant(text, tag)


def m_set_ready_flag_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE046E1 (name block only)."""
    return name_block(PROTO, "SetReadyFlag_V1")


def m_start_session_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04730."""
    return name_block(PROTO, "StartSession_V1")


def m_get_host_station_urls_v1(urls=None, **_kw):
    """``list<String>`` -- body ``FUN_140DF8B00`` at 0xE04786 (reader 0xDF8B00)."""
    return name_block(PROTO, "GetHostStationUrls_V1") + string_list(_many(urls, DEFAULT_URL["name"]))


def m_get_game_session_slots_v1(slots=None, count=None, **_kw):
    """``u32, vector<A5_GameSessionSlot>`` -- body at 0xE04827/0xE04857."""
    items = _many(slots, DEFAULT_SLOT)
    head = len(items) if count is None else _num(count)
    return (name_block(PROTO, "GetGameSessionSlots_V1")
            + u32(head)
            + game_session_slot_list(items))


def m_get_unassigned_players_v1(players=None, **_kw):
    """``vector<A5_GameSessionPlayer>`` -- body at 0xE048F9 (reader 0xE05050)."""
    return (name_block(PROTO, "GetUnassignedPlayers_V1")
            + game_session_player_list(_many(players, DEFAULT_PLAYER)))


def m_set_ai_slot_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04986."""
    return name_block(PROTO, "SetAiSlot_V1")


def m_set_human_slot_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE049D5."""
    return name_block(PROTO, "SetHumanSlot_V1")


def m_set_sub_slot_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04A24."""
    return name_block(PROTO, "SetSubSlot_V1")


def m_update_slot_color_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04A73."""
    return name_block(PROTO, "UpdateSlotColor_V1")


def m_update_slot_faction_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04AC2."""
    return name_block(PROTO, "UpdateSlotFaction_V1")


def m_clear_slot_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04B11."""
    return name_block(PROTO, "ClearSlot_V1")


def m_clear_sub_slot_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04B60."""
    return name_block(PROTO, "ClearSubSlot_V1")


def m_move_slot_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04BAF."""
    return name_block(PROTO, "MoveSlot_V1")


def m_move_sub_slot_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04BFE."""
    return name_block(PROTO, "MoveSubSlot_V1")


def m_swap_sub_slots_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04C4D."""
    return name_block(PROTO, "SwapSubSlots_V1")


def m_kick_player_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04C9C."""
    return name_block(PROTO, "KickPlayer_V1")


def m_set_host_descriptor_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04CEB."""
    return name_block(PROTO, "SetHostDescriptor_V1")


def m_get_game_settings_v1(settings=None, **_kw):
    """``map<u32,u32>`` -- body at 0xE04D41 (reader ``FUN_140DE9460``)."""
    return (name_block(PROTO, "GetGameSettings_V1")
            + map_u32_u32(settings if settings is not None else DEFAULT_SESSION["settings"]))


def m_set_game_settings_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04DF3."""
    return name_block(PROTO, "SetGameSettings_V1")


def m_set_game_setting_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04E42."""
    return name_block(PROTO, "SetGameSetting_V1")


def m_get_game_session_players_v1(players=None, **_kw):
    """``vector<A5_GameSessionPlayer>`` -- body at 0xE04E98 (reader 0xE05050)."""
    return (name_block(PROTO, "GetGameSessionPlayers_V1")
            + game_session_player_list(_many(players, DEFAULT_PLAYER)))


def m_get_game_session_player_v1(player=None, **_kw):
    """one ``A5_GameSessionPlayer`` -- body at 0xE04F35 (reader ``0xE00210``)."""
    return (name_block(PROTO, "GetGameSessionPlayer_V1")
            + game_session_player(_one(player, DEFAULT_PLAYER)))


def m_set_own_player_property_v1(*_a, **_kw):
    """void -- ``jne 0xE0502F`` at 0xE04F8E."""
    return name_block(PROTO, "SetOwnPlayerProperty_V1")


def m_get_own_station_urls_v1(urls=None, **_kw):
    """``list<String>`` -- body at 0xE04FDF (``FUN_140E002B0`` -> 0xDF8B00)."""
    return name_block(PROTO, "GetOwnStationUrls_V1") + string_list(_many(urls, DEFAULT_URL["name"]))


# --------------------------------------------------------------------------
# dispatch tables
# --------------------------------------------------------------------------

METHOD_NAMES = {
    "GetSessions_V1": m_get_sessions_v1,
    "CreateSession_V1": m_create_session_v1,
    "SetSessionParameters_V1": m_set_session_parameters_v1,
    "PreJoinSession_V1": m_pre_join_session_v1,
    "JoinSession_V1": m_join_session_v1,
    "LeaveSession_V1": m_leave_session_v1,
    "SetReadyFlag_V1": m_set_ready_flag_v1,
    "StartSession_V1": m_start_session_v1,
    "GetHostStationUrls_V1": m_get_host_station_urls_v1,
    "GetGameSessionSlots_V1": m_get_game_session_slots_v1,
    "GetUnassignedPlayers_V1": m_get_unassigned_players_v1,
    "SetAiSlot_V1": m_set_ai_slot_v1,
    "SetHumanSlot_V1": m_set_human_slot_v1,
    "SetSubSlot_V1": m_set_sub_slot_v1,
    "UpdateSlotColor_V1": m_update_slot_color_v1,
    "UpdateSlotFaction_V1": m_update_slot_faction_v1,
    "ClearSlot_V1": m_clear_slot_v1,
    "ClearSubSlot_V1": m_clear_sub_slot_v1,
    "MoveSlot_V1": m_move_slot_v1,
    "MoveSubSlot_V1": m_move_sub_slot_v1,
    "SwapSubSlots_V1": m_swap_sub_slots_v1,
    "KickPlayer_V1": m_kick_player_v1,
    "SetHostDescriptor_V1": m_set_host_descriptor_v1,
    "GetGameSettings_V1": m_get_game_settings_v1,
    "SetGameSettings_V1": m_set_game_settings_v1,
    "SetGameSetting_V1": m_set_game_setting_v1,
    "GetGameSessionPlayers_V1": m_get_game_session_players_v1,
    "GetGameSessionPlayer_V1": m_get_game_session_player_v1,
    "SetOwnPlayerProperty_V1": m_set_own_player_property_v1,
    "GetOwnStationUrls_V1": m_get_own_station_urls_v1,
}

#: Every one of the 30 names in the client's tables has a proven *layout*
#: (see the branch table in the module docstring), so nothing is refused here.
UNKNOWN_METHODS = {}

#: Layout [C], payload content [G]: these five carry a generic ``Variant``
#: whose *string/tag* the reference server chose.  The image only proves the
#: encoding (``qstr + u16``), not the value; the builders default to the
#: client's own "absent" Variant and accept ``text=``/``tag=`` overrides.
PROVISIONAL_METHODS = frozenset((
    "CreateSession_V1",
    "SetSessionParameters_V1",
    "PreJoinSession_V1",
    "JoinSession_V1",
    "LeaveSession_V1",
))

#: the 17 setters/actions whose reply is the name block and nothing else
VOID_METHODS = frozenset(name for name, fn in METHOD_NAMES.items()
                         if fn.__doc__ is not None and fn.__doc__.startswith("void"))


def is_void(method):
    """True when the reply body is empty (client jumps straight to its exit)."""
    return method in VOID_METHODS


def build(proto, method, **kwargs):
    """Full reply payload for ``proto::method``, or ``None`` if unknown."""
    if not isinstance(method, str):
        return None
    if str(proto).strip("*").lower() not in ("matchmakingserviceprotocol",
                                             "matchmakingserviceprotocolclient"):
        return None
    fn = METHOD_NAMES.get(method.strip("*"))
    if fn is None:
        return None
    try:
        return fn(**kwargs)
    except TypeError:
        return fn()
