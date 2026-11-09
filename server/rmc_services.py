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
import shutil
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
MAIL = "MailServiceProtocol"
CHAT = "ChatServiceProtocol"
FRIENDS = "FriendsServiceProtocol"
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


#: Every client RMC **request body** starts with a u32 ``kind`` word.  ``0``
#: means "the arguments follow immediately"; **N > 0 means "N type names are
#: declared first"** - the reply (or argument) types the client expects, each as
#: a Quazal String followed by a u16 flag (1).  Reading from offset 0 shifted
#: every argument by one word: ``Vote_V1``'s ``(id, faction)`` arrived as
#: ``(0, id)``, so a council vote (id 1001) was read as faction 1001 and
#: ``GetVoteCount_V1`` always saw id 0.
#:
#: Both shapes are in the live captures (2026-11-08 session, 349 log lines):
#:
#:   00 00 00 00 | 19 74 0f 00                     SetStatus_V1 (kind 0, args)
#:   00 00 00 00 | e9 03 00 00                     GetVoteCount_V1 (id 1001)
#:   01 00 00 00 | 08 00 "ArkItem\0" | 01 00          GetArkItems_V1 (no args)
#:   01 00 00 00 | 0a 00 "VoteEntry\0" | 01 00 | e9 03 00 00 | 0a 00 00 00
#:                                                GetLastVoters_V1 (id, count)
#:   02 00 00 00 | "WorldEventStats\0" | 01 00 | "WorldEventStatsEntry\0" | 01 00
#:                                                WorldEvent GetStats_V1 (nested)
#:
#: So the type block must be skipped, not just the kind word: with only a 4-byte
#: skip, ``GetLastVoters_V1`` read the length of the string "VoteEntry" as its
#: election id and the letters "Vote" as its limit.
_REQUEST_PREFIX = 4

#: Guard against a garbage count turning into a long parse.
_MAX_DECLARED_TYPES = 16

#: (kind, declared types) already logged (see :func:`request_reader`).
_SEEN_REQUEST_KINDS = set()


def _skip_declared_types(data):
    """``(offset_of_the_arguments, [type names])`` for one request body.

    A body whose declared-type block does not parse cleanly is treated as
    "arguments start right after the kind word", so a shape we have not seen can
    never move the cursor into nonsense.
    """
    kind = int.from_bytes(data[:_REQUEST_PREFIX], "little")
    names = []
    if kind == 0:
        return _REQUEST_PREFIX, names
    if kind > _MAX_DECLARED_TYPES:
        return _REQUEST_PREFIX, names
    pos = _REQUEST_PREFIX
    for _ in range(kind):
        if pos + 2 > len(data):
            return _REQUEST_PREFIX, []
        size = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2
        if size < 2 or pos + size > len(data):
            return _REQUEST_PREFIX, []
        raw = data[pos:pos + size]
        pos += size
        if pos + 2 <= len(data):                 # the per-entry flag (always 1)
            pos += 2
        try:
            names.append(raw.rstrip(b"\x00").decode("ascii"))
        except UnicodeDecodeError:               # pragma: no cover
            return _REQUEST_PREFIX, []
    return pos, names


def request_reader(body=b""):
    """A :class:`Reader` over one request body, positioned at its arguments.

    Use this for *wire* bodies (what the client actually sent): it skips the
    u32 kind word and, when the kind declares types, the declared-type block in
    front of the arguments.  A body shorter than the kind word is read from 0, so
    a hand-built argument-only body still parses instead of reading nothing.

    The declared type names are logged once per ``(kind, names)`` - they are the
    client telling us which reply type it expects, which is free ground truth for
    the shape modules.
    """
    try:
        data = bytes(body or b"")
    except Exception:                                # pragma: no cover
        data = b""
    if len(data) < _REQUEST_PREFIX:
        return Reader(data)
    offset, names = _skip_declared_types(data)
    if names or int.from_bytes(data[:_REQUEST_PREFIX], "little"):
        key = (int.from_bytes(data[:_REQUEST_PREFIX], "little"), tuple(names))
        if key not in _SEEN_REQUEST_KINDS:
            _SEEN_REQUEST_KINDS.add(key)
            if LOG is not None:
                LOG("request declares %d type(s) %r (kind word %d)"
                    % (len(names), names, key[0]))
    return Reader(data, offset)


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

    def rest(self):
        """Every byte from the cursor to the end (never raises)."""
        raw = self.d[self.o:]
        self.o = len(self.d)
        return raw

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
    healed = 0
    voting = state.get("voting")
    if isinstance(voting, dict):
        if not isinstance(voting.get("infos"), dict):
            voting["infos"] = content_voting.default_elections()
            voting.pop("votingInfo", None)
        # every served slot must own a document: a save written before the
        # World Council slot existed has only infos["0"], which made the
        # council's own vote (f5) unrecoverable.  heal_infos() creates the
        # missing record and refills the own vote from the durable vote log.
        try:
            fixed = content_voting.heal_infos(voting)
        except Exception as exc:                     # noqa: BLE001
            fixed = 0
            print(f"[store] voting heal failed ({exc!r}); the votes themselves "
                  f"are untouched")
        if fixed:
            healed += int(fixed)
            print(f"[store] repaired {fixed} voting document field(s) from the "
                  f"vote log")
        if not isinstance(voting.get("counts"), dict) and not voting.get("votes"):
            voting["counts"] = {0: dict(content_voting.DEFAULT_COUNTS)}
    we = state.get("worldEvent")
    if isinstance(we, dict):
        we.pop("active", None)
        if "running" not in we:
            we["running"] = False         # wire f5 inverted: False == running
        for key, default in (("day", None), ("a", content_events.EVENT_INSTANCE_ID),
                             ("b", content_events.MISSION_SET_INDEX),
                             ("since", 0)):
            we.setdefault(key, default)
    # an older state file stored the reputation map as a *list*; writing to it
    # then raised IndexError inside SetReputationPoints and the client's points
    # were dropped (it saw a clean void reply and believed they were saved)
    # the six ARK items a fresh profile is seeded with are ours, not the
    # player's: tag them so a mission reward may replace one when every socket
    # of its group is full (see _grant_ark_reward)
    seed_ids = {(1, 0, 60530), (2, 0, 60396), (2, 1, 60412),
                (3, 0, 60353), (3, 1, 60359), (3, 2, 60364)}
    players = state.get("players")
    if isinstance(players, dict):
        for doc in players.values():
            if not isinstance(doc, dict):
                continue
            box = doc.get("arkItems")
            if isinstance(box, list):
                for item in box:
                    if not isinstance(item, dict) or "src" in item:
                        continue
                    key = (int(item.get("a") or 0), int(item.get("b") or 0),
                           int(item.get("c") or 0))
                    if key in seed_ids:
                        item["src"] = "seed"
            mails = doc.get("infoMails")
            if isinstance(mails, dict) and any(isinstance(k, str) for k in mails):
                doc["infoMails"] = {int(k): v for k, v in mails.items()
                                    if str(k).lstrip("-").isdigit()}
            # profile: the old build stored the title as a String in "title"
            # (it is a u32 asset id) and kept creation times in the slots the
            # client reads as portrait/icon - move both out of the way
            if isinstance(doc.get("title"), str):
                if doc["title"] and not doc.get("titleText"):
                    doc["titleText"] = doc["title"]
                doc["title"] = 0
            for stale in ("t0", "t1", "t2", "t3", "v5"):
                doc.pop(stale, None)
            for key in PROFILE_VALUE_FIELDS:
                if not isinstance(doc.get(key), int):
                    try:
                        doc[key] = int(doc.get(key) or 0)
                    except (TypeError, ValueError):
                        doc[key] = 0
            rep = doc.get("reputation")
            if not isinstance(rep, dict):
                doc["reputation"] = {}
            elif rep and "reputationVersion" not in doc:
                # a save from before the version pair: announce the map once, so
                # the first login after this upgrade makes the client refetch
                # (otherwise it keeps its own zeros and the career page stays
                # empty although the points are right here)
                doc["reputationVersion"] = 1
                doc["reputationSeen"] = 0
            else:
                # JSON turns the u32 keys into strings; a mix of both makes
                # store.save()'s sort_keys raise TypeError and lose the write
                doc["reputation"] = {int(k): v for k, v in rep.items()
                                     if str(k).lstrip("-").isdigit()}
    # the caller (load) persists the document when this reports a repair, so a
    # healed save is written back instead of being re-healed every start
    state["_healed"] = int(healed)
    return state


_PLAYER_DEFAULTS = {
    "name": "Player",
    # The five u32 of the PlayerProfile wire record are NOT timestamps: the
    # client's completion callback (FUN_140db3070) fills a vector with them in
    # wire order and exposes it by index to SetProfileValue(kind), where
    # kind 0..4 = Portrait, Icon, Colour, Title, Ark (the setter switch
    # FUN_140daaeb0 sends SetPortrait/SetIcon/SetColor/SetPlayerTitle/SetArk).
    # Sending unix seconds here (what t0/t1 used to be) made the client resolve
    # a nonsense portrait GUID and show the default picture.
    "portrait": 0, "icon": 0, "color": 0, "title": 0, "ark": 0,
    "listA": [], "listB": [],
    # the 9th wire field is an unidentified wstring; the player's *title* is a
    # u32 asset id above, so this is kept apart from it
    "titleText": "",
    # PN 148: an ArkItem is {socket_quality, socket_index, item_id, count} - NOT
    # opaque ids. Quality 1/2/3 = sockets A/B/C (the Ark asset declares A:1, B:2,
    # C:3), the index is the 0-based socket ordinal within that group, item_id is
    # an asset GUID from the shipped item DB, count is the stack size. The client
    # DROPS any record whose quality is not 1..3 and resolves item_id against its
    # own DB, so invented ids render as nothing. These six are real, named items
    # (see decomp/_adhoc/ark-ids.md, tools/ark_item_ids.json).
    "arkItems": [
        # "src": "seed" marks these as OUR starter items, not the player's: a
        # mission reward may replace one when its socket group is full.  Anything
        # without src (i.e. placed by the player or the client) is never touched.
        {"a": 1, "b": 0, "c": 60530, "d": 1, "src": "seed"},   # A0 Additional Level 1 Socket
        {"a": 2, "b": 0, "c": 60396, "d": 1, "src": "seed"},   # B0 Low-Wage Regulations
        {"a": 2, "b": 1, "c": 60412, "d": 1, "src": "seed"},   # B1 Macroeconomic subsidies
        {"a": 3, "b": 0, "c": 60353, "d": 1, "src": "seed"},   # C0 Low wage regulations
        {"a": 3, "b": 1, "c": 60359, "d": 1, "src": "seed"},   # C1 Energy Supply Act
        {"a": 3, "b": 2, "c": 60364, "d": 1, "src": "seed"},   # C2 Nature conservation catalog
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


#: Bumped when the shape of the saved document changes in a way a reader must
#: know about.  Written into every save (``"schema": N``) so a future build can
#: migrate instead of guessing.
STATE_SCHEMA = 2


class Store:
    """Whole game state in one JSON document, saved atomically on mutation.

    On disk: ``state.json`` (the live save), ``state.json.bak`` (the previous
    one, written once, as a safety net) and ``state.json.tmp`` (only while a
    save is in flight).
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.state = json.loads(json.dumps(DEFAULT_STATE))  # deep copy
        #: True when the live file was unreadable and the backup was used
        self.recovered = False
        self.load()

    def load(self):
        """Read the save, falling back to ``.bak`` when the live file is bad.

        A single unreadable ``state.json`` (a half-written file from a dead
        disk, a hand edit, a crashing editor) used to reset the state to the
        defaults - and the next save then *overwrote* the player's progress with
        an empty document.  The backup is the previous successful save, so it is
        strictly better than an empty slate; say loudly that it happened.
        """
        for candidate, is_backup in ((self.path, False),
                                     ((self.path or "") + ".bak", True)):
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                if not isinstance(disk, dict):
                    raise ValueError("the save is not a JSON object")
            except Exception as e:                    # noqa: BLE001
                if is_backup:
                    print(f"[store] {candidate} is unreadable too ({e!r}); "
                          f"starting from the defaults")
                else:
                    print(f"[store] {candidate} is unreadable ({e!r}); "
                          f"trying the backup")
                continue
            for k, v in disk.items():
                self.state[k] = v
            if is_backup:
                self.recovered = True
                print(f"[store] RECOVERED from {candidate} (the live save was "
                      f"unreadable); the next save repairs the live file")
            else:
                print(f"[store] loaded {candidate}")
            break
        # a state file written by an older build gets the content-shaped
        # voting/worldEvent sections (_migrate_state); a fresh state already is
        # in that shape, so this is a no-op there.
        _migrate_state(self.state)
        healed = int(self.state.pop("_healed", 0) or 0)
        if healed:
            # write the repair back: the file itself must carry the council's
            # own vote, not just this process's memory of it
            self.save()

    def save(self):
        """Write the whole document atomically, keeping one previous copy.

        ``state.json`` is the player's only save: a truncated or half-written
        file would lose the ARK box, the votes, the career points and the event
        progress at once.  So: write ``.tmp``, fsync it, copy the *previous*
        good file to ``.bak`` (first run only), then ``os.replace`` the new one
        into place.  A crash can therefore never leave a partial file behind.
        """
        if not self.path:
            return
        with self.lock:
            tmp = self.path + ".tmp"
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                self.state["schema"] = STATE_SCHEMA
                with open(tmp, "w", encoding="utf-8") as f:
                    try:
                        json.dump(self.state, f, indent=1, sort_keys=True)
                    except TypeError:
                        # sort_keys compares the keys, and a document can hold
                        # an int key and its JSON string beside each other (a
                        # mail id read from disk as "0" next to an int one).
                        # Losing the whole save over the *order* of the keys is
                        # far worse than an unsorted file.
                        f.seek(0)
                        f.truncate()
                        json.dump(self.state, f, indent=1, sort_keys=False)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                # rotate the backup on EVERY save: .bak is then always "the
                # last known good file", not "the file from the first run"
                if os.path.isfile(self.path):
                    try:
                        shutil.copy2(self.path, self.path + ".bak")
                    except OSError:
                        pass
                os.replace(tmp, self.path)
                self.saves = getattr(self, "saves", 0) + 1
                return True
            except Exception as e:                    # noqa: BLE001
                print(f"[store] save FAILED ({e!r}) - the previous {self.path} "
                      f"is intact and .bak keeps the save before it")
                return False

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


def _as_int(value, fallback):
    """``int(value)`` or ``fallback`` - for values read out of JSON/records."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


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


#: The five u32 of the PlayerProfile record, in wire order (+0x10..+0x20).
PROFILE_VALUE_FIELDS = ("portrait", "icon", "color", "title", "ark")


def _profile_document(player_doc):
    """The ``PlayerProfile`` fields this server serves, from a player record.

    Field order is the client's own (proven):
    portrait, icon, colour, title, ark - then the two ``list<u32>`` and the
    unidentified trailing string.  Everything is opaque data from the client's
    point of view (asset GUIDs / ids), so it is echoed, never interpreted.
    """
    def as_int(key):
        try:
            return int(player_doc.get(key) or 0) & 0xFFFFFFFF
        except (TypeError, ValueError):
            return 0

    return {
        "name": player_doc.get("name", "Player"),
        "values": [as_int(key) for key in PROFILE_VALUE_FIELDS],
        "list_a": player_doc.get("listA", []),
        "list_b": player_doc.get("listB", []),
        "tail": str(player_doc.get("titleText") or ""),
    }


def _set_profile_value(store, ctx, key, value):
    """Store one profile u32, refusing a zero that would erase a real value.

    The client pushes all five values right after login (observed:
    SetArk, SetIcon, SetColor, SetPortrait, SetPlayerTitle).  If our profile
    reply was wrong or simply arrived late, that push carries zeros and would
    wipe a portrait/colour the player had chosen - so a zero never overwrites a
    non-zero.  There is no "reset to default" in the UI, so nothing is lost.
    """
    p = store.player(_ctx_pid(ctx))
    try:
        value = int(value) & 0xFFFFFFFF
    except (TypeError, ValueError):
        value = 0
    current = 0
    try:
        current = int(p.get(key) or 0) & 0xFFFFFFFF
    except (TypeError, ValueError):
        current = 0
    if value == 0 and current:
        if LOG is not None:
            LOG("profile: ignored a zero %s (stored %d)" % (key, current))
        return current
    p[key] = value
    store.save()
    if LOG is not None and value != current:
        LOG("profile: %s %d -> %d" % (key, current, value))
    return value


def h_get_player_profile(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["lastLogin"] = int(time.time())
    return _reply(PROFILE, "GetPlayerProfile_V1",
                  **_profile_document(p))


def h_create_player_profile(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    name = r.string() if r.remaining() else ""
    if name:
        p["name"] = name
    store.save()
    # Result(String + u16) + PlayerProfile + map<u32,bool>
    return _reply(PROFILE, "CreatePlayerProfile_V1",
                  msg="", kind=0, mails=_int_keyed(p.get("infoMails", {})),
                  **_profile_document(p))


def h_does_user_exist(store, r, ctx):
    return _reply(PROFILE, "DoesUserExist_V1", exists=1)


def h_set_portrait(store, r, ctx):
    """``SetPortrait_V1``: one u32 (an asset GUID) -> void."""
    _set_profile_value(store, ctx, "portrait", r.u32())
    return _reply(PROFILE, "SetPortrait_V1")


def h_set_icon(store, r, ctx):
    """``SetIcon_V1``: one u32 -> void."""
    _set_profile_value(store, ctx, "icon", r.u32())
    return _reply(PROFILE, "SetIcon_V1")


def h_set_color(store, r, ctx):
    """``SetColor_V1``: one u32 -> void."""
    _set_profile_value(store, ctx, "color", r.u32())
    return _reply(PROFILE, "SetColor_V1")


def h_set_player_title(store, r, ctx):
    """``SetPlayerTitle_V1``: one u32 (the title's asset GUID) -> void.

    It used to be read as a String, which turned the four bytes into a bogus
    u16 length and stored garbage - titles are GUIDs, per the client's own
    writer (``FUN_140deb2e0``: a single fixed-width 4-byte field).
    """
    if r.remaining() >= 4:
        _set_profile_value(store, ctx, "title", r.u32())
    elif r.remaining():
        # a String body from an older client: keep it, but out of the u32 slot
        p = store.player(_ctx_pid(ctx))
        p["titleText"] = r.string()
        store.save()
    return _reply(PROFILE, "SetPlayerTitle_V1")


# ---- ARK item box (the headline feature) ---------------------------------

def h_get_ark_items(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(PROFILE, "GetArkItems_V1", items=p.get("arkItems", []))


def h_set_ark_items(store, r, ctx):
    """``SetArkItems_V1``: the whole ``list<ArkItem>`` -- the box, replaced.

    Two different "nothing" bodies, and they must not be confused:

    * **no argument bytes at all** (a request that carried nothing, whatever the
      reason) is a *no-op*: never an instruction to delete the player's items;
    * **count 0** (a real, readable empty list) *is* the clear - that is how the
      game empties a socket.

    Wiping the box on a bodiless request is exactly the kind of "my ARK items
    disappeared" report this guards against.
    """
    p = store.player(_ctx_pid(ctx))
    if r.remaining() < 4:
        return _reply(PROFILE, "SetArkItems_V1")
    p["arkItems"] = read_ark_items(r)
    store.save()
    # FIXED (was: an empty FRAME): the reply is an empty PAYLOAD -- the name
    # block and nothing else -- wrapped by the caller in a normal envelope.
    return _reply(PROFILE, "SetArkItems_V1")


def h_set_ark(store, r, ctx):
    """``SetArk_V1`` has TWO shapes, and they must not be confused.

    **The profile field (kind 4, "Ark").**  ``CPlayerProfileServiceImpl::
    SetProfileValue(4, id)`` sends ONE u32 - the ark/ship asset id of the
    player's profile (writer ``FUN_140deb4c0``: a single fixed-width field).
    That is what this handler stores, and it is what makes the profile's fifth
    value survive a restart.

    **A legacy item delta.**  An older build of this server treated a 16-byte
    record list here as an ArkItem delta to merge into the box; the real
    ``list<ArkItem>`` call is ``SetArkItems_V1``.  A body longer than 4 bytes is
    still accepted with that older meaning, so nothing that worked breaks: the
    key is (socket_quality, socket_index), the value (item_id, count), and
    ``count == 0`` means "skip".  It merges (never replaces), so a one-socket
    delta cannot wipe the other sockets.
    """
    p = store.player(_ctx_pid(ctx))
    if r.remaining() <= 4:
        _set_profile_value(store, ctx, "ark", r.u32())
        return _reply(PROFILE, "SetArk_V1")
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
        if _w[3] == 0:                              # count 0 -> skip
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
    """``AddAchievement_V1``: u32 achievement id, u64 stamp -> u64 stamp.

    De-duplicated by id: the client re-sends the achievements it already owns
    on every login, so appending blindly grew the list forever (and, with the
    request prologue ignored, every one of them was id 0).  The FIRST stamp is
    kept - it is the moment the player earned it, which is what the dossier
    shows.
    """
    p = store.player(_ctx_pid(ctx))
    ach_id = r.u32() if r.remaining() >= 4 else 0
    value = r.u64() if r.remaining() >= 8 else int(time.time())
    if not value:
        value = int(time.time())
    achievements = p.get("achievements")
    if not isinstance(achievements, list):
        achievements = p["achievements"] = []
    for rec in achievements:
        if isinstance(rec, dict) and _as_int(rec.get("id"), -1) == ach_id:
            return _reply(PROFILE, "AddAchievement_V1", value=_as_int(rec.get("value"), value))
    achievements.append({"id": ach_id, "value": value})
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


def h_chat_channels(store, r, ctx):
    """``ChatServiceProtocol::GetChannels_V1``: the channels to join.

    A channel carries no name on the wire - the client resolves the id against
    its own data - so a made-up id would render as a blank tab.  The store may
    publish real ones under ``state["chat"]["channels"]``; until it does, the
    honest answer is an empty (but well-formed) list.
    """
    chat = store.state.get("chat")
    rows = chat.get("channels") if isinstance(chat, dict) else None
    return _reply(CHAT, "GetChannels_V1", channels=rows or ())


def h_chat_ignore_list(store, r, ctx):
    """``GetIgnoreList_V1``: the players this client muted (u32 ids)."""
    p = store.player(_ctx_pid(ctx))
    return _reply(CHAT, "GetIgnoreList_V1", ignored=p.get("ignored") or ())


def h_chat_void(method):
    def handler(store, r, ctx):
        store.player(_ctx_pid(ctx))
        return _reply(CHAT, method)
    handler.__name__ = "h_chat_" + method.lower()
    return handler


def _chat_store(store):
    chat = store.state.get("chat")
    if not isinstance(chat, dict):
        chat = store.state["chat"] = {}
    return chat


def _record(container, key, value):
    """Append ``value`` to ``container[key]`` once (a list of ids/strings)."""
    rows = container.get(key)
    if not isinstance(rows, list):
        rows = container[key] = []
    if value not in rows:
        rows.append(value)
    return rows


def h_chat_subscribe(store, r, ctx):
    """``Subscribe_V1``: join a channel (request: one String)."""
    channel = r.string() if r.remaining() else ""
    if channel:
        _record(_chat_store(store), "subscribed", channel)
        store.save()
    return _reply(CHAT, "Subscribe_V1")


def h_chat_unsubscribe(store, r, ctx):
    channel = r.string() if r.remaining() else ""
    rows = _chat_store(store).get("subscribed")
    if isinstance(rows, list) and channel in rows:
        rows.remove(channel)
        store.save()
    return _reply(CHAT, "Unsubscribe_V1")


def h_chat_send_channel(store, r, ctx):
    """``SendToChannel_V1``: String channel, String message -> void.

    There is nobody to deliver to on a local server, so the message is recorded
    (last 50, so the state file cannot grow without bound) and acknowledged.
    That is enough for the client's own chat window to behave.
    """
    channel = r.string() if r.remaining() else ""
    message = r.string() if r.remaining() else ""
    if message:
        sent = _chat_store(store).setdefault("sent", [])
        sent.append({"channel": channel, "text": message[:400],
                     "ts": int(time.time())})
        del sent[:-50]
        store.save()
    return _reply(CHAT, "SendToChannel_V1")


def h_chat_send_user(store, r, ctx, method="SendToUser_V1"):
    """``SendToUser_V1`` / ``SendToGroup_V1``: String target, String message."""
    to = r.string() if r.remaining() else ""
    message = r.string() if r.remaining() else ""
    if message:
        sent = _chat_store(store).setdefault("sent", [])
        sent.append({"to": to, "text": message[:400], "ts": int(time.time())})
        del sent[:-50]
        store.save()
    return _reply(CHAT, method)


def h_chat_ignore(store, r, ctx, method="Ignore_V1", remove=False):
    who = r.string() if r.remaining() else ""
    p = store.player(_ctx_pid(ctx))
    ignored = p.get("ignored")
    if not isinstance(ignored, list):
        ignored = p["ignored"] = []
    if who and not remove and who not in ignored:
        ignored.append(who)
        store.save()
    elif who and remove and who in ignored:
        ignored.remove(who)
        store.save()
    return _reply(CHAT, method)


def h_chat_void_of(method):
    """A chat method whose request we do not need: acknowledge it."""
    def handler(store, r, ctx):
        store.player(_ctx_pid(ctx))
        return _reply(CHAT, method)
    handler.__name__ = "h_chat_" + method.lower()
    return handler


def _friends_list(store, ctx, key):
    p = store.player(_ctx_pid(ctx))
    rows = p.get(key)
    return rows if isinstance(rows, list) else []


def h_find_players(store, r, ctx, method="FindPlayers_V1"):
    """``FindPlayers_V1`` / ``FindFacebookFriends_V1``: a search.

    The request is a handful of u32 filters; on a local server there is nobody
    to find, so the reply is an empty ``SearchResult`` list - the truth, and the
    same document the client already copes with.
    """
    while r.remaining() >= 4:
        r.u32()
    return _reply(FRIENDS, method)


def h_set_comment(store, r, ctx):
    """``SetComment_V1``: String friend, String comment -> void."""
    who = r.string() if r.remaining() else ""
    comment = r.string() if r.remaining() else ""
    p = store.player(_ctx_pid(ctx))
    if who:
        comments = p.get("comments")
        if not isinstance(comments, dict):
            comments = p["comments"] = {}
        comments[who] = comment[:200]
        store.save()
    return _reply(FRIENDS, "SetComment_V1")


def h_chat_send_messages(store, r, ctx):
    """``SendMessages_V1``: a batch of chat messages (bool,u32,bool,u32 each)."""
    sent = _chat_store(store).setdefault("sent", [])
    while r.remaining() >= 10:
        flag_a = bool(r.u32()) if r.remaining() >= 4 else False
        target = r.u32() if r.remaining() >= 4 else 0
        flag_b = bool(r.u32()) if r.remaining() >= 4 else False
        payload = r.u32() if r.remaining() >= 4 else 0
        sent.append({"batch": True, "a": flag_a, "target": target,
                     "b": flag_b, "value": payload, "ts": int(time.time())})
    del sent[:-50]
    store.save()
    return _reply(CHAT, "SendMessages_V1")


def h_mail_upload_pair(store, r, ctx, method):
    """``UploadMailToProfile_V1`` and friends: u32, u32 -> void.

    The pair is (profile id, mail id) in the captures we have; whatever it is,
    recording it is strictly better than dropping it, and the reply is void by
    contract so no shape can go wrong.
    """
    p = store.player(_ctx_pid(ctx))
    first = r.u32() if r.remaining() >= 4 else 0
    second = r.u32() if r.remaining() >= 4 else 0
    if first or second:
        uploads = p.get("mailUploads")
        if not isinstance(uploads, list):
            uploads = p["mailUploads"] = []
        uploads.append({"first": first, "second": second, "ts": int(time.time())})
        del uploads[:-50]
        store.save()
    return _reply(MAIL, method)


def h_add_recent_contact(store, r, ctx):
    return h_add_friend_kind(store, r, ctx, "AddRecentContact_V1", "recentContacts")


def h_add_friend_kind(store, r, ctx, method, key):
    who = r.string() if r.remaining() else ""
    p = store.player(_ctx_pid(ctx))
    if who:
        rows = p.get(key)
        if not isinstance(rows, list):
            rows = p[key] = []
        if who not in rows:
            rows.append(who)
            store.save()
    return _reply(FRIENDS, method)


def h_add_friend(store, r, ctx):
    """``AddFriend_V1``: one String (the profile id / name) -> void."""
    who = r.string() if r.remaining() else ""
    p = store.player(_ctx_pid(ctx))
    if who:
        rows = p.get("friends")
        if not isinstance(rows, list):
            rows = p["friends"] = []
        if who not in rows:
            rows.append(who)
            store.save()
    return _reply(FRIENDS, "AddFriend_V1")


def h_remove_friend(store, r, ctx):
    who = r.string() if r.remaining() else ""
    p = store.player(_ctx_pid(ctx))
    rows = p.get("friends")
    if isinstance(rows, list) and who in rows:
        rows.remove(who)
        store.save()
    return _reply(FRIENDS, "RemoveFriend_V1")


def h_get_friends(store, r, ctx):
    """``FriendsServiceProtocol::GetFriends_V1``: the friend list.

    ``Friend`` carries ids, not names; with nobody else on a local server the
    truthful answer is an empty list.  A store may fill
    ``players.<pid>.friends`` later and the same reply will carry it.
    """
    p = store.player(_ctx_pid(ctx))
    return _reply(FRIENDS, "GetFriends_V1", friends=p.get("friends") or ())


def h_get_friends_with_activations(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(FRIENDS, "GetFriendsWithActivations_V1",
                  friends=p.get("friends") or ())


def h_get_recent_contacts(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    return _reply(FRIENDS, "GetRecentContacts_V1",
                  recent=p.get("recentContacts") or ())


def h_mail_count(store, r, ctx):
    """``MailServiceProtocol::GetMailCount_V1``: three u32 (total, unread, 0).

    The client polls this once per session and badges the mail icon with it.
    The number is the player's real mailbox: every info mail we deliver (the
    world-event intro mail, for example) counts, and one counts as unread while
    its ``infoMails`` value is 0 - the same map ``SetInfoMailRead_V1`` flips.
    """
    p = store.player(_ctx_pid(ctx))
    mails = _int_keyed(p.get("infoMails", {}))
    unread = sum(1 for flag in mails.values() if not _as_int(flag, 1))
    return _reply(MAIL, "GetMailCount_V1", total=len(mails), unread=unread)


def _mailbox(player_doc):
    """The player's real mailbox: a list of ``{id, stamp, template, read}``."""
    box = player_doc.get("mailbox")
    if not isinstance(box, list):
        box = player_doc["mailbox"] = []
    return box


def deliver_mail(player_doc, template, stamp=None, mail_id=None, read=False):
    """Put one mail in the mailbox (idempotent per template)."""
    box = _mailbox(player_doc)
    for row in box:
        if isinstance(row, dict) and int(row.get("template") or 0) == int(template):
            return row
    if mail_id is None:
        mail_id = (max([int(r.get("id") or 0) for r in box] or [0]) + 1)
    row = {"id": int(mail_id), "stamp": int(stamp if stamp is not None else time.time()),
           "template": int(template), "read": bool(read)}
    box.append(row)
    return row


def h_get_mails(store, r, ctx):
    """``MailServiceProtocol::GetMails_V1``: the mailbox, newest first.

    ``Mail`` = ``u32; u64; u32; bool``.  The u32 we return in the third slot is
    the *loca id* of the mail (the same id the event's ``InitialMail`` /
    ``ProgressMail`` / ``ConclusionMail`` use), so the client can render the
    real text; the second is a unix timestamp and the last is the read flag.
    """
    p = store.player(_ctx_pid(ctx))
    box = sorted(_mailbox(p), key=lambda row: -int(row.get("stamp") or 0))
    return _reply(MAIL, "GetMails_V1", mails=box)


def h_get_mail(store, r, ctx):
    """``GetMail_V1``: one mail by id (request ``u32``), or an empty record."""
    p = store.player(_ctx_pid(ctx))
    wanted = r.u32() if r.remaining() >= 4 else 0
    for row in _mailbox(p):
        if int(row.get("id") or 0) == wanted:
            return _reply(MAIL, "GetMail_V1", mail=row)
    return _reply(MAIL, "GetMail_V1", mail=None)


def h_set_read_flag(store, r, ctx):
    """``SetReadFlag_V1``: mark mails read (also mirrors into ``infoMails``)."""
    p = store.player(_ctx_pid(ctx))
    read = True
    while r.remaining() >= 4:
        wanted = r.u32()
        if r.remaining() >= 1:
            read = bool(r.bool() if hasattr(r, "bool") else 1)
        for row in _mailbox(p):
            if int(row.get("id") or 0) == wanted:
                row["read"] = read
                template = int(row.get("template") or 0)
                mails = p.get("infoMails")
                if isinstance(mails, dict) and template in _int_keyed(mails):
                    _int_keyed(mails)[template] = 1 if read else 0
                    p["infoMails"] = _int_keyed(mails)
    store.save()
    return _reply(MAIL, "SetReadFlag_V1")


def h_delete_mail(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    wanted = {r.u32()} if r.remaining() >= 4 else set()
    p["mailbox"] = [row for row in _mailbox(p)
                    if int(row.get("id") or 0) not in wanted]
    store.save()
    return _reply(MAIL, "DeleteMail_V1")


def h_delete_all_mails(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    p["mailbox"] = []
    store.save()
    return _reply(MAIL, "DeleteAllMails_V1")


def h_upload_mail(store, r, ctx):
    """``MailServiceProtocol::UploadMail_V1``: the client reports its mailbox.

    Request (captured): ``u32 u32 String String``.  The two strings are the
    mail's own ids; we keep what the client reports so the count and the
    mailbox never disagree, and never invent mail the player did not get.
    """
    p = store.player(_ctx_pid(ctx))
    first = r.u32() if r.remaining() >= 4 else 0
    second = r.u32() if r.remaining() >= 4 else 0
    a = r.string() if r.remaining() else ""
    b = r.string() if r.remaining() else ""
    if not (a or b):
        return _reply(MAIL, "UploadMail_V1")
    p.setdefault("uploadedMail", []).append(
        {"first": first, "second": second, "a": a, "b": b,
         "ts": int(time.time())})
    store.save()
    return _reply(MAIL, "UploadMail_V1")


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
    """Mark an info mail read - and the same mail in the mailbox.

    The two documents describe the same mail (``infoMails`` is the flag map the
    login reads, ``mailbox`` is the list the mail panel reads), so one handler
    keeps them in step instead of letting the badge and the panel disagree.
    """
    p = store.player(_ctx_pid(ctx))
    mail_id, read = r.u32(), r.bool() if r.remaining() else True
    p.setdefault("infoMails", {})[mail_id] = 1 if read else 0
    for row in _mailbox(p):
        if int(row.get("template") or 0) == int(mail_id) or \
                int(row.get("id") or 0) == int(mail_id):
            row["read"] = bool(read)
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
    """``SetStats_V1``: ``list<StatsEntry>`` (36 B each) -> void.

    An EMPTY request is "no rows", not "delete my statistics": the client sends
    exactly that on every login, and treating it as a wipe was destroying the
    stored rows each session (the "statistics are not persistent" report).
    """
    p = store.player(_ctx_pid(ctx))
    if r.remaining() < 4:
        return _reply(PROFILE, "SetStats_V1")
    rows = read_stats_entries(r)
    if rows or r.remaining() >= 4:
        p["stats"] = rows
        store.save()
    return _reply(PROFILE, "SetStats_V1")


def h_get_world_stats(store, r, ctx):
    p = store.player(_ctx_pid(ctx))
    ws = p.get("worldStats") or {}
    return _reply(PROFILE, "GetWorldStats_V1",
                  version=ws.get("version", 0),
                  entries=ws.get("entries", []))


# ---- reputation ----------------------------------------------------------

#: Diagnostic knob for the ONE unproven thing on the career path: whether
#: ``AreReputationPointsMissing_V1`` asks "do you have points for me?" (answer 0
#: when we do) or "is my copy stale?" (answer 1 so the client re-asks).  We have
#: never observed the client follow up with either
#: ``GetReputationPoints_V1`` or ``SetReputationPoints_V1``, so the polarity
#: cannot be settled from the captures.  ``RV_REPUTATION_PROBE`` lets one live
#: session try the other answers without a rebuild:
#:
#:   unset / "normal"  -- answer from the profile (the shipped behaviour)
#:   "present"         -- always 0 ("the server has your points")
#:   "missing"         -- always 1 ("ask me again")
#:   "alternate"       -- the opposite of the truth, ONCE per process
#:
#: If a ``GetReputationPoints_V1``/``SetReputationPoints_V1`` follows the
#: alternate answer, the probe is the trigger and the answer becomes "missing=1
#: whenever our map is non-empty and the client has not asked yet".
_REPUTATION_PROBE_STATE = []


def _reputation_probe(asked, missing):
    """The override for :func:`h_are_rep_missing` (see the knob's docstring)."""
    mode = (os.environ.get("RV_REPUTATION_PROBE") or "").strip().lower()
    if mode in ("", "normal", "0", "off"):
        return None
    if mode in ("present", "have", "0-answer"):
        return 0
    if mode in ("missing", "1-answer"):
        return 1
    if mode == "alternate":
        if _REPUTATION_PROBE_STATE:
            return None
        _REPUTATION_PROBE_STATE.append(True)
        if LOG is not None:
            LOG("reputation: PROBE alternate answer for asked=%r (was %d) -> %d"
                % (asked, missing, 0 if missing else 1))
        return 0 if missing else 1
    return None


def h_get_reputation_points(store, r, ctx):
    """``map<u32,u32>`` faction id -> reputation points (the career page).

    The client renders one panel per faction (``FACTION_ECO=0``,
    ``FACTION_TYCOON=1``, ``FACTION_TECH=2``) and treats a *missing key* as "no
    data for that faction", so once the player has any reputation all three
    keys are served - the two they have not earned read 0 and render as an
    empty bar instead of a blank panel.
    """
    p = store.player(_ctx_pid(ctx))
    rep = _int_keyed(p.get("reputation", {}))
    pairs = {faction: max(0, _as_int(rep.get(faction), 0))
             for faction in content_voting.FACTIONS}
    pairs.update({k: v for k, v in rep.items() if k not in pairs})
    p["reputationSeen"] = _as_int(p.get("reputationVersion"), 0)
    if LOG is not None:
        LOG("reputation: GetReputationPoints -> %r (v%s, marked as seen)"
            % (pairs, p["reputationSeen"]))
    return _reply(PROFILE, "GetReputationPoints_V1", pairs=pairs)


def h_are_rep_missing(store, r, ctx):
    """``AreReputationPointsMissing_V1``: one bool, per reputation key.

    The request's u32 is the key being asked about (0 when the body is empty).
    Answering "missing" forever while the map was empty made the client keep
    its locally-earned points instead of asking for the server's.
    """
    p = store.player(_ctx_pid(ctx))
    asked = r.u32() if r.remaining() >= 4 else None
    rep = _int_keyed(p.get("reputation", {}))
    # "missing" means "your copy is stale", not "do you have any points":
    #  * no points at all            -> missing (nothing to copy yet)
    #  * points changed since the
    #    client last fetched the map -> missing (refetch, this is the moment the
    #                                   career page refreshes)
    #  * client already has the map  -> not missing
    version = _as_int(p.get("reputationVersion"), 0)
    seen = _as_int(p.get("reputationSeen"), 0)
    missing = 1 if (not rep or seen < version) else 0
    probe = _reputation_probe(asked, missing)
    if probe is not None:
        missing = probe
    if LOG is not None:
        LOG("reputation: AreMissing asked=%r have=%r version=%d seen=%d -> %d%s"
            % (asked, rep, version, seen, missing,
               " (probe override)" if probe is not None else ""))
    return _reply(PROFILE, "AreReputationPointsMissing_V1", missing=missing)


def h_set_reputation_points(store, r, ctx):
    """``SetReputationPoints_V1``: u32 key, u32 value -> void.

    ``p["reputation"]`` is coerced to a dict first: an older state file stored
    it as a list, and ``setdefault(...)[key] = value`` on a list raises
    IndexError, which the reply path swallows -- so the client was told the
    points were saved while nothing was written.
    """
    p = store.player(_ctx_pid(ctx))
    key, value = r.u32(), r.u32()
    rep = p.get("reputation")
    if not isinstance(rep, dict):
        rep = p["reputation"] = {}
    existing = _int_keyed(rep)
    old_value = _as_int(existing.get(key), 0)
    if value < old_value:
        # a client echo may RAISE a total but never lower it: the client keeps
        # its own local copy and re-pushes it on login, so an overwrite would
        # delete points the server had already paid out
        if LOG is not None:
            LOG("reputation: SetReputationPoints key=%d ignored lower value %d < %d"
                % (key, value, old_value))
        return _reply(PROFILE, "SetReputationPoints_V1")
    rep[key] = value
    _bump_reputation_version(p)
    store.save()
    if LOG is not None:
        try:
            LOG(f"RMC reputation: SetReputationPoints key={key} value={value}")
        except Exception:                        # noqa: BLE001
            pass
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


# ---- upload capture ------------------------------------------------------

#: Bounded so a chatty client cannot grow the save file without limit.
MAX_UPLOADS = 20
MAX_UPLOAD_BYTES = 256


def _capture_upload(bucket, proto, method, rows=None):
    """Handler factory: record what the client uploads instead of dropping it.

    These methods (``UploadStatsReputation_V1``, ``UploadFactionRewards_V1``,
    ``UploadReputationMails_V1``, ``SolveDailyQuest_V1``,
    ``UploadWorldStatsStencils_V1``) have a VOID reply, so their request body is
    the only copy of the data that exists anywhere.  Sending it to ``_void_player``
    threw it away *and* logged nothing, which is why "are statistics/reputation
    persisted?" could not be answered from the captures.

    Two things are recorded: the decoded rows when the body parses as the
    documented ``u32 count + fixed-size rows`` shape, and a bounded hex tail
    with the byte count -- enough to decode the real layout from a live session
    without another round of guesswork.  The reply stays void, so a wrong
    guess here can never break the client.
    """
    def handler(store, r, ctx):
        p = store.player(_ctx_pid(ctx))
        body = r.rest()
        record = {"ts": int(time.time()), "bytes": len(body)}
        if rows is not None and len(body) >= 4:
            try:
                record["rows"] = rows(Reader(body))
            except Exception:                     # noqa: BLE001
                record["rows"] = []
        record["raw"] = body[:MAX_UPLOAD_BYTES].hex()
        log = p.get(bucket)
        if not isinstance(log, list):
            log = p[bucket] = []
        log.append(record)
        del log[:-MAX_UPLOADS]
        store.save()
        if LOG is not None:
            LOG("upload %s: %d bytes %s" % (method, len(body), record["raw"][:64]))
        return _reply(proto, method)
    handler.__name__ = "h_capture_" + re.sub(r"[^a-z0-9]+", "_", method.lower())
    return handler


#: What a solved daily quest is worth in career reputation.  ``properties.xml``
#: ships ``DailyQuestReputation = 100`` (line 21267) - a real, in-game daily
#: quest reward, unlike the career ladder itself, which is client-side.  GUESS:
#: that value is the *in-game* currency reward's sibling; if a live session shows
#: a different number in the quest's own "+N reputation" toast, change it here.
DAILY_QUEST_REWARD = 100


def h_solve_daily_quest(store, r, ctx):
    """``SolveDailyQuest_V1``: u32 quest id -> void.

    Two things happen: the call is captured (the reply is void, so the request is
    the only copy of the fact) and, for a quest id this player has never solved,
    its reputation is credited to the career map.  Payments are de-duplicated on
    ``players.<pid>.dailyQuests`` so re-solving (or a replayed request) cannot
    farm points.  A body we cannot read credits nothing - never invent a quest.
    """
    p = store.player(_ctx_pid(ctx))
    body = r.rest()
    record = {"ts": int(time.time()), "bytes": len(body),
              "raw": body[:MAX_UPLOAD_BYTES].hex()}
    rows = []
    try:
        rows = _u32_rows(Reader(body))
    except Exception:                             # noqa: BLE001
        rows = []
    record["rows"] = rows
    log = p.get("dailyQuests")
    if not isinstance(log, list):
        log = p["dailyQuests"] = []
    known = {_as_int(entry.get("quest"), -1) for entry in log
             if isinstance(entry, dict)}
    for quest in rows:
        if quest in known:
            continue
        known.add(quest)
        log.append({"quest": quest, "ts": int(time.time()),
                    "reward": DAILY_QUEST_REWARD})
        _add_reputation(store, _ctx_pid(ctx), DAILY_QUEST_REWARD,
                        reason="daily quest %d" % quest)
    del log[:-MAX_UPLOADS]
    store.save()
    if LOG is not None:
        LOG("daily quest: %d bytes, rows=%r (paid %d per new id)"
            % (len(body), rows, DAILY_QUEST_REWARD))
    return _reply(PROFILE, "SolveDailyQuest_V1")


def _stats_reputation_rows(r):
    """``list<StatsReputation>``: 12 B each (three u32), per
    ``__AV_DDL_StatsReputation_rdv__.c`` (+8/+0xc/+0x10) and the 0xc stride of
    ``CPlayerProfileServiceImpl::UploadStatsReputation``."""
    n = min(r.count(), r.remaining() // 12)
    return [{"a": r.u32(), "b": r.u32(), "c": r.u32()} for _ in range(n)]


def _u32_rows(r):
    """``list<u32>`` -- the shape ``SolveDailyQuest_V1``/``FactionRewards`` use."""
    return r.u32_list()


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

def _elections(store, voter=None):
    """The election map the store currently serves (key 0 always present).

    ``voter`` is the session player.  It only matters for the own-vote field:
    when the stored document lost it (an older save, or a slot whose record was
    never created) the value is recovered from the vote log, so the panel says
    "voted" instead of letting the player vote again.
    """
    return content_voting.elections_from_state(store.state.get("voting"),
                                               voter=voter)


def _election_id(store):
    """The one election id every panel reads (``elections_from_state`` -> 0).

    The MVP serves exactly one election and forces its ``Vote()`` id to 0, so
    this is 0 -- but deriving it keeps the handlers honest if that ever grows.
    """
    keys = sorted(_elections(store))
    return keys[0] if keys else 0


def h_get_voting_infos(store, r, ctx):
    """``list<VotingInfo>`` -- the elections the senate screen renders."""
    infos = content_voting.voting_infos(elections=_elections(store,
                                                             _ctx_pid(ctx)))
    return _reply(VOTING, "GetVotingInfos_V1", infos=infos)


def h_vote(store, r, ctx):
    """``Vote_V1``: u32 election id, u32 faction -> a VOID reply.

    The vote is recorded through ``content_voting.apply_vote``, which keeps the
    counts, the voter list and the caller's own vote (VotingInfo f6) in step;
    the client then re-issues GetVoteCount/GetLastVoters by itself.

    The id the client sends is the ``f2`` of the document it rendered, so it is
    a *Vote id* (0 for the senate, 1001 for the World Council), not a store
    slot.  It must be mapped to the slot: coercing every id to the first slot
    put council votes on the senate (and they then read back as "not voted").
    """
    raw_id = r.u32() if r.remaining() >= 4 else 0
    faction = r.u32() if r.remaining() >= 4 else 0
    voting = store.state.setdefault("voting", {})
    elections = _elections(store)
    by_id = {content_voting.vote_id(info): slot for slot, info in elections.items()}
    election_id = by_id.get(raw_id)
    if election_id is None:
        # a stale client document that sends an id we do not serve must not put
        # the vote where no panel will ever read it
        election_id = _election_id(store)
    pid = _ctx_pid(ctx)
    # One reward per (SLOT, ELECTION INSTANCE, player): the panel can be clicked
    # again, and a repeated Vote_V1 used to add another count and another +50
    # career points every time.  The ticket carries the election instance, so a
    # new election (a new cycle of the same slot) pays again - which is the
    # point of an election - while a change of mind inside one election does not.
    rewarded = voting.get("rewarded")
    if not isinstance(rewarded, dict):
        rewarded = voting["rewarded"] = {}
    instance = content_voting.election_index(election_id)
    ticket = "%d#%d:%s" % (election_id, instance, pid)
    previous = rewarded.get(ticket)
    if previous == faction:
        store.save()
        return _reply(VOTING, "Vote_V1")     # same vote again: nothing changed
    try:
        content_voting.apply_vote(voting, election_id, faction, voter=pid)
    except (TypeError, ValueError):
        # a faction outside the proven enum (0/1/2) is a client bug: never
        # corrupt the bars, never fail the call, just record no vote
        pass
    else:
        rewarded[ticket] = faction
        if previous is None:
            # first vote in this election: pay once
            _credit_vote(store, faction, ctx)
        elif previous != faction:
            # a change of mind MOVES the reward to the faction the player now
            # supports - it still pays only once per election, but the points
            # must not stay on a faction the player has voted away from (that
            # is what made "I voted Eco but my Eco points never arrived" true)
            _move_vote_reward(store, pid, previous, faction)
    store.save()
    # Vote_V1's reply has NO body at all (the client skips parsing)
    return _reply(VOTING, "Vote_V1")


def _move_vote_reward(store, pid, old_faction, new_faction):
    """Move an election's 50 points from the faction voted before to the new one.

    One payment per election stays one payment; only its faction follows the
    player's final choice, so the career page cannot end up crediting a faction
    they no longer support.
    """
    if old_faction not in content_voting.FACTIONS or new_faction not in content_voting.FACTIONS:
        return
    if old_faction == new_faction:
        return
    p = store.player(pid)
    rep = _int_keyed(p.get("reputation", {}))
    old = _as_int(rep.get(old_faction), 0)
    moved = min(old, content_voting.VOTE_REWARD)
    if not moved:
        return
    rep[old_faction] = old - moved
    rep[new_faction] = _as_int(rep.get(new_faction), 0) + moved
    p["reputation"] = rep
    _bump_reputation_version(p)
    if LOG is not None:
        LOG("career: vote changed %d -> %d, moved %d reputation  map=%r v%d"
            % (old_faction, new_faction, moved, dict(sorted(rep.items())),
               p.get("reputationVersion")))


def _credit_vote(store, faction, ctx):
    """Credit the career reputation a vote is worth (``VOTE_REWARD``).

    The client shows "+50" locally and ``Vote_V1`` has no reply body, so the
    real service could only persist it through the profile -- which is where it
    now lands, under the faction the player voted for.  ``worldEvent.reputation``
    is kept equal for the same player so the world-event panel and the career
    page can never disagree (they read different documents).
    """
    if faction not in content_voting.FACTIONS:
        return
    pid = _ctx_pid(ctx)
    _add_reputation(store, pid, content_voting.VOTE_REWARD,
                    reason="vote", faction=faction)
    we = _world_event_state(store)
    wer = we.get("reputation")
    if not isinstance(wer, dict):
        wer = we["reputation"] = {}
    try:
        wer[pid] = int(wer.get(pid, 0) or 0) + content_voting.VOTE_REWARD
    except (TypeError, ValueError):
        wer[pid] = content_voting.VOTE_REWARD


def aligned_faction(store, pid="default"):
    """The faction the player last voted for (their "alignment"), else Eco.

    A world-event mission is not faction-specific, so its reputation has to go
    somewhere.  Picking the *smallest existing key* (what this used to do) put a
    mission's points on whatever faction happened to be first - the report "I
    earned points and my faction still shows 0" is exactly that.  The player's
    own last vote is the only signal we have about which faction they are
    working for, and Eco (0) is the neutral default.
    """
    voting = store.state.get("voting") if isinstance(store.state, dict) else None
    votes = voting.get("votes") if isinstance(voting, dict) else None
    for vote in reversed(votes if isinstance(votes, list) else ()):
        if not isinstance(vote, dict):
            continue
        if str(vote.get("voter", "") or "") != str(pid):
            continue
        try:
            choice = int(vote.get("choice", -1))
        except (TypeError, ValueError):
            continue
        if choice in content_voting.FACTIONS:
            return choice
    return content_voting.FACTION_ECO


def _bump_reputation_version(player_doc, delta=1):
    """Remember that the career map changed, so the client can be told to refetch.

    ``AreReputationPointsMissing_V1`` is the client's "is my copy stale?" probe.
    Answering it from "do we have any points" was a coin flip; a version pair
    (changed vs fetched) is the honest answer: after every credit the client is
    told the points are missing *until it has fetched the map*, which is the
    moment it refreshes the career page.
    """
    try:
        player_doc["reputationVersion"] = int(
            player_doc.get("reputationVersion") or 0) + int(delta)
    except (TypeError, ValueError):
        player_doc["reputationVersion"] = int(delta)
    return player_doc["reputationVersion"]


def _add_reputation(store, pid, points, reason="reward", faction=None):
    """Add career reputation to ``players.<pid>.reputation`` (never overwrite).

    ``faction`` picks the key explicitly (a vote knows which faction paid); with
    ``None`` the points land on the player's *aligned* faction (their last vote,
    else Eco) - see :func:`aligned_faction`.  Add-only is the rule everywhere:
    two sources paying the same map must never wipe each other, and a client
    echo must never lower a total (see ``h_set_reputation_points``).

    Every write bumps ``reputationVersion`` so ``AreReputationPointsMissing_V1``
    can tell the client its copy is stale (that is what makes the career page
    refresh instead of showing its own zeros).

    Returns the new total for that key (0 when nothing was written).
    """
    points = _as_int(points, 0)
    if not points:
        return 0
    p = store.player(pid)
    rep = p.get("reputation")
    if not isinstance(rep, dict):
        rep = p["reputation"] = {}
    normalised = _int_keyed(rep)
    if faction in content_voting.FACTIONS:
        key = int(faction)
    else:
        key = aligned_faction(store, pid)
    normalised[key] = _as_int(normalised.get(key), 0) + points
    p["reputation"] = normalised
    _bump_reputation_version(p)
    if LOG is not None:
        LOG(f"career: +{points} reputation ({reason}) -> faction {key} "
            f"= {normalised[key]} for {pid}  map={dict(sorted(normalised.items()))} "
            f"v{p.get('reputationVersion')}")
    return normalised[key]


def _credit_mission(store, pid, record):
    """Pay out a solved world-event mission: career reputation + event points.

    The mission's worth comes from the shipped asset table
    (``content_events.mission_info``): 1/5/10 points and 50/150/300 reputation.
    The reputation lands in ``players.<pid>.reputation`` - the same map the
    senate vote fills, so the career page shows one growing number - under the
    first key the player already has (a vote picks the faction key), or key 0
    when this is the first reputation they ever earn.  Add only; never
    overwrite, so a vote reward cannot be wiped by a mission and vice versa.
    """
    if not isinstance(record, dict):
        return
    info = content_events.mission_info(record.get("id"))
    points = _as_int(record.get("points"), info["points"])
    rep_points = _as_int(record.get("rep"), info["reputation"])
    record["points"], record["rep"] = points, rep_points

    p = store.player(pid)
    _add_reputation(store, pid, rep_points, reason="mission %s" % record.get("id"))

    we = _world_event_state(store)
    wer = we.get("reputation")
    if not isinstance(wer, dict):
        wer = we["reputation"] = {}
    try:
        wer[pid] = _as_int(wer.get(pid), 0) + points
    except (TypeError, ValueError):
        wer[pid] = points

    _grant_ark_reward(store, p, record.get("id"))
    _grant_achievement(store, p, record.get("id"))
    _send_progress_mail(store, p, record.get("id"))


def _send_event_mail(store, chapter):
    """Put the event's own intro mail in the player's mailbox.

    The id is the chapter's ``InitialMail`` from ``assets.xml`` - real client
    text, no invention - so the mail panel shows the same mail the official
    server would have sent when the event started.
    """
    mail_id = content_events.chapter_mail(chapter, "initial")
    if not mail_id:
        return
    p = store.player("default")
    mails = p.get("infoMails")
    if not isinstance(mails, dict):
        mails = p["infoMails"] = {}
    mails = {int(k): v for k, v in mails.items() if str(k).lstrip("-").isdigit()}
    p["infoMails"] = mails
    if int(mail_id) in mails:
        return
    mails[int(mail_id)] = 0                     # 0 = unread
    deliver_mail(p, int(mail_id))
    if LOG is not None:
        try:
            LOG("RMC event mail: %s (chapter %s)" % (mail_id, chapter))
        except Exception:                    # noqa: BLE001
            pass


def _grant_achievement(store, player_doc, mission_id):
    """Unlock the mission's achievement (the client renders its own text)."""
    achievement = content_events.mission_achievement(mission_id)
    if not achievement:
        return
    items = player_doc.get("achievements")
    if not isinstance(items, list):
        items = player_doc["achievements"] = []
    if any(isinstance(a, dict) and int(a.get("id") or 0) == achievement
           for a in items):
        return
    items.append({"id": int(achievement), "value": int(time.time())})
    if LOG is not None:
        try:
            LOG("RMC achievement: %s (mission %s)" % (achievement, mission_id))
        except Exception:                    # noqa: BLE001
            pass


def _send_progress_mail(store, player_doc, mission_id):
    """Mail the chapter's own reaction to a solved mission.

    Every chapter ships ``ProgressMail`` (one mission solved) and
    ``ConclusionMail`` (the whole chain) beside its ``InitialMail`` - all real
    loca ids the client renders itself.  Sending the matching one is what makes
    the mailbox read like the official server's, without inventing any text.
    """
    info = content_events.mission_info(mission_id)
    chapter = info.get("chapter")
    if not chapter:
        return
    total = content_events.chapter_mission_count(chapter) or 1
    solved = set()
    we = _world_event_state(store)
    for rec in we.get("completedMissions") or ():
        if not isinstance(rec, dict):
            continue
        if content_events.mission_info(rec.get("id")).get("chapter") != chapter:
            continue
        solved.add(int(rec.get("id") or 0))
    kind = "conclusion" if len(solved) >= total else "progress"
    mail_id = content_events.chapter_mail(chapter, kind)
    if not mail_id:
        return
    mails = player_doc.get("infoMails")
    if not isinstance(mails, dict):
        mails = player_doc["infoMails"] = {}
    mails = {int(k): v for k, v in mails.items() if str(k).lstrip("-").isdigit()}
    player_doc["infoMails"] = mails
    if int(mail_id) in mails:
        return
    mails[int(mail_id)] = 0
    deliver_mail(player_doc, int(mail_id))
    if LOG is not None:
        try:
            LOG("RMC event mail: %s (%s, chapter %s)" % (mail_id, kind, chapter))
        except Exception:                    # noqa: BLE001
            pass


def _grant_ark_reward(store, player_doc, mission_id):
    """Put the mission's ARK reward into a matching socket, if it has one.

    The reward ids come from the achievement table that links mission assets to
    rewards; only the ones that are real Item assets are in
    ``content_events.MISSION_ARK_REWARDS`` (the rest are profile cosmetics and
    cannot be items at all).  Allocation rules, from the client's own Ark asset:

    * an item may only sit in the socket group of its own quality, so the first
      free index **of that group** is used (A: 1, B: 2, C: 3 slots);
    * when that group is full the reward is skipped rather than forced into a
      wrong socket, where the client would drop it and show an empty slot;
    * the box is a delta-managed map, so an existing socket is never
      overwritten - the item the player put there stays.
    """
    if os.environ.get("RV_ARK_REWARDS", "1").strip() == "0":
        return
    rewards = content_events.mission_ark_rewards(mission_id)
    if not rewards:
        return
    box = player_doc.get("arkItems")
    if not isinstance(box, list):
        box = player_doc["arkItems"] = []
    letters = {1: "A", 2: "B", 3: "C"}
    granted = []
    for item_id, quality in rewards:
        index = content_events.first_free_slot(box, quality)
        evicted = None
        if index is None:
            # the group is full: a previous mission reward or one of our own
            # seed items may be pushed out (never an item the player put there
            # himself), so a finished event's reward is always collectable
            for pos, item in enumerate(box):
                if not isinstance(item, dict) or item.get("src") not in ("mission", "seed"):
                    continue
                if int(item.get("a") or 0) != int(quality):
                    continue
                evicted = (pos, item.get("c"))
                index = int(item.get("b") or 0)
                break
        if index is None:
            granted.append("%d:no free %s socket" % (item_id, letters.get(quality, "?")))
            continue
        if evicted:
            box.pop(evicted[0])
            granted.append("replaced %s" % evicted[1])
        box.append({"a": int(quality), "b": int(index), "c": int(item_id), "d": 1,
                    "src": "mission"})
        granted.append("%d->%s%d" % (item_id, letters.get(quality, "?"), index))
    if LOG is not None and granted:
        try:
            LOG("RMC ark reward: mission %s -> %s" % (mission_id, ", ".join(granted)))
        except Exception:                        # noqa: BLE001
            pass


def _voting_slot_for(store, raw_id):
    """Store slot for the Vote id the client just sent (0 when unknown)."""
    for slot, info in _elections(store).items():
        if content_voting.vote_id(info) == raw_id:
            return slot
    return _election_id(store)


def _vote_simulation_on():
    """Is the simulated electorate on?  ``RV_VOTE_SIM=0`` turns it off."""
    return (os.environ.get("RV_VOTE_SIM") or "").strip().lower() not in (
        "0", "off", "no", "false", "static")


def _served_counts(store, slot):
    """The bars the client shows: simulated world + this player's real votes."""
    return content_voting.served_counts(store.state, slot,
                                        simulate=_vote_simulation_on())


def h_get_vote_count(store, r, ctx):
    """``map<u32,u32>`` faction -> votes, for the election the client asked for.

    The request's u32 is the Vote id of the rendered document (the client polls
    both the senate id 0 and the council id 1001 every few seconds), so it
    selects the slot -- ignoring it made the council show the senate's bars.
    """
    raw_id = r.u32() if r.remaining() >= 4 else 0
    counts = _served_counts(store, _voting_slot_for(store, raw_id))
    return _reply(VOTING, "GetVoteCount_V1", counts=counts)


def h_get_voting_stats(store, r, ctx):
    """``list<VotingStats>``: u32 key, u32 denominator, map<u32,u32>.

    ``f1`` is the client's percentage denominator (``100*sum/f1``), so the
    content module guarantees ``f1 > 0`` and ``f1 >= sum`` -- a zero would
    paint ``INT_MIN`` % bars.

    One row per election is returned (the senate row first); the client picks
    the one it rendered.  The request's leading u32, when present, is the Vote
    id, but the reply shape is a list either way, so filtering buys nothing.
    """
    entries = content_voting.voting_stats(
        store.state, _elections(store),
        counts_fn=lambda slot: _served_counts(store, slot))
    return _reply(VOTING, "GetVotingStats_V1", entries=entries)


def h_get_last_voters(store, r, ctx):
    """``GetLastVoters_V1``: u32 election id, u32 limit -> ``list<VoteEntry>``.

    ``list<VoteEntry>`` entries are ``String voter, u32 faction, u32 per``.
    The two request words are proven from the sender in
    ``decomp/ddl_ser.c:3190,3203`` (``u32,u32``) and from the live captures
    (the frame always re-issues the pair right after ``Vote_V1``): *id* first,
    then the number of voters the panel wants.  Recorded votes come back
    newest first, and the content module fills the list from the counts when
    nobody has voted yet so the panel is not blank.
    """
    raw_id = None
    limit = 10
    if r.remaining() >= 8:
        raw_id = r.u32()
        limit = r.u32()
    elif r.remaining() >= 4:
        raw_id = r.u32()
    limit = min(max(0, limit), 64)
    slot = _election_id(store) if raw_id is None else _voting_slot_for(store, raw_id)
    voters = content_voting.served_voters(store.state, slot, limit,
                                          simulate=_vote_simulation_on())
    # the panel bolds YOUR row by comparing the name in the list with the profile
    # name, so a ballot of ours must be sent under the displayed name, not the
    # internal player id ("default")
    pid = _ctx_pid(ctx)
    try:
        display = str(store.player(pid).get("name") or pid)
    except Exception:                                # noqa: BLE001
        display = pid
    voters = [dict(entry, voter=display)
              if str(entry.get("voter", "")) == pid else entry
              for entry in voters]
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


def _campaign_players(store, we=None, ctx=None):
    """Every player the campaign pays out to (registered, stored, session)."""
    pids = []
    we = we if isinstance(we, dict) else _world_event_state(store)
    for name in (we.get("registered") or ()):
        if isinstance(name, str) and name and name not in pids:
            pids.append(name)
    players = store.state.get("players")
    if isinstance(players, dict):
        for name in players:
            name = str(name)
            if name and name not in pids:
                pids.append(name)
    if ctx is not None:
        pid = _ctx_pid(ctx)
        if pid and pid not in pids:
            pids.append(pid)
    return pids


def _pay_campaign_tiers(store, we, summary, ctx=None):
    """Pay the reputation of every campaign tier the world has just crossed.

    The ladder is the chapter's own shipped one (25k/50k/75k/100k for chapter 1,
    25k/75k/150k/300k for the DLC chapters) and each tier pays
    ``CAMPAIGN_TIER_REWARDS`` to *every* player the campaign knows about - it is
    a world event, so the whole world reached the threshold together.

    Idempotent per ``(campaign index, tier)``: the crossed tiers are recorded in
    ``worldEvent.campaignPaid`` before the money is handed out, so a restart, a
    second login or a re-request cannot pay twice.
    """
    reached = _as_int(summary.get("tiersReached"), 0)
    if reached <= 0:
        return []
    paid = we.get("campaignPaid")
    if not isinstance(paid, dict):
        paid = we["campaignPaid"] = {}
    key = str(_as_int(summary.get("index"), 0))
    done = paid.get(key)
    if not isinstance(done, list):
        done = paid[key] = []
    done_ints = {_as_int(x, -1) for x in done}
    fresh = [tier for tier in range(1, reached + 1) if tier not in done_ints]
    if not fresh:
        return []
    rewards = content_events.CAMPAIGN_TIER_REWARDS
    pids = _campaign_players(store, we, ctx)
    for tier in fresh:
        reward = _as_int(rewards[min(tier, len(rewards)) - 1], 0)
        for pid in pids:
            _add_reputation(store, pid, reward,
                            reason="campaign tier %d" % tier)
        done.append(int(tier))                 # record BEFORE paying out
    if LOG is not None:
        LOG("campaign %s: tiers %r crossed, %d reputation each to %r"
            % (key, fresh, rewards[0] if rewards else 0, pids))
    store.save()
    return fresh


def _campaign(store, now=None, ctx=None):
    """Refresh the campaign block, pay crossed tiers, return the summary."""
    we = _world_event_state(store)
    summary = content_events.campaign_sync(we, now)
    _pay_campaign_tiers(store, we, summary, ctx)
    return summary


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
    """The world event of the day, as ``WorldEvent`` (bare, no count prefix).

    ``content_events.event_for_day`` advances the rotation first (one chapter
    per UTC day, persisted in the store so a restart mid-day keeps the same
    event), then ``latest_event`` carries the two wire traps: f5 is INVERTED
    (running -> False) and f4 is a bit-packed ``Date``, not epoch seconds.
    """
    we = _world_event_state(store)
    if content_events.event_for_day(we):
        _send_event_mail(store, we.get("b"))
    # the campaign owns f6/f7/f4: a month of simulated worldside progress and
    # the tier payouts that come with crossing its ladder
    summary = _campaign(store, ctx=ctx)
    store.save()
    event = content_events.latest_event(event=we, campaign=summary)
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
    content_events.complete_mission(we, mission, instance)
    records = we.get("completedMissions")
    if (isinstance(records, list) and len(records) > before
            and isinstance(records[-1], dict)):
        records[-1]["pid"] = _ctx_pid(ctx)
        _credit_mission(store, _ctx_pid(ctx), records[-1])
    # The reply lands in the client's progress-MAX slot (ddl_region.c:8694:
    # ``*(impl + 0x58) = reply``), so it must be THIS campaign's target and not
    # 0 -- a 0 there makes the bar divide by zero right after solving a mission.
    result = max(1, _as_int(_campaign(store, ctx=ctx).get("target"), 1))
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
    PROFILE + "::UploadWorldStatsStencils_V1": _capture_upload(
        "worldStatsUploads", PROFILE, "UploadWorldStatsStencils_V1", None),
    PROFILE + "::GetReputationPoints_V1": h_get_reputation_points,
    PROFILE + "::AreReputationPointsMissing_V1": h_are_rep_missing,
    PROFILE + "::SetReputationPoints_V1": h_set_reputation_points,
    PROFILE + "::SolveDailyQuest_V1": h_solve_daily_quest,
    PROFILE + "::UploadFactionRewards_V1": _capture_upload(
        "factionRewards", PROFILE, "UploadFactionRewards_V1", _stats_reputation_rows),
    PROFILE + "::UploadStatsReputation_V1": _capture_upload(
        "statsReputation", PROFILE, "UploadStatsReputation_V1",
        _stats_reputation_rows),
    PROFILE + "::GetFacebookAccount_V1": h_get_facebook_account,
    PROFILE + "::SetFacebookAccount_V1": h_set_facebook_account,
    PROFILE + "::InvalidateAllReputationPoints_V1": h_invalidate_reputation,
    PROFILE + "::UploadReputationMails_V1": _capture_upload(
        "reputationMails", PROFILE, "UploadReputationMails_V1", _u32_rows),
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

    # ---- ChatServiceProtocol (12) ----
    CHAT + "::Initialize_V1": h_chat_void("Initialize_V1"),
    CHAT + "::SignalChatReady_V1": h_chat_void("SignalChatReady_V1"),
    CHAT + "::SignalChatNotReady_V1": h_chat_void("SignalChatNotReady_V1"),
    CHAT + "::GetChannels_V1": h_chat_channels,
    CHAT + "::GetIgnoreList_V1": h_chat_ignore_list,
    CHAT + "::Subscribe_V1": h_chat_subscribe,
    CHAT + "::Unsubscribe_V1": h_chat_unsubscribe,
    CHAT + "::SendToChannel_V1": h_chat_send_channel,
    CHAT + "::SendToUser_V1": h_chat_send_user,
    CHAT + "::SendToGroup_V1": (lambda store, r, ctx:
                                h_chat_send_user(store, r, ctx, "SendToGroup_V1")),
    CHAT + "::Ignore_V1": h_chat_ignore,
    CHAT + "::Unignore_V1": (lambda store, r, ctx:
                             h_chat_ignore(store, r, ctx, "Unignore_V1", True)),
    CHAT + "::BanPlayerFromChat_V1": h_chat_void("BanPlayerFromChat_V1"),
    CHAT + "::UnbanPlayerFromChat_V1": h_chat_void("UnbanPlayerFromChat_V1"),
    CHAT + "::GetBanInfo_V1": h_chat_void("GetBanInfo_V1"),
    CHAT + "::SendMessages_V1": h_chat_send_messages,

    # ---- FriendsServiceProtocol (6) ----
    FRIENDS + "::GetFriends_V1": h_get_friends,
    FRIENDS + "::GetRecentContacts_V1": h_get_recent_contacts,
    FRIENDS + "::GetFriendsWithActivations_V1": h_get_friends_with_activations,
    FRIENDS + "::AddFriend_V1": h_add_friend,
    FRIENDS + "::RemoveFriend_V1": h_remove_friend,
    FRIENDS + "::AddRecentContact_V1": h_add_recent_contact,
    FRIENDS + "::SetComment_V1": h_set_comment,
    FRIENDS + "::FindPlayers_V1": h_find_players,
    FRIENDS + "::FindFacebookFriends_V1": (lambda store, r, ctx:
                                           h_find_players(store, r, ctx,
                                                          "FindFacebookFriends_V1")),

    # ---- MailServiceProtocol (2) ----
    MAIL + "::GetMailCount_V1": h_mail_count,
    MAIL + "::GetMails_V1": h_get_mails,
    MAIL + "::GetMail_V1": h_get_mail,
    MAIL + "::SetReadFlag_V1": h_set_read_flag,
    MAIL + "::DeleteMail_V1": h_delete_mail,
    MAIL + "::DeleteAllMails_V1": h_delete_all_mails,
    MAIL + "::UploadMail_V1": h_upload_mail,
    MAIL + "::UploadMailToProfile_V1": (lambda store, r, ctx:
                                        h_mail_upload_pair(store, r, ctx,
                                                           "UploadMailToProfile_V1")),
    MAIL + "::UploadMailsToProfiles_V1": (lambda store, r, ctx:
                                          h_mail_upload_pair(store, r, ctx,
                                                             "UploadMailsToProfiles_V1")),
    MAIL + "::BroadcastMail_V1": (lambda store, r, ctx:
                                  h_mail_upload_pair(store, r, ctx, "BroadcastMail_V1")),
    MAIL + "::DeleteAllMailsByPid_V1": (lambda store, r, ctx:
                                        h_mail_upload_pair(store, r, ctx,
                                                           "DeleteAllMailsByPid_V1")),

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
        # chat / friends
        "Initialize_V1", "SignalChatReady_V1", "SignalChatNotReady_V1",
        "Subscribe_V1", "Unsubscribe_V1", "SendToChannel_V1", "SendToUser_V1",
        "SendToGroup_V1", "Ignore_V1", "Unignore_V1",
        "BanPlayerFromChat_V1", "UnbanPlayerFromChat_V1", "SendMessages_V1",
        "AddFriend_V1", "RemoveFriend_V1", "AddRecentContact_V1",
        "SetComment_V1",
        # mail
        "UploadMail_V1", "SetReadFlag_V1", "DeleteMail_V1", "DeleteAllMails_V1",
        "UploadMailToProfile_V1", "UploadMailsToProfiles_V1", "BroadcastMail_V1",
        "DeleteAllMailsByPid_V1",
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
    # upload capture: these store what the client pushes (they used to discard
    # the body, which is why "are statistics/achievements persisted?" was
    # unanswerable from a capture)
    "UploadStatsReputation_V1", "UploadFactionRewards_V1",
    "UploadReputationMails_V1", "SolveDailyQuest_V1",
    "UploadWorldStatsStencils_V1",
    "InvalidateAllReputationPoints_V1",
    "SetFacebookAccount_V1", "ActivateUniqueKey_V1", "AddActivation_V1",
    "AddShopItem_V1", "RemoveShopItem_V1", "UploadLeaderboardTemplates_V1",
    # chat / friends (these write the store)
    "Subscribe_V1", "Unsubscribe_V1", "SendToChannel_V1", "SendToUser_V1",
    "SendToGroup_V1", "Ignore_V1", "Unignore_V1",
    "AddFriend_V1", "RemoveFriend_V1", "AddRecentContact_V1", "SetComment_V1",
    "SendMessages_V1",
    # mail
    "SetReadFlag_V1", "DeleteMail_V1", "DeleteAllMails_V1",
    "UploadMailToProfile_V1", "UploadMailsToProfiles_V1", "BroadcastMail_V1",
    "DeleteAllMailsByPid_V1",
    # voting
    "Vote_V1", "UploadVotingTemplate_V1", "ClearVotingTemplates_V1",
    "UploadVoteMultipliers_V1",
    # world events
    "Register_V1", "Unregister_V1", "CompleteMission_V1", "SetVisited_V1",
    # GetLatestWorldEvent_V1 persists the day rotation
    "GetLatestWorldEvent_V1",
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
    ``body``   the request's **wire** body -- i.e. what the client sent,
    including the 4-byte request prologue (:data:`_REQUEST_PREFIX`).  A body
    shorter than 4 bytes is read from offset 0, so a hand-built
    argument-only body still parses.  No reply SHAPE depends on the body, so
    an empty body still produces a complete document -- but the writer methods
    do read it to decide *what* to write, and a body with no argument bytes
    means "write nothing": ``SetArkItems_V1`` with no arguments leaves the box
    ALONE (an explicit count of 0 is what clears it).  Pass the wire body for
    any method in :data:`WRITER_METHODS`.

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
            request = request_reader(body)
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
        request = request_reader(body)
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
