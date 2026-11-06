#!/usr/bin/env python3
"""Anno 2070 mimic server - RMC service layer with JSON persistence.

Implements the RMC services reconstructed from decompilation (see
``/workspace/docs/05-rmc-services.md`` and ``decomp/batch/step*/*``):

  PlayerProfileServiceProtocol  - profiles, ARK items (item box), stats,
                                  settings, key bindings, info mails,
                                  achievements, reputation, shop, ...
  VotingServiceProtocol         - senate / world-patron elections
  WorldEventServiceProtocol     - world events + missions
  BackEndServiceProtocol        - version / maintenance / sandbox

Wire format
-----------
Every reply this module produces is built by :mod:`rv_shapes` (the
package sitting next to this file), which is the single source of truth for
the wire shapes:

* a reply *payload* is ``name_block(proto, method)`` -- ``u16 length INCLUDING
  the NUL``, then ``"Proto::Method*"``, then the NUL, then the method's fields;
* the method name inside a reply carries a TRAILING ``'*'`` (the client builds
  ``decodedName + "*"`` and compares with ``_mbscmp``); requests do not;
* all integers are LITTLE-endian (the stream layer is a plain ``memcpy`` and
  the client's own request frames prove LE on this wire);
* a Quazal ``String`` is ``u16 length EXCLUDING the NUL`` then the bytes;
* a bool is one byte (non-zero == true);
* ``list<T>``/``map<K,V>`` are a ``u32`` count then the items;
* a *void* method's payload is the name block and nothing else.

The RMC envelope around the payload (``u32 size | u16 proto_len_incl_NUL |
"Proto\\0" | u8 type | u8 status | u32 call_id | payload``, type 0 = reply,
status 1 = clean) is produced by :meth:`RmcResponder.handle_payload` so that
every bytes blob handed back to a transport is a complete, self-contained
frame.  The old code returned a bare payload (no envelope, no name block,
big-endian fields); see ``decomp/_adhoc/rmcsuccess.md`` for the grammar and
``decomp/batch/step3_ark/ARK.md`` for the three ARK bugs this fixes.

For callers that own their own envelope -- the websocket path in
``mimic_server`` -- :func:`respond_payload` returns just the state-aware
payload for one ``(proto, method)`` against a store, or ``None`` when this
module has no handler, so the caller can fall back to ``rv_shapes``.

Persistence: whole game state in one JSON file (default
``<data-dir>/mimic_state.json``), loaded at boot, saved atomically on every
mutation.
"""
import json
import traceback
import os
import re
import threading
import time

try:                                    # normal case: server/ is on sys.path
    import rv_shapes as rv
    import content_events
    import content_voting
except ImportError:                     # pragma: no cover - run from elsewhere
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rv_shapes as rv
    import content_events
    import content_voting

if not hasattr(rv, "build"):            # pragma: no cover - defensive
    rv = None


# --------------------------------------------------------------------------
# protocols
# --------------------------------------------------------------------------

PROFILE = "PlayerProfileServiceProtocol"
VOTING = "VotingServiceProtocol"
WORLD = "WorldEventServiceProtocol"
BACKEND = "BackEndServiceProtocol"

#: value returned by BackEndServiceProtocol::GetVersion_V1 (a u32, not a string)
BUILD_NUMBER = 8051

#: sandbox id we advertise (must match the RV config's sandbox name)
SANDBOX_NAME = "127.0.0.1"

#: protocol version list served by GetProtocolVersions_V1
PROTOCOL_VERSIONS = (1,)

#: how many records a single reply may carry (a malformed/hostile count must
#: never turn into a multi-gigabyte allocation)
MAX_RECORDS = 4096


def set_sandbox_name(name):
    """Advertise ``name`` as the sandbox (called by mimic_server at boot)."""
    global SANDBOX_NAME
    if name:
        SANDBOX_NAME = str(name)


# --------------------------------------------------------------------------
# little-endian helpers
# --------------------------------------------------------------------------
# The *writers* all live in rv_shapes (see the module docstring).  Only the
# request side needs readers, and those read LITTLE-endian explicitly.

def u16_at(data, off):
    """u16 LE at ``off`` (0 when out of range)."""
    raw = bytes(data[off:off + 2])
    return int.from_bytes(raw.ljust(2, b"\x00"), "little")


def u32_at(data, off):
    """u32 LE at ``off`` (0 when out of range)."""
    raw = bytes(data[off:off + 4])
    return int.from_bytes(raw.ljust(4, b"\x00"), "little")


class Reader:
    """Bounded cursor over a request body (LITTLE-endian, never raises).

    Every read is total: a truncated request yields zero/empty values instead
    of an exception, because a defaulted reply is far better than a dropped
    session.  ``string()`` accepts both conventions in the wild -- the engine's
    writer (``FUN_141370f90``) stores ``len+1`` with the NUL, while readers use
    ``len`` bytes only -- by taking ``len`` bytes and stripping a trailing NUL.
    """

    __slots__ = ("d", "o")

    def __init__(self, data=b"", off=0):
        try:
            self.d = bytes(data or b"")
        except Exception:                            # pragma: no cover
            self.d = b""
        try:
            off = int(off or 0)
        except Exception:                            # pragma: no cover
            off = 0
        self.o = max(0, min(off, len(self.d)))

    # -- scalars ---------------------------------------------------------
    def u8(self):
        raw = self.d[self.o:self.o + 1]
        self.o += 1
        return raw[0] if raw else 0

    def u16(self):
        raw = self.d[self.o:self.o + 2]
        self.o += 2
        return int.from_bytes(raw.ljust(2, b"\x00"), "little")

    def u32(self):
        raw = self.d[self.o:self.o + 4]
        self.o += 4
        return int.from_bytes(raw.ljust(4, b"\x00"), "little")

    def u64(self):
        raw = self.d[self.o:self.o + 8]
        self.o += 8
        return int.from_bytes(raw.ljust(8, b"\x00"), "little")

    def bool(self):
        return self.u8() != 0

    def string(self):
        n = self.u16()
        raw = self.d[self.o:self.o + n]
        self.o += n
        return raw.rstrip(b"\x00").decode("utf-8", "replace")

    def raw(self, n):
        try:
            n = max(0, int(n))
        except Exception:                            # pragma: no cover
            n = 0
        raw = self.d[self.o:self.o + n]
        self.o += n
        return raw

    def remaining(self):
        return max(0, len(self.d) - self.o)

    # -- containers ------------------------------------------------------
    def count(self, limit=MAX_RECORDS):
        """A container count, clamped to what the buffer can actually hold."""
        n = self.u32()
        return min(n, max(0, int(limit)))

    def u32_list(self, limit=MAX_RECORDS):
        n = min(self.count(limit), self.remaining() // 4)
        return [self.u32() for _ in range(n)]

    def string_list(self, limit=64):
        """list<String> (used by GetLastVoters-like requests)."""
        n = min(self.count(limit), self.remaining() // 2)
        out = []
        for _ in range(n):
            if not self.remaining():
                break
            out.append(self.string())
        return out


# --------------------------------------------------------------------------
# persistent store
# --------------------------------------------------------------------------

DEFAULT_STATE = {
    "players": {},
    # the senate/election document of :mod:`content_voting` -- map key 0 (the
    # one the senate frame hard-codes) plus the MVP bars, no votes yet.  Every
    # value the client renders comes from that module, never from a stat here.
    "voting": dict(content_voting.default_state(),
                   templates=[],          # uploaded templates (kept, not served)
                   multipliers=[]),       # uploaded VoteMultipliers (same)
    "worldEvent": {
        # wire f5 is INVERTED (False == the event is running), so the store's
        # flag is False for the one live event we ship -- see content_events.
        "running": False,
        "name": "",                   # f3 (the client's converter drops it)
        "templates": [],              # uploaded WorldEventTemplates
        "registered": [],             # session pids that called Register_V1
        "completedMissions": [],      # content_events: {a, id, b, ts, pid}
        "reputation": {},             # pid -> points
        "stats": {"listA": [], "listB": [], "c0": 0, "c1": 0, "c2": 0},
    },
    "mail": {"infoMails": {}, "mails": []},
}


def _migrate_state(state):
    """Upgrade an older state document to the content modules' shapes.

    Idempotent, and it never drops a vote, a mission, a template or a
    reputation record -- only the two legacy *content* placeholders that would
    otherwise render wrong:

    * ``voting``: the old ``votingInfo`` carried epoch-second dates (the client
      decodes them as "year 26, hour 24" and prints a blank tag) and ``f6 = 0``,
      which the client reads as "already voted Eco".  It is replaced by the
      ``infos`` document :mod:`content_voting` serves; the vote counts are
      seeded with the MVP bars only when nothing has been voted yet (with
      recorded votes the tally is the truth, so no counts are invented).
    * ``worldEvent``: the old ``active`` flag (True == up) is dropped.  Nothing
      ever set it and it shipped False, so no event could ever appear; the
      content module ships one live event, whose wire flag is inverted
      (``running`` False == running).
    """
    if not isinstance(state, dict):
        return state
    voting = state.get("voting")
    if isinstance(voting, dict):
        if not isinstance(voting.get("infos"), dict):
            voting["infos"] = content_voting.default_elections()
            voting.pop("votingInfo", None)
        if not isinstance(voting.get("counts"), dict) and not voting.get("votes"):
            voting["counts"] = {0: dict(content_voting.DEFAULT_COUNTS)}
    we = state.get("worldEvent")
    if isinstance(we, dict):
        we.pop("active", None)
        if "running" not in we:
            we["running"] = False         # wire f5 inverted: False == running
    return state


_PLAYER_DEFAULTS = {
    "name": "Player",
    "t0": 0, "t1": 0, "t2": 0, "t3": 0, "v5": 0,
    "listA": [], "listB": [], "title": "",
    # PN 148: an ArkItem is {socket_quality, socket_index, item_id, count} - NOT
    # opaque ids. Quality 1/2/3 = sockets A/B/C (the Ark asset declares A:1, B:2,
    # C:3), the index is the 0-based socket ordinal within that group, item_id is
    # an asset GUID from the shipped item DB, count is the stack size. The client
    # DROPS any record whose quality is not 1..3 and resolves item_id against its
    # own DB, so invented ids render as nothing. These six are real, named items
    # (see decomp/_adhoc/ark-ids.md, tools/ark_item_ids.json).
    "arkItems": [
        {"a": 1, "b": 0, "c": 60530, "d": 1},   # A0  Additional Level 1 Socket
        {"a": 2, "b": 0, "c": 60396, "d": 1},   # B0  Comprehensive Low-Wage Regulations
        {"a": 2, "b": 1, "c": 60412, "d": 1},   # B1  Increased Macroeconomic subsidies
        {"a": 3, "b": 0, "c": 60353, "d": 1},   # C0  Low wage regulations
        {"a": 3, "b": 1, "c": 60359, "d": 1},   # C1  Energy Supply Act
        {"a": 3, "b": 2, "c": 60364, "d": 1},   # C2  Nature conservation catalog
    ],
    "stats": [], "settings": {}, "keyBindings": {},
    "infoMails": {}, "achievements": [], "reputation": {},
    "worldStats": {"version": 0, "entries": []},
    "status": 0, "portrait": 0, "icon": 0, "color": 0,
    "activations": [], "helperQuests": [],
    "facebook": {"id": 0, "name": ""},
    "shop": {"status": 0, "items": []},
    "account": {"name": "", "account": ""},
    "counters": {}, "leaderboardTemplates": [], "uniqueKeys": [],
}


LOG = None                 # set by the server (mimic_server) so failures are visible
_FAILED_REPORTED = set()


class Store:
    """Whole game state in one JSON document, saved atomically on mutation."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.state = json.loads(json.dumps(DEFAULT_STATE))  # deep copy
        self.load()

    def load(self):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                if isinstance(disk, dict):
                    for k, v in disk.items():
                        self.state[k] = v
                print(f"[store] loaded {self.path}")
            except Exception as e:                    # noqa: BLE001
                print(f"[store] load failed ({e!r}); starting fresh")
        # a state file written by an older build gets the content-shaped
        # voting/worldEvent sections (_migrate_state); a fresh state already is
        # in that shape, so this is a no-op there.
        _migrate_state(self.state)

    def save(self):
        if not self.path:
            return
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.state, f, indent=1, sort_keys=True)
                os.replace(tmp, self.path)
            except Exception as e:                    # noqa: BLE001
                print(f"[store] save failed ({e!r})")

    # ---- player helpers ----
    def player(self, pid="default"):
        """The mutable player document, created (and persisted) on first use."""
        pid = str(pid or "default")
        players = self.state.setdefault("players", {})
        p = players.get(pid)
        created = not isinstance(p, dict)
        if created:
            p = {}
            players[pid] = p
        now = int(time.time())
        defaults = dict(_PLAYER_DEFAULTS)
        defaults["created"] = now
        defaults["lastLogin"] = now
        if created:
            defaults["t0"] = now
            defaults["t1"] = now
        for key, value in defaults.items():
            if key not in p:
                # a JSON round-trip copies the mutable defaults, so two players
                # never share a list/dict
                p[key] = json.loads(json.dumps(value))
        if created:
            self.save()
        return p


# --------------------------------------------------------------------------
# reply builders (thin wrappers over rv_shapes)
# --------------------------------------------------------------------------

def _reply(proto, method, **fields):
    """Complete reply payload (name block + fields) via :mod:`rv_shapes`.

    Falls back to the name block alone if the package has no shape for the
    method -- never a bare result value, because the client's per-protocol
    reader starts by matching the name.
    """
    payload = None
    if rv is not None:
        try:
            payload = rv.build(proto, method, **fields)
        except Exception:                            # pragma: no cover
            payload = None
    if isinstance(payload, (bytes, bytearray)) and payload:
        return bytes(payload)
    return _name_only(proto, method)


def _name_only(proto, method):
    """The void payload: the name block and nothing else."""
    if rv is not None:
        try:
            return rv.name_block(proto, method)
        except Exception:                            # pragma: no cover
            pass
    nm = f"{proto}::{method}*".encode("ascii") + b"\x00"
    return len(nm).to_bytes(2, "little") + nm      # pragma: no cover


def _ctx_pid(ctx):
    try:
        return str((ctx or {}).get("pid") or "default")
    except Exception:                                # pragma: no cover
        return "default"


# ---- request record readers ----------------------------------------------

def read_ark_items(r):
    """``list<ArkItem>``: u32 count + count x 16 B (4 independent u32 LE).

    ``FUN_140df2490`` reads four u32 into object +8/+0xc/+0x10/+0x14 -- a flat
    16-byte record, *not* two u64 (decomp/batch/step3_ark/ARK.md section 2).
    """
    n = min(r.count(), r.remaining() // 16)
    return [{"a": r.u32(), "b": r.u32(), "c": r.u32(), "d": r.u32()}
            for _ in range(n)]


def read_stats_entries(r):
    """``list<StatsEntry>``: 36 B each -- u32, u32, u32, u64, u64, u64."""
    n = min(r.count(), r.remaining() // 36)
    out = []
    for _ in range(n):
        v = {"a": r.u32(), "b": r.u32(), "c": r.u32(),
             "d": r.u64(), "e": r.u64(), "f": r.u64()}
        out.append(v)
    return out


def _int_keyed(d):
    """Store-side view of a ``map<u32, ...>`` (JSON turns keys into strings)."""
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                continue
    return out


# ==========================================================================
# PlayerProfileServiceProtocol -- 54 methods
# ==========================================================================

def h_set_status(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["status"] = r.u32() if r.remaining() else 0
    store.save()
    return _reply(PROFILE, "SetStatus_V1")


def h_get_status(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetStatus_V1", status=p.get("status", 0))


def h_get_player_profile(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["lastLogin"] = int(time.time())
    return _reply(PROFILE, "GetPlayerProfile_V1",
                  name=p.get("name", "Player"),
                  values=[p.get("t0", 0), p.get("t1", 0), p.get("t2", 0),
                          p.get("t3", 0), p.get("v5", 0)],
                  list_a=p.get("listA", []),
                  list_b=p.get("listB", []),
                  tail=p.get("title", ""))


def h_create_player_profile(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    name = r.string() if r.remaining() else ""
    if name:
        p["name"] = name
    store.save()
    # Result(String + u16) + PlayerProfile + map<u32,bool>
    return _reply(PROFILE, "CreatePlayerProfile_V1",
                  msg="", kind=0,
                  name=p.get("name", "Player"),
                  values=[p.get("t0", 0), p.get("t1", 0), p.get("t2", 0),
                          p.get("t3", 0), p.get("v5", 0)],
                  list_a=p.get("listA", []),
                  list_b=p.get("listB", []),
                  tail=p.get("title", ""),
                  mails=_int_keyed(p.get("infoMails", {})))


def h_does_user_exist(store, r, ctx):
    return _reply(PROFILE, "DoesUserExist_V1", exists=1)


def h_set_portrait(store, r, ctx):
    store.player(_ctx_pid(ctx))["portrait"] = r.u32()
    store.save()
    return _reply(PROFILE, "SetPortrait_V1")


def h_set_icon(store, r, ctx):
    store.player(_ctx_pid(ctx))["icon"] = r.u32()
    store.save()
    return _reply(PROFILE, "SetIcon_V1")


def h_set_color(store, r, ctx):
    store.player(_ctx_pid(ctx))["color"] = r.u32()
    store.save()
    return _reply(PROFILE, "SetColor_V1")


def h_set_player_title(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["title"] = r.string() if r.remaining() else ""
    store.save()
    return _reply(PROFILE, "SetPlayerTitle_V1")


# ---- ARK item box (the headline feature) ---------------------------------

def h_get_ark_items(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetArkItems_V1", items=p.get("arkItems", []))


def h_set_ark_items(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["arkItems"] = read_ark_items(r)
    store.save()
    # FIXED (was: an empty FRAME): the reply is an empty PAYLOAD -- the name
    # block and nothing else -- wrapped by the caller in a normal envelope.
    return _reply(PROFILE, "SetArkItems_V1")


def h_set_ark(store, r, ctx):
    """SetArk_V1: a DELTA against the box, not a replacement (PN 150).

    The client sends the same 16-byte record but reads it as a map entry:
    key = (w0, w1) = (socket_quality, socket_index), value = (w2, w3) =
    (item_id, count), and **w3 == 0 means "skip this entry"** - that is how the
    client signals "no change" (decomp/_adhoc/ark-ids.md, the writer/reader pair
    around FUN_140db2520). Two bugs are fixed here:
      * it REPLACED the whole box with the delta's entries (so a one-socket delta
        wiped the other five sockets);
      * it did not honour w3 == 0, so a "skip" entry overwrote a real stack with
        an item of count 0.
    An empty request is still a no-op (never an accidental clear: that is
    SetArkItems_V1 with an empty list).
    """
    p = store.player(_ctx_pid(ctx))
    delta = read_ark_items(r)
    if not delta:
        return _reply(PROFILE, "SetArk_V1")
    box = {}
    for _it in p.get("arkItems") or []:
        _w = _ark_words(_it)
        box[(_w[0], _w[1])] = _w
    changed = False
    for ent in delta:
        _w = _ark_words(ent)
        if _w[3] == 0:                              # w3 == 0 -> skip
            continue
        box[(_w[0], _w[1])] = _w
        changed = True
    if changed:
        p["arkItems"] = [{"a": w[0], "b": w[1], "c": w[2], "d": w[3]}
                         for w in (box[k] for k in sorted(box))]
        store.save()
    return _reply(PROFILE, "SetArk_V1")


def _ark_words(item):
    """(socket_quality, socket_index, item_id, count) from any spelling.

    The store and the tools use both shapes: dicts with ``a,b,c,d`` (or
    ``w0..w3``) and 4-element sequences ``[quality, index, item_id, count]``.
    """
    if isinstance(item, dict):
        if "a" in item:
            return (int(item.get("a") or 0), int(item.get("b") or 0),
                    int(item.get("c") or 0), int(item.get("d") or 0))
        return (int(item.get("w0") or 0), int(item.get("w1") or 0),
                int(item.get("w2") or 0), int(item.get("w3") or 0))
    seq = list(item or [])
    seq += [0] * (4 - len(seq))
    return tuple(int(v or 0) for v in seq[:4])


# ---- achievements --------------------------------------------------------

def h_get_achievements(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetAchievements_V1", items=p.get("achievements", []))


def h_add_achievement(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    ach_id = r.u32() if r.remaining() >= 4 else 0
    value = r.u64() if r.remaining() >= 8 else int(time.time())
    if not value:
        value = int(time.time())
    p.setdefault("achievements", []).append({"id": ach_id, "value": value})
    store.save()
    return _reply(PROFILE, "AddAchievement_V1", value=value)


# ---- settings / key bindings / info mails ---------------------------------

def h_get_settings(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetSettings_V1", settings=_int_keyed(p.get("settings", {})))


def h_set_setting(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    key, value = r.u32(), r.u32()
    p.setdefault("settings", {})[key] = value
    store.save()
    return _reply(PROFILE, "SetSetting_V1")


def h_get_key_bindings(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetKeyBindings_V1",
                  bindings=_int_keyed(p.get("keyBindings", {})))


def h_set_key_binding(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    key = r.u32()
    p.setdefault("keyBindings", {})[key] = r.u32_list(64)
    store.save()
    return _reply(PROFILE, "SetKeyBinding_V1")


def h_reset_key_bindings(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["keyBindings"] = {}
    store.save()
    return _reply(PROFILE, "ResetKeyBindings_V1")


def h_add_helper_quest(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p.setdefault("helperQuests", []).append(r.u32())
    store.save()
    return _reply(PROFILE, "AddHelperQuest_V1")


def h_reset_helper_quests(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["helperQuests"] = []
    store.save()
    return _reply(PROFILE, "ResetHelperQuests_V1")


def h_get_info_mails(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetInfoMails_V1", mails=_int_keyed(p.get("infoMails", {})))


def h_add_info_mail(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    mail_id = r.u32()
    p.setdefault("infoMails", {})[mail_id] = 0
    store.save()
    return _reply(PROFILE, "AddInfoMail_V1")


def h_set_info_mail_read(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    mail_id, read = r.u32(), r.bool() if r.remaining() else True
    p.setdefault("infoMails", {})[mail_id] = 1 if read else 0
    store.save()
    return _reply(PROFILE, "SetInfoMailRead_V1")


def h_reset_info_mails(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["infoMails"] = {}
    store.save()
    return _reply(PROFILE, "ResetInfoMails_V1")


# ---- stats ---------------------------------------------------------------

def h_get_stats(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetStats_V1", items=p.get("stats", []))


def h_set_stats(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["stats"] = read_stats_entries(r)
    store.save()
    return _reply(PROFILE, "SetStats_V1")


def h_get_world_stats(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    ws = p.get("worldStats") or {}
    return _reply(PROFILE, "GetWorldStats_V1",
                  version=ws.get("version", 0),
                  entries=ws.get("entries", []))


# ---- reputation ----------------------------------------------------------

def h_get_reputation_points(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetReputationPoints_V1",
                  pairs=_int_keyed(p.get("reputation", {})))


def h_are_rep_missing(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "AreReputationPointsMissing_V1",
                  missing=0 if p.get("reputation") else 1)


def h_set_reputation_points(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    key, value = r.u32(), r.u32()
    p.setdefault("reputation", {})[key] = value
    store.save()
    return _reply(PROFILE, "SetReputationPoints_V1")


def h_invalidate_reputation(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["reputation"] = {}
    store.save()
    return _reply(PROFILE, "InvalidateAllReputationPoints_V1")


# ---- facebook / account / unique keys ------------------------------------

def h_get_facebook_account(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    fb = p.get("facebook") or {}
    return _reply(PROFILE, "GetFacebookAccount_V1",
                  account_id=fb.get("id", 0), name=fb.get("name", ""))


def h_set_facebook_account(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["facebook"] = {"id": r.u64(), "name": r.string()}
    store.save()
    return _reply(PROFILE, "SetFacebookAccount_V1")


def h_activate_unique_key(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    key = r.u32()
    keys = p.setdefault("uniqueKeys", [])
    if key not in keys:
        keys.append(key)
        store.save()
    return _reply(PROFILE, "ActivateUniqueKey_V1", keys=keys)


def h_get_account_info(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    acc = p.get("account") or {}
    return _reply(PROFILE, "GetAccountInfo_V1",
                  name=acc.get("name", p.get("name", "")),
                  account=acc.get("account", ""))


def h_initialize_service(store, r, ctx):
    store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "InitializeService_V1")


def h_add_activation(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p.setdefault("activations", []).append(r.u32())
    store.save()
    return _reply(PROFILE, "AddActivation_V1")


# ---- shop ----------------------------------------------------------------

def h_add_shop_item(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    item = r.u32()
    shop = p.setdefault("shop", {"status": 0, "items": []})
    if item not in shop.setdefault("items", []):
        shop["items"].append(item)
        store.save()
    return _reply(PROFILE, "AddShopItem_V1")


def h_remove_shop_item(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    item = r.u32()
    shop = p.setdefault("shop", {"status": 0, "items": []})
    shop["items"] = [x for x in shop.get("items", []) if x != item]
    store.save()
    return _reply(PROFILE, "RemoveShopItem_V1")


def h_list_shop_items(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    shop = p.get("shop") or {}
    return _reply(PROFILE, "ListShopItems_V1",
                  status=shop.get("status", 0), items=shop.get("items", []))


# ---- counters / leaderboards --------------------------------------------

def h_fetch_asset_counter_graph(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    counters = p.get("counters") or {}
    return _reply(PROFILE, "FetchAssetCounterGraph_V1",
                  pairs=_int_keyed(counters), suffix=len(_int_keyed(counters)))


def h_get_leaderboard_templates(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetLeaderboardTemplates_V1",
                  items=p.get("leaderboardTemplates", []))


def h_upload_leaderboard_templates(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["leaderboardTemplates"] = []
    store.save()
    return _reply(PROFILE, "UploadLeaderboardTemplates_V1")


# ---- void uploaders ------------------------------------------------------

def _void_player(proto, method):
    """Handler factory for a void method that only needs the player to exist."""
    def handler(store, r, ctx):
        store.player(_ctx_pid(ctx))
        return _reply(proto, method)
    handler.__name__ = "h_void_" + re.sub(r"[^a-z0-9]+", "_", method.lower())
    return handler


# ==========================================================================
# VotingServiceProtocol -- 8 methods
# ==========================================================================
# The *values* come from :mod:`content_voting` (election document, vote
# counts, voter list, percentage denominator); this section only maps them onto
# the store and the wire shapes of ``rv_shapes.voting``.  One trap governs
# everything here: the senate frame hard-codes ``GetVotingInfo(out, id=0)``, so
# the map in the GetVotingInfos reply MUST contain key 0 --
# ``elections_from_state`` guarantees exactly that.

def _elections(store):
    """The election map the store currently serves (key 0 always present)."""
    return content_voting.elections_from_state(store.state.get("voting"))


def _election_id(store):
    """The one election id every panel reads (``elections_from_state`` -> 0).

    The MVP serves exactly one election and forces its ``Vote()`` id to 0, so
    this is 0 -- but deriving it keeps the handlers honest if that ever grows.
    """
    keys = sorted(_elections(store))
    return keys[0] if keys else 0


def h_get_voting_infos(store, r, ctx):
    """``list<VotingInfo>`` -- the elections the senate screen renders."""
    infos = content_voting.voting_infos(elections=_elections(store))
    return _reply(VOTING, "GetVotingInfos_V1", infos=infos)


def h_vote(store, r, ctx):
    """``Vote_V1``: u32 election id, u32 faction -> a VOID reply.

    The vote is recorded through ``content_voting.apply_vote``, which keeps the
    counts, the voter list and the caller's own vote (VotingInfo f6) in step;
    the client then re-issues GetVoteCount/GetLastVoters by itself.
    """
    election_id = r.u32() if r.remaining() >= 4 else 0
    faction = r.u32() if r.remaining() >= 4 else 0
    voting = store.state.setdefault("voting", {})
    served = _election_id(store)
    if election_id != served:
        # one election in the store: a stale client document that sends another
        # id must not put the vote where no panel will ever read it
        election_id = served
    try:
        content_voting.apply_vote(voting, election_id, faction,
                                  voter=_ctx_pid(ctx))
    except (TypeError, ValueError):
        # a faction outside the proven enum (0/1/2) is a client bug: never
        # corrupt the bars, never fail the call, just record no vote
        pass
    store.save()
    # Vote_V1's reply has NO body at all (the client skips parsing)
    return _reply(VOTING, "Vote_V1")


def h_get_vote_count(store, r, ctx):
    """``map<u32,u32>`` faction -> votes for the one election we serve.

    The request's u32 (the client's election id) is ignored on purpose: the
    store has exactly one election, whose ``Vote()`` id
    ``elections_from_state`` forces to 0, so every call refers to it.
    """
    counts = content_voting.vote_counts(store.state, _election_id(store))
    return _reply(VOTING, "GetVoteCount_V1", counts=counts)


def h_get_voting_stats(store, r, ctx):
    """``list<VotingStats>``: u32 key, u32 denominator, map<u32,u32>.

    ``f1`` is the client's percentage denominator (``100*sum/f1``), so the
    content module guarantees ``f1 > 0`` and ``f1 >= sum`` -- a zero would
    paint ``INT_MIN`` % bars.
    """
    entries = content_voting.voting_stats(store.state, _elections(store))
    return _reply(VOTING, "GetVotingStats_V1", entries=entries)


def h_get_last_voters(store, r, ctx):
    """``list<VoteEntry>``: String voter, u32 faction, u32 per entry.

    Request is ``u32 limit, u32 offset``; recorded votes come back newest
    first, and the content module fills the list from the counts when nobody
    has voted yet so the panel is not blank.
    """
    limit = r.u32() if r.remaining() >= 4 else 10
    if r.remaining() >= 4:
        r.u32()                                   # offset (not needed)
    limit = min(max(0, limit), 64)
    voters = content_voting.last_voters(store.state, _election_id(store), limit)
    return _reply(VOTING, "GetLastVoters_V1", voters=voters)


def h_upload_voting_template(store, r, ctx):
    voting = store.state.setdefault("voting", {})
    template_id = r.u32() if r.remaining() >= 4 else 0
    level = r.u32() if r.remaining() >= 4 else 0
    voting.setdefault("templates", []).append(
        {"id": template_id, "level": level, "ts": int(time.time())})
    store.save()
    return _reply(VOTING, "UploadVotingTemplate_V1")


def h_clear_voting_templates(store, r, ctx):
    store.state.setdefault("voting", {})["templates"] = []
    store.save()
    return _reply(VOTING, "ClearVotingTemplates_V1")


def h_upload_vote_multipliers(store, r, ctx):
    """list<VoteMultiplier> = u32 count + 3 x u32 each (ddl_ser.c:3514-3523)."""
    n = min(r.count(), r.remaining() // 12)
    ms = [{"a": r.u32(), "b": r.u32(), "c": r.u32()} for _ in range(n)]
    store.state.setdefault("voting", {})["multipliers"] = ms
    store.save()
    return _reply(VOTING, "UploadVoteMultipliers_V1")


# ==========================================================================
# WorldEventServiceProtocol -- 12 methods
# ==========================================================================
# Same split as the voting protocol: :mod:`content_events` owns every *value*
# (the event document, the mission records, the stats), this section owns the
# store and the wire shapes of ``rv_shapes.voting``.

def _world_event_state(store):
    """The store's ``worldEvent`` dict (created on demand, never a non-dict)."""
    we = store.state.get("worldEvent")
    if not isinstance(we, dict):
        we = store.state["worldEvent"] = {}
    return we


def _we_owner(record):
    """Session player that completed a mission, or ``""``.

    ``CompleteMission_V1`` stamps ``pid`` on each record it appends (see
    :func:`h_we_complete_mission`), so "own" is the session rather than a name
    parsed out of the request string.  Records written by the old handler -- or
    by hand -- only carry the display name ``a``, hence the fallback.
    """
    if not isinstance(record, dict):
        return ""
    owner = record.get("pid")
    return str(owner) if owner is not None else str(record.get("a", ""))


def h_get_latest_world_event(store, r, ctx):
    """The one live world event, as ``WorldEvent`` (bare, no count prefix).

    ``content_events.latest_event`` carries the two traps: f5 is INVERTED
    (running -> False) and f4 is a bit-packed ``Date``, not epoch seconds.
    """
    event = content_events.latest_event(event=_world_event_state(store))
    return _reply(WORLD, "GetLatestWorldEvent_V1", event=event)


def h_we_register(store, r, ctx):
    we = _world_event_state(store)
    pid = _ctx_pid(ctx)
    if pid not in we.setdefault("registered", []):
        we["registered"].append(pid)
        store.save()
    return _reply(WORLD, "Register_V1")


def h_we_unregister(store, r, ctx):
    we = _world_event_state(store)
    pid = _ctx_pid(ctx)
    we["registered"] = [x for x in we.get("registered", []) if x != pid]
    store.save()
    return _reply(WORLD, "Unregister_V1")


def h_we_complete_mission(store, r, ctx):
    """Request is ``u32 mission index, String "<PlayerName>_<unixtime>"``.

    The reply is a single u32.  ``content_events.complete_mission`` owns the
    record -- including its idempotency per (mission, player) -- and the session
    player is stamped on a *new* record as ``pid`` so GetOwnCompletedMissions
    can tell "own" from "last" without guessing at the request string.
    """
    we = _world_event_state(store)
    mission = r.u32() if r.remaining() >= 4 else 0
    instance = r.string() if r.remaining() else ""
    before = len(we.get("completedMissions") or ())
    result = content_events.complete_mission(we, mission, instance)
    records = we.get("completedMissions")
    if (isinstance(records, list) and len(records) > before
            and isinstance(records[-1], dict)):
        records[-1]["pid"] = _ctx_pid(ctx)
    store.save()
    return _reply(WORLD, "CompleteMission_V1", result=result)


def h_we_own_completed(store, r, ctx):
    """``list<WorldEventCompletedMission>`` for THIS session's player."""
    we = _world_event_state(store)
    pid = _ctx_pid(ctx)
    mine = [rec for rec in (we.get("completedMissions") or ())
            if _we_owner(rec) == pid]
    return _reply(WORLD, "GetOwnCompletedMissions_V1",
                  missions=content_events.completed_missions(
                      {"completedMissions": mine}))


def h_we_last_completed(store, r, ctx):
    """``list<WorldEventCompletedMission>``, newest ``limit`` (request u32)."""
    we = _world_event_state(store)
    limit = r.u32() if r.remaining() >= 4 else 10
    limit = min(max(0, limit), MAX_RECORDS)
    missions = content_events.completed_missions(we)
    return _reply(WORLD, "GetLastCompletedMissions_V1",
                  missions=missions[-limit:] if limit else [])


def h_we_set_visited(store, r, ctx):
    """``SetVisited_V1`` -- marks the event seen (its wire f8)."""
    content_events.set_visited(_world_event_state(store), True)
    store.save()
    return _reply(WORLD, "SetVisited_V1")


def h_we_clear_templates(store, r, ctx):
    _world_event_state(store)["templates"] = []
    store.save()
    return _reply(WORLD, "ClearWorldEventTemplates_V1")


def h_we_upload_template(store, r, ctx):
    """Request is one WorldEventTemplate record; the reply is void.

    The record layout (world_event_template) is known but its *meaning* is not,
    so the raw size is all we keep -- the reply does not echo it either way.
    """
    we = _world_event_state(store)
    we.setdefault("templates", []).append(
        {"ts": int(time.time()), "size": r.remaining()})
    store.save()
    return _reply(WORLD, "UploadWorldEventTemplate_V1")


def h_we_are_rep_missing(store, r, ctx):
    """Request is a u32 (profile id); reply is a bool."""
    we = _world_event_state(store)
    pid = str(r.u32()) if r.remaining() >= 4 else _ctx_pid(ctx)
    missing = not bool((we.get("reputation") or {}).get(pid))
    return _reply(WORLD, "AreReputationPointsMissing_V1", missing=missing)


def h_we_set_reputation(store, r, ctx):
    we = _world_event_state(store)
    pid, points = r.u32(), r.u32()
    we.setdefault("reputation", {})[str(pid)] = points
    store.save()
    return _reply(WORLD, "SetReputationPoints_V1")


def h_we_get_stats(store, r, ctx):
    """WorldEventStats: list<Entry> A, list<Entry> B, u32, u32, u32.

    FIXED (PN 147): this method used to be routed to the *PlayerProfile*
    GetStats handler, which emitted ``u32 + list<StatsEntry>`` -- an entirely
    different shape.  The values now come from
    :func:`content_events.event_stats` (per-player completions in A,
    per-player reputation in B, and the three counters kept consistent with
    them); a hand-written ``stats`` document in the store still wins.
    """
    we = _world_event_state(store)
    explicit = we.get("stats")
    if isinstance(explicit, dict) and (explicit.get("listA") or explicit.get("listB")
                                       or explicit.get("c0") or explicit.get("c1")
                                       or explicit.get("c2")):
        stats = explicit
    else:
        stats = content_events.event_stats(we)
    return _reply(WORLD, "GetStats_V1", stats=stats)


# ==========================================================================
# BackEndServiceProtocol -- 9 methods
# ==========================================================================

def h_backend_get_version(store, r, ctx):
    return _reply(BACKEND, "GetVersion_V1", version=BUILD_NUMBER)


def h_backend_protocol_versions(store, r, ctx):
    return _reply(BACKEND, "GetProtocolVersions_V1", entries=list(PROTOCOL_VERSIONS))


def h_backend_login_allowed(store, r, ctx):
    # String msg + u16 kind: "" with kind 0 is the clean (empty) Result
    return _reply(BACKEND, "CheckLoginAllowed_V1", msg="", kind=0)


def h_backend_maintenance(store, r, ctx):
    return _reply(BACKEND, "IsMaintenanceOngoing_V1", flag=False)


def h_backend_maintenance_time(store, r, ctx):
    return _reply(BACKEND, "GetMaintenanceTime_V1", when=0)


def h_backend_schedule_maintenance(store, r, ctx):
    return _reply(BACKEND, "ScheduleMaintenance_V1")


def h_backend_stop_maintenance(store, r, ctx):
    return _reply(BACKEND, "StopMaintenance_V1")


def h_backend_profiling_units(store, r, ctx):
    return _reply(BACKEND, "GetProfilingUnits_V1", units=())


def h_backend_sandbox(store, r, ctx):
    return _reply(BACKEND, "GetSandboxName_V1", name=SANDBOX_NAME)


# ==========================================================================
# dispatch table:  "Proto::Method" -> f(store, Reader, ctx) -> payload bytes
# ==========================================================================

METHODS = {
    # ---- PlayerProfileServiceProtocol (54) ----
    PROFILE + "::SetStatus_V1": h_set_status,
    PROFILE + "::CreatePlayerProfile_V1": h_create_player_profile,
    PROFILE + "::GetPlayerProfile_V1": h_get_player_profile,
    PROFILE + "::DoesUserExist_V1": h_does_user_exist,
    PROFILE + "::SetPortrait_V1": h_set_portrait,
    PROFILE + "::SetIcon_V1": h_set_icon,
    PROFILE + "::SetColor_V1": h_set_color,
    PROFILE + "::SetPlayerTitle_V1": h_set_player_title,
    PROFILE + "::SetArk_V1": h_set_ark,
    PROFILE + "::AddActivation_V1": h_add_activation,
    PROFILE + "::GetAchievements_V1": h_get_achievements,
    PROFILE + "::AddAchievement_V1": h_add_achievement,
    PROFILE + "::GetSettings_V1": h_get_settings,
    PROFILE + "::SetSetting_V1": h_set_setting,
    PROFILE + "::GetKeyBindings_V1": h_get_key_bindings,
    PROFILE + "::SetKeyBinding_V1": h_set_key_binding,
    PROFILE + "::ResetKeyBindings_V1": h_reset_key_bindings,
    PROFILE + "::AddHelperQuest_V1": h_add_helper_quest,
    PROFILE + "::ResetHelperQuests_V1": h_reset_helper_quests,
    PROFILE + "::GetInfoMails_V1": h_get_info_mails,
    PROFILE + "::AddInfoMail_V1": h_add_info_mail,
    PROFILE + "::SetInfoMailRead_V1": h_set_info_mail_read,
    PROFILE + "::GetStats_V1": h_get_stats,
    PROFILE + "::SetStats_V1": h_set_stats,
    PROFILE + "::GetArkItems_V1": h_get_ark_items,
    PROFILE + "::SetArkItems_V1": h_set_ark_items,
    PROFILE + "::GetStatus_V1": h_get_status,
    PROFILE + "::GetWorldStats_V1": h_get_world_stats,
    PROFILE + "::ResetInfoMails_V1": h_reset_info_mails,
    PROFILE + "::UploadWorldStatsStencils_V1": _void_player(PROFILE, "UploadWorldStatsStencils_V1"),
    PROFILE + "::GetReputationPoints_V1": h_get_reputation_points,
    PROFILE + "::AreReputationPointsMissing_V1": h_are_rep_missing,
    PROFILE + "::SetReputationPoints_V1": h_set_reputation_points,
    PROFILE + "::SolveDailyQuest_V1": _void_player(PROFILE, "SolveDailyQuest_V1"),
    PROFILE + "::UploadFactionRewards_V1": _void_player(PROFILE, "UploadFactionRewards_V1"),
    PROFILE + "::UploadStatsReputation_V1": _void_player(PROFILE, "UploadStatsReputation_V1"),
    PROFILE + "::GetFacebookAccount_V1": h_get_facebook_account,
    PROFILE + "::SetFacebookAccount_V1": h_set_facebook_account,
    PROFILE + "::InvalidateAllReputationPoints_V1": h_invalidate_reputation,
    PROFILE + "::UploadReputationMails_V1": _void_player(PROFILE, "UploadReputationMails_V1"),
    PROFILE + "::ActivateUniqueKey_V1": h_activate_unique_key,
    PROFILE + "::InitializeService_V1": h_initialize_service,
    PROFILE + "::AddShopItem_V1": h_add_shop_item,
    PROFILE + "::RemoveShopItem_V1": h_remove_shop_item,
    PROFILE + "::ListShopItems_V1": h_list_shop_items,
    PROFILE + "::UploadShopItems_V1": _void_player(PROFILE, "UploadShopItems_V1"),
    PROFILE + "::UploadSteamAppIdMapping_V1": _void_player(PROFILE, "UploadSteamAppIdMapping_V1"),
    PROFILE + "::UpdateUPCActivations_V1": _void_player(PROFILE, "UpdateUPCActivations_V1"),
    PROFILE + "::UpdateSteamActivations_V1": _void_player(PROFILE, "UpdateSteamActivations_V1"),
    PROFILE + "::GetAccountInfo_V1": h_get_account_info,
    PROFILE + "::UploadAchievementRewards_V1": _void_player(PROFILE, "UploadAchievementRewards_V1"),
    PROFILE + "::FetchAssetCounterGraph_V1": h_fetch_asset_counter_graph,
    PROFILE + "::UploadLeaderboardTemplates_V1": h_upload_leaderboard_templates,
    PROFILE + "::GetLeaderboardTemplates_V1": h_get_leaderboard_templates,

    # ---- VotingServiceProtocol (8) ----
    VOTING + "::GetVotingInfos_V1": h_get_voting_infos,
    VOTING + "::Vote_V1": h_vote,
    VOTING + "::GetVoteCount_V1": h_get_vote_count,
    VOTING + "::GetVotingStats_V1": h_get_voting_stats,
    VOTING + "::GetLastVoters_V1": h_get_last_voters,
    VOTING + "::UploadVotingTemplate_V1": h_upload_voting_template,
    VOTING + "::ClearVotingTemplates_V1": h_clear_voting_templates,
    VOTING + "::UploadVoteMultipliers_V1": h_upload_vote_multipliers,

    # ---- WorldEventServiceProtocol (12) ----
    WORLD + "::GetLatestWorldEvent_V1": h_get_latest_world_event,
    WORLD + "::Register_V1": h_we_register,
    WORLD + "::Unregister_V1": h_we_unregister,
    WORLD + "::CompleteMission_V1": h_we_complete_mission,
    WORLD + "::GetOwnCompletedMissions_V1": h_we_own_completed,
    WORLD + "::GetLastCompletedMissions_V1": h_we_last_completed,
    WORLD + "::SetVisited_V1": h_we_set_visited,
    WORLD + "::ClearWorldEventTemplates_V1": h_we_clear_templates,
    WORLD + "::UploadWorldEventTemplate_V1": h_we_upload_template,
    WORLD + "::AreReputationPointsMissing_V1": h_we_are_rep_missing,
    WORLD + "::SetReputationPoints_V1": h_we_set_reputation,
    WORLD + "::GetStats_V1": h_we_get_stats,

    # ---- BackEndServiceProtocol (9) ----
    BACKEND + "::GetVersion_V1": h_backend_get_version,
    BACKEND + "::GetProtocolVersions_V1": h_backend_protocol_versions,
    BACKEND + "::CheckLoginAllowed_V1": h_backend_login_allowed,
    BACKEND + "::IsMaintenanceOngoing_V1": h_backend_maintenance,
    BACKEND + "::GetMaintenanceTime_V1": h_backend_maintenance_time,
    BACKEND + "::ScheduleMaintenance_V1": h_backend_schedule_maintenance,
    BACKEND + "::StopMaintenance_V1": h_backend_stop_maintenance,
    BACKEND + "::GetProfilingUnits_V1": h_backend_profiling_units,
    BACKEND + "::GetSandboxName_V1": h_backend_sandbox,
}

#: the void methods -- their payload is EXACTLY the name block and nothing else.
VOID_METHODS = frozenset(
    name for name, fn in METHODS.items()
    if name.split("::", 1)[1] in (
        "SetStatus_V1", "SetPortrait_V1", "SetIcon_V1", "SetColor_V1",
        "SetPlayerTitle_V1", "SetArk_V1", "SetArkItems_V1", "SetSetting_V1",
        "AddActivation_V1",
        "SetKeyBinding_V1", "ResetKeyBindings_V1", "AddHelperQuest_V1",
        "ResetHelperQuests_V1", "AddInfoMail_V1", "SetInfoMailRead_V1",
        "SetStats_V1", "ResetInfoMails_V1", "UploadWorldStatsStencils_V1",
        "SetReputationPoints_V1", "SolveDailyQuest_V1", "UploadFactionRewards_V1",
        "UploadStatsReputation_V1", "SetFacebookAccount_V1",
        "InvalidateAllReputationPoints_V1", "UploadReputationMails_V1",
        "InitializeService_V1", "AddShopItem_V1", "RemoveShopItem_V1",
        "UploadShopItems_V1", "UploadSteamAppIdMapping_V1",
        "UpdateUPCActivations_V1", "UpdateSteamActivations_V1",
        "UploadAchievementRewards_V1", "UploadLeaderboardTemplates_V1",
        # voting / world event
        "Vote_V1", "UploadVotingTemplate_V1", "ClearVotingTemplates_V1",
        "UploadVoteMultipliers_V1", "Register_V1", "Unregister_V1",
        "SetVisited_V1", "ClearWorldEventTemplates_V1",
        "UploadWorldEventTemplate_V1",
        # backend
        "ScheduleMaintenance_V1", "StopMaintenance_V1",
    ))


#: Short names of the methods that CHANGE persistent game state -- the ones
#: which may write through the store and call ``store.save()``.  Everything
#: else is a pure read *of the game data* (it may still lazily create the
#: default player record via ``store.player()``, which is the store's own
#: first-use behaviour, not a write by the method).
_WRITER_SHORT_NAMES = frozenset((
    # PlayerProfile: profile / appearance
    "SetStatus_V1", "CreatePlayerProfile_V1", "SetPortrait_V1", "SetIcon_V1",
    "SetColor_V1", "SetPlayerTitle_V1",
    # the ARK item box (SetArk_V1 only writes when the request carries items)
    "SetArk_V1", "SetArkItems_V1",
    # achievements
    "AddAchievement_V1",
    # settings / key bindings / helper quests / info mails
    "SetSetting_V1", "SetKeyBinding_V1", "ResetKeyBindings_V1",
    "AddHelperQuest_V1", "ResetHelperQuests_V1",
    "AddInfoMail_V1", "SetInfoMailRead_V1", "ResetInfoMails_V1",
    # stats / reputation / account / shop / leaderboards / activations
    "SetStats_V1", "SetReputationPoints_V1",
    "InvalidateAllReputationPoints_V1",
    "SetFacebookAccount_V1", "ActivateUniqueKey_V1", "AddActivation_V1",
    "AddShopItem_V1", "RemoveShopItem_V1", "UploadLeaderboardTemplates_V1",
    # voting
    "Vote_V1", "UploadVotingTemplate_V1", "ClearVotingTemplates_V1",
    "UploadVoteMultipliers_V1",
    # world events
    "Register_V1", "Unregister_V1", "CompleteMission_V1", "SetVisited_V1",
    "ClearWorldEventTemplates_V1", "UploadWorldEventTemplate_V1",
))

#: "Proto::Method" names from :data:`_WRITER_SHORT_NAMES` (see the note there).
WRITER_METHODS = frozenset(
    name for name in METHODS if name.split("::", 1)[1] in _WRITER_SHORT_NAMES)


# --------------------------------------------------------------------------
# payload seam: one state-aware payload, no envelope
# --------------------------------------------------------------------------
# The websocket path (mimic_server.rv_method_payload) and the PRUDP transport
# both need the *payload* only -- the name block plus the method's fields --
# and they build their own envelope around it.  rv_shapes' builders are
# stateless, so answering from them alone gives an empty ARK box, a zeroed
# profile, no info mails, ... .  respond_payload() closes that gap: it runs the
# very same handlers this module's responder runs, against the live store, and
# hands back just the payload.

class _StateStore:
    """Adapts ``any object with .state`` to the ``Store`` surface used here.

    ``mimic_server`` passes a real :class:`Store`, but a tool or a test may
    pass a minimal stand-in that only has ``.state`` (the shape
    ``conquest_http`` documents).  The adapter shares the wrapped ``state``
    dict and reuses :class:`Store` for the player defaults, so there is
    exactly one implementation of them; ``save()`` is forwarded to the
    wrapped object when it has one (and is a no-op otherwise).
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped
        self.state = wrapped.state
        self._inner = Store(None)        # in-memory: its save() is a no-op
        self._inner.state = self.state    # ... and shares the *same* dict

    def player(self, pid="default"):
        return self._inner.player(pid)

    def save(self):
        fn = getattr(self._wrapped, "save", None)
        if callable(fn):
            fn()


_DEFAULT_STORE = None
_DEFAULT_STORE_LOCK = threading.Lock()


def _default_store():
    """The process-wide in-memory store used when ``store`` is None.

    Its path is ``None``, so :meth:`Store.save` never touches the disk: a
    fallback call can neither create nor modify the server's state file.  The
    websocket path should pass ``mimic_server.ARK_STORE`` (the same store the
    PRUDP responder uses) -- this is only the safe default for callers that
    have no store handy.
    """
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        with _DEFAULT_STORE_LOCK:
            if _DEFAULT_STORE is None:
                _DEFAULT_STORE = Store(None)
    return _DEFAULT_STORE


def _coerce_store(store):
    """A store with ``.state``/``.player()``/``.save()`` from ``store``.

    Accepts a :class:`Store`, any object exposing that triple (used as-is), any
    object with just a ``.state`` dict (wrapped in :class:`_StateStore`), and
    ``None`` (the in-memory default).  Raises ``TypeError`` for anything else;
    ``respond_payload`` turns that into ``None``.
    """
    if store is None:
        return _default_store()
    state = getattr(store, "state", None)
    if not isinstance(state, dict):
        raise TypeError("store has no .state dict: %r" % (type(store),))
    if callable(getattr(store, "player", None)) and callable(
            getattr(store, "save", None)):
        return store
    return _StateStore(store)


def respond_payload(proto, method, store=None, body=b""):
    """Payload bytes (name block + fields, little-endian) for one RV method,
    using the store for state.

    This is the state-aware sibling of ``rv_shapes.build``: same shape, same
    handlers as :class:`RmcResponder`/the PRUDP path, but driven by the live
    store instead of the stateless defaults.  The caller wraps the result in
    whatever envelope it owns; nothing here adds one.

    ``proto``  protocol name, e.g. ``"PlayerProfileServiceProtocol"``.
    ``method`` short method name, e.g. ``"GetArkItems_V1"``.  A full
    ``"Proto::Method"`` (or a name with the reply's trailing ``'*'``) is
    accepted too and normalised.
    ``store``  a :class:`Store`, any object with ``.state`` (+ optional
    ``.player()``/``.save()``), or ``None`` for the in-memory default store.
    ``body``   the request's parameter bytes, when the caller has them
    (default: empty).  No reply SHAPE depends on them, so an empty body still
    produces a complete document -- but the writer methods do read them to
    decide *what* to write, and an empty body means "write nothing": e.g.
    ``SetArkItems_V1`` with no body CLEARS the ARK box.  Pass the parameters
    for any method in :data:`WRITER_METHODS`.

    Returns the payload bytes, or ``None`` when this module has no handler for
    ``(proto, method)`` -- and also when the handler itself fails -- so the
    caller can fall back to ``rv_shapes.build``'s stateless document.  It never
    raises.

    The handler is the same one the PRUDP responder runs, fed a :class:`Reader`
    over ``body`` (empty by default), so what comes back is the store's current
    state -- the same bytes :meth:`RmcResponder.dispatch` returns for the same
    input.  Methods in :data:`WRITER_METHODS` may write (and call
    ``store.save()``); with the default empty body such a call is still a valid
    request that writes zeroes -- ``SetArkItems_V1`` clears the ARK box,
    ``SetStatus_V1`` sets status 0, ``AddAchievement_V1`` appends id 0 with the
    current time.  Every other method only reads (though any of them may lazily
    create the default player record through the store).
    """
    try:
        proto = str(proto or "")
        method = str(method or "").rstrip("*")
        if "::" in method:                  # a full "Proto::Method" was passed
            head, method = method.rsplit("::", 1)
            if not proto or "::" in proto:
                proto = head
        if "::" in proto:                  # a full "Proto::Method" in proto
            proto = proto.rsplit("::", 1)[0]
        if not proto or not method:
            return None
        handler = METHODS.get(proto + "::" + method)
        if handler is None:
            return None
        try:
            request = Reader(bytes(body or b""))
        except Exception:                            # noqa: BLE001
            request = Reader(b"")
        payload = handler(_coerce_store(store), request, {"pid": "default"})
        if isinstance(payload, (bytes, bytearray)) and payload:
            return bytes(payload)
        return None
    except Exception:                                # noqa: BLE001
        _payload_failure(proto, method)
        return None


def _payload_failure(proto, method):
    """Never let a swallowed handler exception hide again (PN 146).

    The seam above must not raise into the reply path, but returning None for a
    handler that EXISTS is a real bug signal: the caller silently falls back to
    the stateless document and the player sees an empty box. So report it once
    per (proto, method) through whatever logger the server installed.
    """
    key = (proto, method)
    if key in _FAILED_REPORTED:
        return
    _FAILED_REPORTED.add(key)
    msg = "respond_payload handler failed for %s::%s:\n%s" % (
        proto, method, traceback.format_exc())
    if LOG is not None:
        try:
            LOG(msg)
            return
        except Exception:                            # noqa: BLE001
            pass
    try:
        print(msg, flush=True)
    except Exception:                                # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# request-frame parsing
# --------------------------------------------------------------------------

def parse_request_frame(data):
    """``(proto, call_id, next_off)`` for an RMC *request* frame, or ``None``.

    Accepted forms (both proven from the client's own frames):

    * call   ``u32 size | u16 plen "Proto\\0" | u8 1 | u32 call_id | payload``
    * reply  ``u32 size | u16 plen "Proto\\0" | u8 0 | u8 1 | u32 call_id | payload``

    ``size`` counts the bytes after the u32 itself.  ``plen`` INCLUDES the NUL.
    Anything that does not parse returns ``None`` -- callers then fall back to
    the method name token alone (call id 0).
    """
    try:
        data = bytes(data or b"")
    except Exception:                                # pragma: no cover
        return None
    if len(data) < 12:
        return None
    declared = u32_at(data, 0)
    if declared < 8 or declared > len(data) + 8:     # tolerate sloppy sizing
        return None
    plen = u16_at(data, 4)
    if plen < 2 or plen > 128:
        return None
    end = 6 + plen
    if end > len(data):
        return None
    raw = data[6:end]
    if raw[-1:] != b"\x00":
        return None
    try:
        proto = raw[:-1].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not proto or any(ord(c) < 0x21 or ord(c) > 0x7E for c in proto):
        return None
    off = end
    if off >= len(data):
        return None
    kind = data[off]
    off += 1
    if kind == 1:                                    # a call: no status byte
        if off + 4 > len(data):
            return None
        call_id = u32_at(data, off)
        off += 4
    elif kind == 0:                                  # a reply: status then call
        if off + 1 > len(data):
            return None
        if data[off] != 1:                           # 0 = message form, skip
            return None
        off += 1
        if off + 4 > len(data):
            return None
        call_id = u32_at(data, off)
        off += 4
    else:
        return None
    return proto, call_id, off


def build_reply_frame(proto, call_id, payload):
    """Wrap a reply payload in the RMC envelope (little-endian, status 1).

    ``u32 size | u16 plen (INCL NUL) | "Proto\\0" | u8 0 | u8 1 | u32 call_id |
    payload`` -- type 0 = response, status 1 = a clean result (no message).
    Note the two different "ok" meanings: the envelope's ``status 1`` means
    clean, while an embedded payload ``Result`` has ``kind 0`` for ok.
    """
    name = (str(proto or "")).encode("ascii", "replace") + b"\x00"
    if not name[:-1]:
        name = b"Core\x00"
    try:
        payload = bytes(payload or b"")
    except Exception:                                # pragma: no cover
        payload = b""
    inner = (len(name).to_bytes(2, "little") + name
             + b"\x00"                               # type 0 = reply
             + b"\x01"                               # status 1 = clean
             + (int(call_id) & 0xFFFFFFFF).to_bytes(4, "little")
             + payload)
    return len(inner).to_bytes(4, "little") + inner


#: every method name, as the bytes a request frame carries (no trailing '*')
_NAME_TOKENS = None
_NAME_TOKEN_COUNT = -1


def _token_re():
    """Compiled alternation of every known method name (longest first).

    Built lazily and rebuilt if the dispatch table changes size, so importing
    this module has no side effects on a mutated table.
    """
    global _NAME_TOKENS, _NAME_TOKEN_COUNT
    if _NAME_TOKENS is None or _NAME_TOKEN_COUNT != len(METHODS):
        names = sorted(METHODS, key=lambda n: (-len(n), n))
        pattern = b"|".join(re.escape(n.encode("ascii")) for n in names)
        _NAME_TOKENS = re.compile(pattern)
        _NAME_TOKEN_COUNT = len(METHODS)
    return _NAME_TOKENS


def find_method(data):
    """``(offset, "Proto::Method")`` of the first known method name, or None."""
    m = _token_re().search(data)
    if m is None:
        return None
    return m.start(), m.group(0).decode("ascii")


# --------------------------------------------------------------------------
# responder
# --------------------------------------------------------------------------

class RmcResponder:
    """Scans transport payloads for RMC method names and answers them.

    ``handle_payload`` returns a COMPLETE reply frame: the RMC envelope plus
    the ``rv_shapes`` payload.  That is the fix for ARK requests arriving
    through the token-scan path -- they used to get a bare payload with no
    envelope, which the client reads as a new *call* (and NAKs).
    """

    def __init__(self, store, logfn=None):
        self.store = store
        self.log = logfn or (lambda *a, **k: None)
        self.counters = {}
        self.last_frame = None

    # -- one method, by name -------------------------------------------
    def dispatch(self, name, body=b"", ctx=None):
        """Payload bytes for ``"Proto::Method"`` (name block included)."""
        handler = METHODS.get(name)
        if handler is None:
            return None
        self.counters[name] = self.counters.get(name, 0) + 1
        try:
            proto, method = name.split("::", 1)
        except ValueError:                           # pragma: no cover
            proto, method = "", name
        request = Reader(body)
        try:
            payload = handler(self.store, request, ctx if ctx is not None
                              else {"pid": "default"})
        except Exception as e:                       # noqa: BLE001
            self.log(f"RMC handler {name} failed: {e!r} (body {bytes(body)[:64].hex()})")
            payload = None
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            payload = _name_only(proto, method)
        return bytes(payload)

    # -- one frame ------------------------------------------------------
    def handle_request(self, proto, method, body=b"", call_id=0, ctx=None):
        """Full reply frame for an already-parsed request (no scanning)."""
        name = f"{proto}::{method}"
        payload = self.dispatch(name, body, ctx)
        if payload is None:
            return None
        frame = build_reply_frame(proto, call_id, payload)
        self.last_frame = (name, call_id, len(payload), len(frame))
        return frame

    def handle_payload(self, data, send=None, origin=""):
        """Scan ``data`` for a method name and return a complete reply frame.

        ``send(bytes)`` is called with the frame when given (the old
        signature); the frame is also returned.  ``None`` means "not an RMC
        request we know".
        """
        try:
            data = bytes(data or b"")
        except Exception:                            # pragma: no cover
            return None
        hit = find_method(data)
        if hit is None:
            return None
        off, name = hit
        try:
            proto, method = name.split("::", 1)
        except ValueError:                           # pragma: no cover
            return None
        envelope = parse_request_frame(data)
        call_id = 0
        if envelope is not None and envelope[2] <= off:
            proto = envelope[0] or proto     # echo the request's protocol name
            call_id = envelope[1]
        body_off = off + len(name)
        if data[body_off:body_off + 1] == b"\x00":
            body_off += 1                     # the name's NUL is part of the name
        body = data[body_off:]
        ctx = {"pid": "default", "origin": origin}
        payload = self.dispatch(name, body, ctx)
        if payload is None:                           # pragma: no cover
            return None
        frame = build_reply_frame(proto, call_id, payload)
        self.last_frame = (name, call_id, len(payload), len(frame))
        self.log(f"RMC {name} call={call_id} -> {len(payload)}B payload in "
                 f"{len(frame)}B frame")
        if send is not None:
            try:
                send(frame)
            except Exception as e:                   # noqa: BLE001
                self.log(f"RMC send failed for {name}: {e!r}")
        return frame


def open_store(data_file):
    """Open (and create on demand) the JSON state file."""
    return Store(data_file)
