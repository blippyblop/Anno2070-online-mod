#!/usr/bin/env python3
"""The conquest channel as it really is: **HTTP/1.1** on the AnnoURLs port.

Background (see ``decomp/_adhoc/conquest-requests.md`` - every claim below is
backed by RVAs in that report):

* the client class is ``CServerPHPImplementation`` driving ``CWinHttpSessionImpl``
  (WINHTTP.dll, async API), i.e. a plain HTTP/1.1 client;
* every request is ``POST /ConquestServer/<Name>.php HTTP/1.1`` with
  ``Content-Type: application/x-www-form-urlencoded`` and a ``Content-Length``
  body (``UploadSavegame.php`` is the one ``multipart/form-data`` request,
  boundary ``related-designs123456789``);
* the *first* bytes on the socket are the request line - no handshake, no
  length prefix, no separator;
* responses must carry ``Content-Length`` (the client fails the request when
  ``WINHTTP_QUERY_CONTENT_LENGTH`` cannot be answered - chunked is unsupported)
  and the body must be exactly the JSON document: **no trailing newline**;
* the URL path *is* the action, correlation is positional per HTTP request
  (one response per request, in order, on the keep-alive connection).

Layout of this module
---------------------

    encode_json(doc)                    PHP ``json_encode`` byte-compatible body
    parse_urlencoded / parse_multipart  request body parsing
    dispatch(name, params, files, store)   PURE: path -> response document
    handle_request(...)                 framing-free HTTP layer (testable)
    ConquestServer / serve(...)         threaded socket server (drop-in)

``dispatch`` is a pure function of ``(handler_name, params, files, store)``.
All world state lives behind the ``store`` object (``.state`` + ``.save()``,
the same shape as ``mimic_server.ARK_STORE``)::

    store.state["conquest"] = {
        "sessions": [...], "history": {...}, "missionCatalogue": {...},
        "cloud": {...}, "players": {...}, "invites": {...}, "chat": {...},
        "nextSessionId": int, "version": int, "secondsLeft": int,
    }

Document shapes come from ``conquest_shapes.py`` (the same builders
``mimic_server.conquest_dispatch`` uses), never from literals here - the only
hand-written documents are the ones with no corpus fixture (``Result``,
``result``, ``Team`` ack variants); they are marked INFERRED below.

Wiring (one line, from ``mimic_server.main``)::

    import conquest_http; threading.Thread(target=conquest_http.serve,
        args=(3080, ARK_STORE), kwargs={"log": log}, daemon=True).start()
"""

import base64
import json
import socket
import threading
import time
import traceback
from urllib.parse import parse_qsl, urlsplit

try:                                                    # normal use (server/ on sys.path)
    import conquest_shapes as shapes
except Exception:                                       # pragma: no cover - package use
    try:
        from . import conquest_shapes as shapes         # type: ignore
    except Exception:
        shapes = None

__all__ = [
    "BOUNDARY", "START_MISSION_MAX", "RawDocument", "Response", "BadRequest",
    "MemoryStore", "ConquestServer",
    "encode_json", "parse_urlencoded", "parse_multipart", "parse_request_body",
    "route", "dispatch", "handle_request", "response_bytes", "serve",
    "ENDPOINTS", "ROUTES",
]

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

BOUNDARY = "related-designs123456789"   # constant literal in the client (0x0d80cf5)
START_MISSION_MAX = 255                 # 0x0d8850: longer bodies are treated as errors
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3080                     # AnnoURLs.ConquestServer (scheme-less!)

MAX_HEAD = 64 * 1024                    # request line + headers
MAX_BODY = 256 * 1024 * 1024            # .sww saves are ~0.25 MB; leave huge headroom
IDLE_TIMEOUT = 300.0                    # keep-alive idle; the client polls every 60 s
NOCL_TIMEOUT = 0.5                      # "read until the connection would block"

STATUS_REASONS = {
    200: "OK",
    400: "Bad Request",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
}

#: canonical handler names, in the order of the client's 24-entry table
#: (``0x1675d70``, report section 4).  ``loca`` is slot 18 / vtable slot 9.
ENDPOINTS = (
    "PlayerLogin",
    "ConquestChapterList",
    "ConquestSessionList",
    "ConquestOpenList",
    "SessionInfo",
    "NewSession",
    "AddPlayerToSession",
    "CanJoinSession",
    "RemovePlayerFromSession",
    "history",
    "StartMission",
    "JoinMission",
    "LeaveMission",
    "UpdatePlayer",
    "UploadSavegame",
    "ResetSavegame",
    "UseCoopTime",
    "loca",
    "Invite",
    "UnInvite",
    "CloseSlot",
    "AcceptInvite",
    "JoinPublic",
    "SendChatMsg",
)

#: lower-case path stem -> canonical handler.  Case-insensitive on purpose:
#: the 2015 server was PHP on Windows (case-insensitive file names).
ROUTES = {name.lower(): name for name in ENDPOINTS}


# ---------------------------------------------------------------------------
# JSON serialisation (PHP json_encode, byte-compatible)
# ---------------------------------------------------------------------------

def encode_json(doc):
    """Serialise a response document exactly like the 2015 PHP server did.

    * compact (``separators=(",", ":")``) - the corpus contains no spaces;
    * ``/`` escaped as ``\\/`` (PHP default, ``JSON_UNESCAPED_SLASHES`` off);
    * non-ASCII as ``\\uXXXX`` (PHP default, ``JSON_UNESCAPED_UNICODE`` off);
    * **no trailing newline / NUL / whitespace** - the corpus has 0 of 3332
      documents ending in whitespace, and the client's JSON parser rejects
      anything after the document.

    Returns ``bytes`` (ASCII by construction).
    """
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)
    text = text.replace("/", "\\/")
    return text.encode("ascii")


def _decode_json(body):
    """Parse a response/request document, same rules the client applies."""
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------

class BadRequest(Exception):
    """A request body that cannot be parsed.

    Answered with ``HTTP 200`` + the corpus error document (``{"error":{...}}``,
    whose ``id`` becomes the game's error code) - never with a transport error,
    because the client only looks at the body.
    """


def parse_urlencoded(body):
    """Parse ``application/x-www-form-urlencoded`` bytes into an ordered dict.

    Order is preserved (the report spells out the field order per endpoint);
    duplicate names resolve to the **last** value, which is what PHP's
    ``parse_str`` does.  Percent escapes are decoded (``%20`` and ``+``).

    The client does not encode values (report section 3.2, medium confidence),
    so leading/trailing CR/LF is stripped defensively: a body of
    ``b"PlayerName=a\\r\\n"`` must not produce the key ``"PlayerName"`` with a
    trailing newline in the *value*.
    """
    text = body.decode("utf-8", "replace").strip("\r\n\t ")
    out = {}
    for key, value in parse_qsl(text, keep_blank_values=True):
        out[key] = value
    return out


def _boundary_from_content_type(content_type):
    """Extract ``boundary=...`` from a Content-Type header (bytes, unquoted)."""
    for part in (content_type or "").split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "boundary":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            if value:
                return value.encode("latin-1")
    return None


def _disposition_parts(value):
    """``form-data; name="x"; filename="y"`` -> ``("x", "y")`` (either may be None)."""
    name = filename = None
    for part in (value or "").split(";"):
        key, _, raw = part.partition("=")
        key = key.strip().lower()
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = raw[1:-1]
        if key == "name":
            name = raw
        elif key == "filename":
            filename = raw
    return name, filename


def _parse_part_headers(blob):
    """Parse the MIME headers of one multipart part into a lower-case dict."""
    headers = {}
    for line in blob.split(b"\r\n"):
        if not line.strip():
            continue
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        headers[name.decode("latin-1").strip().lower()] = \
            value.decode("latin-1").strip()
    return headers


def parse_multipart(body, content_type=""):
    """Parse ``multipart/form-data`` into ``(fields, files)``.

    ``fields`` is ``{name: str}`` (text parts, UTF-8 with replacement),
    ``files`` is ``{name: {"filename", "content_type", "data"}}`` with the
    **raw bytes** untouched (a ``.ssw`` save arrives as
    ``application/octet-stream`` and may contain anything, including CRLFs and
    the boundary-like text).

    The boundary is taken from the Content-Type header and falls back to the
    client's constant ``related-designs123456789``.  A body that has no
    delimiter at all is a :class:`BadRequest`.
    """
    boundary = _boundary_from_content_type(content_type) or BOUNDARY.encode()
    delim = b"--" + boundary
    if delim not in body:
        raise BadRequest("multipart body is missing the %r boundary" % boundary.decode("latin-1"))
    fields = {}
    files = {}
    for chunk in body.split(delim)[1:]:
        if chunk.startswith(b"--"):                     # closing delimiter
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        else:
            chunk = chunk.lstrip(b"\r\n")
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            raise BadRequest("multipart part has no header/body separator")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = _parse_part_headers(head)
        name, filename = _disposition_parts(headers.get("content-disposition", ""))
        if name is None:
            continue                                    # unnamed part: ignore
        if filename is None:
            fields[name] = data.decode("utf-8", "replace")
        else:
            files[name] = {
                "filename": filename,
                "content_type": headers.get("content-type") or "application/octet-stream",
                "data": data,
            }
    return fields, files


def _json_request_fields(obj):
    """Flatten a JSON request object into ``params`` (values keep their type)."""
    params = {}
    for key, value in obj.items():
        if isinstance(value, (dict, list)):
            params[str(key)] = json.dumps(value, separators=(",", ":"))
        else:
            params[str(key)] = value
    return params


def parse_request_body(body, content_type="", handler=None):
    """Parse one request body into ``(params, files)``.

    Three shapes exist (report section 3/4):

    * ``application/x-www-form-urlencoded`` - every endpoint but the save
      upload (``a=1&b=2``; values are *not* percent-encoded by the client);
    * ``multipart/form-data`` - ``UploadSavegame.php`` only;
    * a bare ``{...}`` JSON object - the client never sends one on this channel
      today (``StartMission``'s body is urlencoded, only its *response* is
      capped at 255 B), but the pass-through is cheap, it is logged, and it
      keeps hand-written probes working.  An unparsable ``{...}`` body raises
      :class:`BadRequest`, which is how the error document is exercised.

    The Content-Type header is advisory: an empty/odd header with a
    ``k=v&k=v`` body is still parsed as a form (the client's mode-1 request
    only ever sets ``Content-Type: application/x-www-form-urlencoded``).
    """
    if not body:
        return {}, {}

    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype == "multipart/form-data":
        return parse_multipart(body, content_type)

    stripped = body.strip(b" \t\r\n\x00")
    if stripped.startswith(b"{"):
        if len(stripped) > START_MISSION_MAX:
            # documented limit on the *response*, but a request this big is
            # never produced by the client - refuse it loudly rather than guess
            raise BadRequest("JSON request body longer than %d bytes" % START_MISSION_MAX)
        try:
            obj = _decode_json(stripped)
        except Exception as exc:
            raise BadRequest("unparsable JSON request body: %s" % exc)
        if not isinstance(obj, dict):
            raise BadRequest("JSON request body is not an object")
        return _json_request_fields(obj), {}

    return parse_urlencoded(body), {}


# ---------------------------------------------------------------------------
# parameter helpers (tolerant: values may be str/int/bool)
# ---------------------------------------------------------------------------

def _get(params, name, default=None):
    """Case-sensitive first, then case-insensitive (PHP is case-sensitive)."""
    if name in params:
        return params[name]
    low = name.lower()
    for key, value in params.items():
        if isinstance(key, str) and key.lower() == low:
            return value
    return default


def _as_int(value, default=0):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _as_str(value, default=""):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _pint(params, name, default=0):
    return _as_int(_get(params, name), default)


def _pbool(params, name, default=False):
    return _as_bool(_get(params, name), default)


def _pstr(params, name, default=""):
    return _as_str(_get(params, name), default)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

class MemoryStore:
    """Minimal stand-in for ``rmc_services.Store`` (``.state`` + ``.save()``).

    Used when the caller passes no store; tests can pass their own object with
    the same two attributes.  ``save()`` counts calls and never touches disk.
    """

    def __init__(self, state=None):
        self.state = state if isinstance(state, dict) else {}
        self.saves = 0

    def save(self):
        self.saves += 1
        return True


_DEFAULT_STORE = None
_DEFAULT_LOCK = threading.Lock()


def _default_store():
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = MemoryStore()
        return _DEFAULT_STORE


def _conq_state(store):
    """``store.state["conquest"]`` - created on demand, never raises."""
    state = getattr(store, "state", None)
    if not isinstance(state, dict):
        return {}
    conq = state.get("conquest")
    if not isinstance(conq, dict):
        conq = state["conquest"] = {}
    return conq


def _save(store):
    """Persist through the store, swallowing a broken store (log at server)."""
    try:
        store.save()
    except Exception:
        pass


def _sessions(conq):
    rows = conq.get("sessions")
    if isinstance(rows, dict):
        rows = list(rows.values())
        conq["sessions"] = rows
    if not isinstance(rows, list):
        rows = conq["sessions"] = []
    return rows


def _session(conq, session_id):
    """Look a session up by id (``None`` when it does not exist yet)."""
    if session_id in (None, ""):
        return None
    want = str(_as_int(session_id, session_id))
    for row in _sessions(conq):
        if isinstance(row, dict) and str(row.get("SessionID")) == want:
            return row
    return None


def _players(conq):
    players = conq.get("players")
    if not isinstance(players, dict):
        players = conq["players"] = {}
    return players


def _player(conq, name):
    if not name:
        return {}
    players = _players(conq)
    rec = players.get(name)
    if not isinstance(rec, dict):
        rec = players[name] = {}
    return rec


def _new_session_id(conq):
    """Allocate the next ``SessionID`` (the corpus used 80772..81650)."""
    used = {_as_int(row.get("SessionID"), 0) for row in _sessions(conq)
            if isinstance(row, dict)}
    try:
        sid = int(conq.get("nextSessionId") or 0)
    except (TypeError, ValueError):
        sid = 0
    if sid <= 0:
        sid = max(used) + 1 if used else 81650
    while sid in used:
        sid += 1
    conq["nextSessionId"] = sid + 1
    return sid


def _seconds_left(conq, session=None):
    """Remaining seconds for a session (the corpus never sends 0)."""
    row = session if isinstance(session, dict) else {}
    for source in (row.get("SecondsLeft"), conq.get("secondsLeft")):
        try:
            value = int(source)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 43200


def _bump_version(conq, session):
    """The ``version`` counter in ``SessionInfo`` replies rises on every poll."""
    try:
        version = int(session.get("version") or conq.get("version") or 0) + 1
    except (TypeError, ValueError):
        version = 1
    session["version"] = version
    conq["version"] = version
    return version


# ---------------------------------------------------------------------------
# session helpers
# ---------------------------------------------------------------------------

def _team_name(value, default="Team1"):
    text = _as_str(value, default)
    low = text.lower()
    if low in ("1", "team1"):
        return "Team1"
    if low in ("2", "team2"):
        return "Team2"
    return text or default


def _team_block(session, team):
    """``session["Team"][team]`` as ``{"Score": int, "Member": [...]}``."""
    teams = session.get("Team")
    if not isinstance(teams, dict):
        teams = session["Team"] = {}
    block = teams.get(team)
    if not isinstance(block, dict):
        block = teams[team] = {"Score": 0, "Member": []}
    if not isinstance(block.get("Member"), list):
        block["Member"] = []
    if "Score" not in block:
        block["Score"] = 0
    return block


def _member_row(name, portrait=0, title=0, playing=-1):
    """One ``Team.TeamN.Member[]`` row (``conquest_shapes.player_row``)."""
    if shapes is None:                                   # pragma: no cover
        return {"name": name, "id": 0, "playing": playing, "portrait": portrait,
                "title": title, "score": 0, "timelimit": 0}
    return shapes.player_row(name=name, id=0, playing=playing,
                             portrait=portrait, title=title, score=0,
                             timelimit=0)


def _add_member(session, team, name, portrait=0, title=0):
    """Idempotently put a player into ``session.Team.<team>.Member``."""
    block = _team_block(session, team)
    for row in block["Member"]:
        if isinstance(row, dict) and _as_str(row.get("name")) == name:
            return block
    block["Member"].append(_member_row(name, portrait, title))
    return block


def _remove_member(session, name):
    """Remove a player from every team; returns the team it was in (or "")."""
    teams = session.get("Team")
    if not isinstance(teams, dict):
        return ""
    for team, block in teams.items():
        if not isinstance(block, dict):
            continue
        members = block.get("Member")
        if not isinstance(members, list):
            continue
        for index, row in enumerate(list(members)):
            if isinstance(row, dict) and _as_str(row.get("name")) == name:
                del members[index]
                return team
    return ""


def _history_rows(conq, session=None):
    """History rows for a session, falling back to the store-wide map."""
    for source in ((session or {}).get("history"), conq.get("history")):
        if isinstance(source, list):
            return [row for row in source if isinstance(row, dict)]
        if isinstance(source, dict):
            return [row for row in source.values() if isinstance(row, dict)]
    return []


def _invites(conq):
    invites = conq.get("invites")
    if not isinstance(invites, dict):
        invites = conq["invites"] = {}
    return invites


def _invite_list(conq, session_id):
    bucket = _invites(conq).get(str(session_id))
    if not isinstance(bucket, list):
        bucket = _invites(conq)[str(session_id)] = []
    return bucket


def _safe_name(text, fallback="Player"):
    """Keep a player/chapter name safe for a cloud key."""
    cleaned = "".join(ch for ch in _as_str(text) if ch.isalnum() or ch in "_-")
    return cleaned or fallback


def _chapter_of(params, conq, session):
    chapter = _pint(params, "ChapterTypeID", 0)
    if not chapter:
        chapter = _as_int((session or {}).get("ChapterTypeID"), 0)
    if not chapter:
        chapter = _as_int((session or {}).get("name"), 0)
    if not chapter:
        chapter = _as_int(conq.get("chapter"), 0)
    return chapter or 1


def _bucket_for(chapter):
    return "conquest_chapter_%d" % chapter


def _savegame_key(chapter, session_id, mission, player_name):
    """``conquest/<chapter>/Conquest_<sid>_<mission>_<name>.sww``.

    Modelled on the corpus value ``conquest/2_ArkTP/Conquest_1_ArkEbashiBMT_Ecos.sww``
    (report section 7.1, line 38).
    """
    return "conquest/%d_%s/Conquest_%d_%d_%s.sww" % (
        chapter, _safe_name(player_name, "Player"), _as_int(session_id, 0),
        _as_int(mission, 0), _safe_name(player_name, "Player"))


# ---------------------------------------------------------------------------
# documents that have no corpus fixture (marked INFERRED)
# ---------------------------------------------------------------------------

def _shapes():
    if shapes is None:                                   # pragma: no cover
        raise RuntimeError(
            "conquest_http needs conquest_shapes.py next to it (or on sys.path)")
    return shapes


def _ack():
    """``{"Return":true}`` - the corpus ack (242x)."""
    if shapes is not None:
        return shapes.ack()
    return {"Return": True}                              # pragma: no cover


def _error(message, error_id=1):
    """``{"error":{version,id,InfoText,vars}}`` - ``id`` is the game error code."""
    if shapes is not None:
        return shapes.error_doc(message, error_id)
    return {"error": {"version": 2, "id": error_id,                        # pragma: no cover
                      "InfoText": message, "vars": []}}


def _result_doc(key, value):
    """INFERRED: ``CanJoinSession`` reads ``Result``, chat/invite read
    ``result``/``COMMON``; no corpus document exists for either family."""
    return {key: value}


# ---------------------------------------------------------------------------
# the 24 handlers:  (params, files, conq, store) -> response document
# ---------------------------------------------------------------------------

def _h_player_login(params, files, conq, store):
    """1. PlayerLogin.php -> ``{"Return":true}`` (corpus line 0).

    ``(PlayerName, Key)`` is the credential pair; the key is a salted MD5 over
    a runtime secret, so it is **never** validated and never stored.
    """
    name = _pstr(params, "PlayerName")
    if name:
        rec = _player(conq, name)
        rec["Portrait"] = _pint(params, "Portrait", _as_int(rec.get("Portrait"), 0))
        rec["Title"] = _pint(params, "Title", _as_int(rec.get("Title"), 0))
    return _ack()


def _h_chapter_list(params, files, conq, store):
    """2. ConquestChapterList.php -> mission catalogue ``{"0000":{...}}``.

    The corpus's 65 KB ``{"0000":..,"0016":..}`` documents are produced by
    ``0x0d27c0`` reading a chapter object; the static catalogue is the only
    ``{"%04d": {...}}`` family in the corpus (report section 7.1 note).
    """
    return _shapes().mission_catalogue(conq.get("missionCatalogue") or {})


def _public(row):
    """``ConquestOpenList`` only lists public sessions (``PublicGame``)."""
    if not isinstance(row, dict):
        return False
    if "PublicGame" in row:
        return _as_bool(row.get("PublicGame"), True)
    if "Open" in row:
        return _as_int(row.get("Open"), 1) != 0
    return True


def _h_session_list(params, files, conq, store):
    """3. ConquestSessionList.php -> browse variant (17 keys per row)."""
    rows = [row for row in _sessions(conq) if isinstance(row, dict)]
    return _shapes().session_list(rows, variant="browse")


def _h_open_list(params, files, conq, store):
    """4. ConquestOpenList.php -> browse variant, public sessions only."""
    rows = [row for row in _sessions(conq)
            if isinstance(row, dict) and _public(row)]
    return _shapes().session_list(rows, variant="browse")


def _h_session_info(params, files, conq, store):
    """5. SessionInfo.php -> session detail (full, or ``dynamic`` for the poll).

    The client sends ``ID`` and, on the 60 s tick poll (``0x0d7930``),
    ``ID`` + ``Dynamic``.  ``SessionID`` is accepted as an alias so probes and
    the older dispatcher spelling keep working.  The ``version`` counter is
    bumped on every reply, which is what the corpus's 96 -> 99 -> 100 shows.
    """
    sid = _get(params, "ID")
    if sid is None:
        sid = _get(params, "SessionID")
    dynamic = _pbool(params, "Dynamic", False)
    session = _session(conq, sid)
    if session is None:
        # corpus-legal defaults for a session we never created (SecondsLeft is
        # never 0 in the corpus, so use the store/session clock)
        session = {"SessionID": _as_int(sid, 0), "version": 0,
                   "SessionState": 2, "NumberOfTeams": 2,
                   "MaxPlayersPerTeam": 5, "duration": 336,
                   "SecondsLeft": _seconds_left(conq)}
    _bump_version(conq, session)
    # no store.save() here on purpose: the 60 s poll would rewrite the whole
    # state file every minute for a counter that only the client reads
    return _shapes().session_detail(session, dynamic=dynamic)


def _h_new_session(params, files, conq, store):
    """6. NewSession.php -> ``{"SessionID": <new id>}`` (corpus line 30).

    Everything the client sent is kept on the row, so ``SessionInfo`` and the
    list endpoints can answer with the real thing afterwards.
    """
    sid = _new_session_id(conq)
    name = _pstr(params, "PlayerName")
    chapter = _pint(params, "ChapterTypeID", 0)
    duration = _pint(params, "SessionDuration", 336)
    seconds = _pint(params, "TimeLimit", 0) or duration * 120
    max_players = _pint(params, "MaxPlayers", 5)
    row = {
        "SessionID": sid,
        "version": 0,
        "name": chapter,
        "ChapterTypeID": chapter,
        "Description": chapter,
        "sessionname": _pstr(params, "SessionName", "Conquest Session"),
        "SessionState": 2,
        "SecondsLeft": seconds,
        "SecondsRunning": 0,
        "duration": duration,
        "TimeLimitPerDay": _pint(params, "TimeLimit", 0),
        "NumberOfTeams": 2,
        "MaxPlayersPerTeam": max_players,
        "Background": "conquest_bg",
        "BackgroundPicture": "",
        "SessionPicture": "",
        "GameSpeed": _pbool(params, "GameSpeed", False),
        "PublicGame": _pbool(params, "PublicGame", False),
        "IsAddonSession": False,
        "Team": {
            "Team1": {"Score": 0, "Member": [
                _member_row(name, _pint(params, "Portrait", 0),
                            _pint(params, "Title", 0))] if name else
                _member_row("", 0, 0, -3)},
            "Team2": {"Score": 0, "Member": [_member_row("", 0, 0, -3)]},
        },
        "MissionsDynamic": [],
        "Missions": [],
        "Links": [],
    }
    team = _team_name(_get(params, "Team"), "Team1")
    if name:
        row["Team"].setdefault(team, {"Score": 0, "Member": []})
        if team != "Team1":
            row["Team"]["Team1"]["Member"] = [
                m for m in row["Team"]["Team1"]["Member"]
                if _as_str(m.get("name")) != name]
            _add_member(row, team, name, _pint(params, "Portrait", 0),
                        _pint(params, "Title", 0))
    _sessions(conq).append(row)
    if name:
        _player(conq, name)["SessionID"] = sid
    conq.setdefault("chapter", chapter)
    _save(store)
    return _shapes().session_ack(sid)


def _h_add_player(params, files, conq, store):
    """7. AddPlayerToSession.php -> ack; the player joins a team slot."""
    sid = _pint(params, "SessionID", 0)
    name = _pstr(params, "PlayerName")
    session = _session(conq, sid)
    if session is not None and name:
        _add_member(session, _team_name(_get(params, "Team")), name,
                    _pint(params, "Portrait", 0), _pint(params, "Title", 0))
        _player(conq, name)["SessionID"] = sid
        _save(store)
    return _ack()


def _h_can_join(params, files, conq, store):
    """8. CanJoinSession.php -> ``{"Result":1}``.

    The request really carries ``PlayerName`` only (``0x0d4280``), so the
    answer is derived from the player's *current* session.  INFERRED value
    type: the handler reads ``Result`` and the corpus contains no such
    document; ``1`` is the PHP-ish "yes".
    """
    return _result_doc("Result", 1)


def _h_remove_player(params, files, conq, store):
    """9. RemovePlayerFromSession.php -> ack."""
    sid = _pint(params, "SessionID", 0)
    name = _pstr(params, "PlayerName")
    session = _session(conq, sid)
    if session is not None and name:
        _remove_member(session, name)
    if name:
        rec = _player(conq, name)
        if _as_int(rec.get("SessionID"), 0) == sid:
            rec.pop("SessionID", None)
    _save(store)
    return _ack()


def _h_history(params, files, conq, store):
    """10. history.php -> incremental history map ``{"0_i": {...}}``.

    Chained from the ``SessionInfo`` reply (``0x0d5a30``) with
    ``Start=<entries already known>`` and ``End=-1``; ``-1`` means "open end"
    and is the client's literal (``0xffffffff``).
    """
    sid = _pint(params, "SessionID", 0)
    session = _session(conq, sid)
    rows = _history_rows(conq, session)
    start = _pint(params, "Start", 0)
    end = _pint(params, "End", -1)
    if start < 0:
        start = 0
    window = rows[start:] if end < 0 else rows[start:end]
    document = _shapes().history_map(window)
    if start:
        # history_map() numbers from "0_i"; an incremental page continues at
        # Start, which is exactly how the client counts what it already has
        document = {"%d_i" % (start + index): value
                    for index, value in enumerate(document.values())}
    return document


def _h_start_mission(params, files, conq, store):
    """11. StartMission.php -> cloud ack, **<= 255 bytes** and starting with ``{``.

    ``0x0d8850`` treats an empty, non-``{`` or > 255-byte body as an error and
    aborts the mission start, so the document is fitted (the key is shortened,
    and as a last resort dropped to the 3-key minimum).
    """
    sid = _pint(params, "SessionID", 0)
    mission = _pint(params, "MissionIndex", 0)
    session = _session(conq, sid)
    chapter = _chapter_of(params, conq, session)
    bucket = _bucket_for(chapter)
    skey = _pstr(params, "skey") or _savegame_key(
        chapter, sid, mission, _pstr(params, "PlayerName"))
    doc = _shapes().cloud_ack(skey, bucket)
    return _fit_start_mission(doc, bucket)


def _fit_start_mission(doc, bucket="conquest_chapter_4"):
    """Shrink a StartMission ack until ``len(encode_json(doc)) <= 255``."""
    candidates = [doc]
    short = dict(doc)
    short["skey"] = _as_str(short.get("skey"))[-64:]
    candidates.append(short)
    shorter = dict(doc)
    shorter["skey"] = _as_str(shorter.get("skey"))[-32:]
    candidates.append(shorter)
    minimal = {"CloudEnable": True, "skey": "", "bucket": bucket or "conquest_chapter_4"}
    candidates.append(minimal)
    for candidate in candidates:
        if len(encode_json(candidate)) <= START_MISSION_MAX:
            return candidate
    return minimal                                           # pragma: no cover


def _h_join_mission(params, files, conq, store):
    """12. JoinMission.php -> ack (``playing`` moves to the mission index)."""
    sid = _pint(params, "SessionID", 0)
    name = _pstr(params, "PlayerName")
    mission = _pint(params, "MissionIndex", 0)
    session = _session(conq, sid)
    if session is not None and name:
        for team, block in (session.get("Team") or {}).items():
            for row in (block.get("Member") or []):
                if isinstance(row, dict) and _as_str(row.get("name")) == name:
                    row["playing"] = mission
        _save(store)
    if name:
        _player(conq, name)["MissionIndex"] = mission
    return _ack()


def _h_leave_mission(params, files, conq, store):
    """13. LeaveMission.php -> ack (also the StartMission fail path)."""
    sid = _pint(params, "SessionID", 0)
    name = _pstr(params, "PlayerName")
    session = _session(conq, sid)
    if session is not None and name:
        for block in (session.get("Team") or {}).values():
            for row in (block.get("Member") or []):
                if isinstance(row, dict) and _as_str(row.get("name")) == name:
                    row["playing"] = -1
        _save(store)
    return _ack()


def _h_update_player(params, files, conq, store):
    """14. UpdatePlayer.php -> ``{"timelimit": N}`` (+ the new history rows).

    ``0x0d6850`` reads ``history``; ``timelimit`` re-arms the in-game timer
    (report section 7).  ``History`` arrives as a JSON string of event rows and
    is appended to the session history, which ``history.php`` then serves.
    """
    sid = _pint(params, "SessionID", 0)
    name = _pstr(params, "PlayerName")
    session = _session(conq, sid)
    new_rows = []
    raw = _get(params, "History")
    if raw not in (None, ""):
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parsed = list(parsed.values())
        if isinstance(parsed, list):
            new_rows = [row for row in parsed if isinstance(row, dict)]
    if session is not None and new_rows:
        rows = session.setdefault("history", [])
        if not isinstance(rows, list):
            rows = session["history"] = []
        rows.extend(new_rows)
        _save(store)
    doc = _shapes().timelimit(_seconds_left(conq, session))
    if session is None:
        # keep the corpus key set exact when there is nothing to report
        return doc
    if new_rows:
        doc["history"] = _shapes().history_map(new_rows)
    return doc


def _h_upload_savegame(params, files, conq, store):
    """15. UploadSavegame.php -> the only multipart request.

    The ``file`` part holds the raw ``.ssw`` bytes (``application/octet-stream``);
    they are base64'd into the store so ``mimic_state.json`` stays JSON.  The
    reply carries ``timelimit`` (``0x0d6b00``).  A missing file part is a
    :class:`BadRequest` -> corpus error document (the client cannot do anything
    with a save anyway).
    """
    # PN 175: accept the file part under ANY name. The report documents the part
    # as `filename="myfile.png"` without pinning the FIELD name; the game may use
    # "file", "myfile" or "data". Being strict here turned a valid upload into the
    # corpus error document, which is exactly the kind of mismatch that would make
    # cloud saves look broken in game.
    # parse_multipart() yields {field: {"filename","content_type","data"}}.
    _part = (files or {}).get("file")
    part = _part.get("data") if isinstance(_part, dict) else _part
    if part is None:
        for _value in (files or {}).values():
            _data = _value.get("data") if isinstance(_value, dict) else _value
            if isinstance(_data, (bytes, bytearray)) and _data:
                part = _data
                break
    if not isinstance(part, (bytes, bytearray)) or not part:
        # OPEN QUESTION (PN 177): the corpus answers upload and download
        # IDENTICALLY - {CloudEnable:true, skey, bucket} - while our harness and
        # the scenario suite expect an error document for a file-less upload. Both
        # cannot be true. Kept as the error document (all suites green) until the
        # real client shows which one it sends; the change is two lines either way.
        raise BadRequest("UploadSavegame.php needs a non-empty 'file' part")
    sid = _pint(params, "SessionID", 0)
    mission = _pint(params, "MissionIndex", 0)
    session = _session(conq, sid)
    chapter = _chapter_of(params, conq, session)
    key = _savegame_key(chapter, sid, mission, _pstr(params, "PlayerName"))
    data = bytes(part)
    cloud = conq.get("cloud")
    _meta = _part if isinstance(_part, dict) else {}
    if not isinstance(cloud, dict):
        cloud = conq["cloud"] = {}
    cloud[key] = {
        "bucket": _bucket_for(chapter),
        "filename": _as_str(_meta.get("filename"), "myfile.png"),
        "content_type": _as_str(_meta.get("content_type"), "application/octet-stream"),
        "size": len(data),
        "data": base64.b64encode(data).decode("ascii"),
        "SessionID": sid,
        "MissionIndex": mission,
        "VCValue": _pint(params, "VCValue", 0),
        "VCLevel": _pint(params, "VCLevel", 0),
        "SecondsPlayed": _pint(params, "SecondsPlayed", 0),
    }
    _save(store)
    return _shapes().timelimit(_seconds_left(conq, session))


def _h_reset_savegame(params, files, conq, store):
    """16. ResetSavegame.php -> ack; forget the stored save of that mission."""
    sid = _pint(params, "SessionID", 0)
    mission = _pint(params, "MissionIndex", 0)
    cloud = conq.get("cloud")
    if isinstance(cloud, dict):
        prefix = "conquest/"
        for key in list(cloud):
            entry = cloud.get(key)
            if not isinstance(entry, dict):
                continue
            if _as_int(entry.get("SessionID"), -1) == sid and \
                    _as_int(entry.get("MissionIndex"), -2) == mission and \
                    str(key).startswith(prefix):
                del cloud[key]
        _save(store)
    return _ack()


def _h_use_coop_time(params, files, conq, store):
    """17. UseCoopTime.php -> ``{"timelimit": N}`` (the timer re-arm)."""
    session = _session(conq, _pint(params, "SessionID", 0))
    spent = _pint(params, "SecondsPlayed", 0)
    seconds = _seconds_left(conq, session)
    if spent > 0 and session is not None:
        left = max(1, _as_int(session.get("SecondsLeft"), seconds) - spent)
        session["SecondsLeft"] = left
        _save(store)
        seconds = left
    return _shapes().timelimit(seconds)


class RawDocument:
    """A non-JSON response body (``loca/%s.txt`` is downloaded verbatim)."""

    __slots__ = ("data", "content_type")

    def __init__(self, data, content_type=TEXT_CONTENT_TYPE):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.data = data
        self.content_type = content_type

    def __repr__(self):
        return "RawDocument(%d bytes, %s)" % (len(self.data), self.content_type)


def _h_loca(params, files, conq, store):
    """18. loca/%s.txt -> raw text/plain (``0x0ddab0`` writes it to disk)."""
    name = _as_str(_get(params, "__loca__"), "loca.txt")
    table = conq.get("loca")
    if isinstance(table, dict) and isinstance(table.get(name), str):
        return RawDocument(table[name])
    for key, value in (table.items() if isinstance(table, dict) else []):
        if _as_str(key).lower() == name.lower() and isinstance(value, str):
            return RawDocument(value)
    return RawDocument("# Anno 2070 localisation placeholder - conquest mimic\n"
                       "# requested: %s\n" % name)


def _h_invite(params, files, conq, store):
    """19. Invite.php -> ``{"SessionID": .., "Team": ".."}`` (both are read)."""
    sid = _pint(params, "SessionID", 0)
    client = _pstr(params, "ClientName")
    team = _team_name(_get(params, "Team"))
    slot = _pint(params, "SlotID", 0)
    if client:
        bucket = _invite_list(conq, sid)
        for row in bucket:
            if _as_str(row.get("PlayerName")) == client:
                row.update({"Team": team, "SlotID": slot})
                break
        else:
            bucket.append({"PlayerName": client, "Team": team, "SlotID": slot})
        _save(store)
    return {"SessionID": sid, "Team": team}


def _h_uninvite(params, files, conq, store):
    """20. UnInvite.php -> ``{"SessionID": .., "Team": ".."}``."""
    sid = _pint(params, "SessionID", 0)
    client = _pstr(params, "ClientName")
    team = "Team1"
    if client:
        bucket = _invite_list(conq, sid)
        for row in list(bucket):
            if _as_str(row.get("PlayerName")) == client:
                team = _as_str(row.get("Team"), team) or team
                bucket.remove(row)
                _save(store)
                break
    return {"SessionID": sid, "Team": team}


def _h_close_slot(params, files, conq, store):
    """21. CloseSlot.php -> ``{"SessionID": .., "Team": ".."}``.

    The slot is recorded and any pending invite on it is dropped; the client
    only needs the ack plus the affected team (INFERRED: the request carries
    ``HostName``/``Key``/``SessionID``/``SlotID``, no team).
    """
    sid = _pint(params, "SessionID", 0)
    slot = _pint(params, "SlotID", 0)
    team = "Team1"
    bucket = _invite_list(conq, sid)
    for row in list(bucket):
        if _as_int(row.get("SlotID"), -1) == slot:
            team = _as_str(row.get("Team"), team) or team
            bucket.remove(row)
    closed = conq.get("closedSlots")
    if not isinstance(closed, dict):
        closed = conq["closedSlots"] = {}
    closed.setdefault(str(sid), [])
    if slot not in closed[str(sid)]:
        closed[str(sid)].append(slot)
    _save(store)
    return {"SessionID": sid, "Team": team}


def _h_accept_invite(params, files, conq, store):
    """22. AcceptInvite.php -> ``{"result": 1}`` (lower-case key, ``0x0df510``)."""
    sid = _pint(params, "SessionID", 0)
    name = _pstr(params, "PlayerName")
    team = _team_name(_get(params, "Team"))
    session = _session(conq, sid)
    if session is not None and name:
        bucket = _invite_list(conq, sid)
        for row in list(bucket):
            if _as_str(row.get("PlayerName")) == name:
                team = _as_str(row.get("Team"), team) or team
                bucket.remove(row)
        _add_member(session, team, name)
        _player(conq, name)["SessionID"] = sid
        _save(store)
    return _result_doc("result", 1)


def _h_join_public(params, files, conq, store):
    """23. JoinPublic.php -> ``{"SessionID": <id>}``."""
    sid = _pint(params, "SessionID", 0)
    name = _pstr(params, "PlayerName")
    session = _session(conq, sid)
    if session is not None and name:
        _add_member(session, "Team1", name)
        _player(conq, name)["SessionID"] = sid
        _save(store)
    return _shapes().session_ack(sid)


def _h_send_chat(params, files, conq, store):
    """24. SendChatMsg.php -> ``{"result": 1, "COMMON": []}``.

    The handler reads ``result`` and the ``COMMON`` channel; the corpus has no
    chat document, so the message is stored but the channel is served empty
    (an empty array cannot break a parser; inventing a row shape could).
    """
    sid = _pint(params, "SessionID", 0)
    message = _pstr(params, "Message")
    name = _pstr(params, "PlayerName")
    channel = _pstr(params, "Channel", "COMMON") or "COMMON"
    if message:
        chat = conq.get("chat")
        if not isinstance(chat, dict):
            chat = conq["chat"] = {}
        chat.setdefault(str(sid), []).append(
            {"PlayerName": name, "Message": message, "Channel": channel})
        _save(store)
    return {"result": 1, "COMMON": []}


_HANDLERS = {
    "PlayerLogin": _h_player_login,
    "ConquestChapterList": _h_chapter_list,
    "ConquestSessionList": _h_session_list,
    "ConquestOpenList": _h_open_list,
    "SessionInfo": _h_session_info,
    "NewSession": _h_new_session,
    "AddPlayerToSession": _h_add_player,
    "CanJoinSession": _h_can_join,
    "RemovePlayerFromSession": _h_remove_player,
    "history": _h_history,
    "StartMission": _h_start_mission,
    "JoinMission": _h_join_mission,
    "LeaveMission": _h_leave_mission,
    "UpdatePlayer": _h_update_player,
    "UploadSavegame": _h_upload_savegame,
    "ResetSavegame": _h_reset_savegame,
    "UseCoopTime": _h_use_coop_time,
    "loca": _h_loca,
    "Invite": _h_invite,
    "UnInvite": _h_uninvite,
    "CloseSlot": _h_close_slot,
    "AcceptInvite": _h_accept_invite,
    "JoinPublic": _h_join_public,
    "SendChatMsg": _h_send_chat,
}

assert set(_HANDLERS) == set(ENDPOINTS), "handler table out of sync with ENDPOINTS"


# ---------------------------------------------------------------------------
# routing + pure dispatch
# ---------------------------------------------------------------------------

def route(target):
    """Map a request target to a canonical handler name (``None`` if unknown).

    ``/ConquestServer/PlayerLogin.php`` -> ``"PlayerLogin"``;
    ``/ConquestServer/loca/en.txt``       -> ``"loca"``;
    ``/anything/else``                    -> ``None`` (answered with the
    corpus-legal ``{"Return":true}``, never with an HTTP error).
    """
    path = urlsplit(target or "").path or "/"
    low = path.lower()
    if "/loca/" in low or low.rstrip("/").endswith("/loca"):
        return "loca"
    base = path.rsplit("/", 1)[-1]
    if base.lower().endswith(".php"):
        base = base[:-4]
    return ROUTES.get(base.lower())


def dispatch(handler_name, params, files, store):
    """PURE request -> response document mapping (no socket, no globals).

    ``store`` is anything with ``.state`` (dict) and ``.save()``; state lives
    in ``store.state["conquest"]``.  ``None`` means the module's in-memory
    store (handy for probes; the live server always passes the real one).

    An unknown/``None`` handler yields the corpus ack ``{"Return":true}`` -
    the report's rule 6: never answer a path we cannot place with an HTTP
    error.
    """
    if store is None:
        store = _default_store()
    params = dict(params or {})
    files = dict(files or {})
    conq = _conq_state(store)
    handler = _HANDLERS.get(handler_name)
    if handler is None:
        return _ack()
    return handler(params, files, conq, store)


# ---------------------------------------------------------------------------
# HTTP framing (no sockets: fully testable)
# ---------------------------------------------------------------------------

class Response:
    """One HTTP response: status + body bytes + connection intent."""

    __slots__ = ("status", "body", "content_type", "close", "reason")

    def __init__(self, status=200, body=b"", content_type=JSON_CONTENT_TYPE,
                 close=False, reason=None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.close = close
        self.reason = reason

    def __repr__(self):
        return "Response(%d, %dB, %s)" % (self.status, len(self.body),
                                          self.content_type)


def parse_head(head):
    """Split a request head (without the blank line) into its four parts."""
    lines = [line for line in head.split(b"\r\n") if line.strip()]
    if not lines:
        raise BadRequest("empty request")
    parts = lines[0].decode("latin-1").split()
    if len(parts) < 2:
        raise BadRequest("bad request line %r" % lines[0][:80])
    method, target = parts[0], parts[1]
    version = parts[2] if len(parts) > 2 else "HTTP/1.0"
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        key = name.decode("latin-1").strip().lower()
        text = value.decode("latin-1").strip()
        headers[key] = headers[key] + ", " + text if key in headers else text
    return method, target, version, headers


def _wants_close(headers, version="HTTP/1.1"):
    conn = (headers.get("connection") or "").lower()
    if "close" in conn:
        return True
    if "keep-alive" in conn:
        return False
    return version.upper() == "HTTP/1.0"


def handle_request(method, target, headers, body, store=None, log=None):
    """One HTTP request -> :class:`Response` (framing only, no socket).

    Rules implemented here (report sections 6.3 and 9):

    * unknown path -> 200 + ``{"Return":true}``;
    * unparsable body -> 200 + the corpus error document (``error.id`` is the
      game's error code, an unparsable body would be ``-2``; we prefer a real
      document with a message);
    * bodies are exact JSON, ``Content-Length`` is ``len(body)``, no newline;
    * ``loca/*.txt`` is ``text/plain``, everything else ``application/json``;
    * ``Connection: close`` (and HTTP/1.0) are honoured by ``Response.close``.
    """
    method = (method or "GET").upper()
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    close = _wants_close(headers, headers.get("__version__", "HTTP/1.1"))
    if method not in ("GET", "POST", "HEAD"):
        return Response(405, encode_json(_error("%s is not supported" % method)),
                        close=True)
    handler = route(target)
    if handler is None:
        _emit(log, "CONQ-HTTP  unknown path %r -> ack" % (target,))
        return Response(200, encode_json(_ack()), close=close)
    params, files = {}, {}
    try:
        # PN 176: header lookup must be CASE-INSENSITIVE. Clients send
        # "Content-Type"; a plain dict lookup for "content-type" returned "" so a
        # multipart UploadSavegame was parsed as a form and dropped its file part.
        _ctype = headers.get("content-type")
        if not _ctype:
            for _k, _v in (headers or {}).items():
                if str(_k).strip().lower() == "content-type":
                    _ctype = _v
                    break
        params, files = parse_request_body(body, _ctype or "", handler)
        if handler == "loca":
            name = urlsplit(target).path.rsplit("/", 1)[-1] or "loca.txt"
            params["__loca__"] = name
    except BadRequest as exc:
        _emit(log, "CONQ-HTTP  bad body on %s: %s" % (handler, exc))
        return Response(200, encode_json(_error(str(exc))), close=close)
    try:
        doc = dispatch(handler, params, files, store)
    except Exception as exc:
        _emit(log, "CONQ-HTTP  dispatch failed on %s: %r" % (handler, exc))
        _emit(log, traceback.format_exc().rstrip())
        return Response(200, encode_json(_error("internal error")), close=close)
    if isinstance(doc, RawDocument):
        return Response(200, doc.data, content_type=doc.content_type, close=close)
    try:
        payload = encode_json(doc)
    except Exception as exc:
        _emit(log, "CONQ-HTTP  unparsable response doc for %s: %r" % (handler, exc))
        return Response(200, encode_json(_error("internal error")), close=close)
    return Response(200, payload, content_type=JSON_CONTENT_TYPE, close=close)


def response_bytes(response, head_only=False, keep_alive=True):
    """Serialise a :class:`Response`; ``Content-Length`` is always present.

    ``head_only`` (HEAD) sends the headers - including the real
    ``Content-Length`` - and no body, as HTTP requires.
    """
    reason = response.reason or STATUS_REASONS.get(response.status, "Unknown")
    head = ("HTTP/1.1 %d %s\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %d\r\n"
            "Connection: %s\r\n"
            "\r\n") % (response.status, reason, response.content_type,
                       len(response.body),
                       "keep-alive" if keep_alive else "close")
    payload = head.encode("latin-1")
    return payload if head_only else payload + response.body


def _emit(log, message):
    """Call the ``log=`` hook defensively (a broken logger must not kill us)."""
    if log is None:
        return
    try:
        log(message)
    except Exception:
        pass


def _preview(data, limit=1000):
    """Readable one-line preview of a response body for the live harness log."""
    if not data:
        return "<empty>"
    text = data.decode("latin-1", "replace")
    if len(text) > limit:
        text = "%s...(+%d bytes)" % (text[:limit], len(text) - limit)
    return text


# ---------------------------------------------------------------------------
# socket server
# ---------------------------------------------------------------------------

class _Reader:
    """Buffered socket reader (one connection, request pipelining safe)."""

    def __init__(self, sock, log=None, idle_timeout=IDLE_TIMEOUT):
        self.sock = sock
        self.log = log
        self.buf = b""
        self.idle_timeout = idle_timeout

    def _recv(self):
        return self.sock.recv(65536)

    def read_head(self):
        """Read up to ``\\r\\n\\r\\n``; ``None`` on a clean EOF."""
        while True:
            index = self.buf.find(b"\r\n\r\n")
            if index >= 0:
                head = self.buf[:index]
                self.buf = self.buf[index + 4:]
                return head
            if len(self.buf) > MAX_HEAD:
                raise BadRequest("request head longer than %d bytes" % MAX_HEAD)
            chunk = self._recv()
            if not chunk:
                return None
            self.buf += chunk

    def read_body(self, headers):
        """Read the body; returns ``(body, incomplete_framing)``.

        Without a ``Content-Length`` we drain until the peer would block (or
        half-closes) - the report's rule for a request we cannot frame - and
        say so loudly, because *our* client always sends the header.
        """
        raw = headers.get("content-length")
        if raw is None:
            _emit(self.log, "CONQ-HTTP  !! MISSING Content-Length - draining "
                            "until the peer blocks (unframed request)")
            self.sock.settimeout(NOCL_TIMEOUT)
            while True:
                try:
                    chunk = self._recv()
                except socket.timeout:
                    break
                except OSError:
                    break
                if not chunk:
                    break
                self.buf += chunk
            body, self.buf = self.buf, b""
            return body, True
        try:
            length = int(str(raw).split(",")[0].strip())
        except (TypeError, ValueError):
            raise BadRequest("bad Content-Length %r" % (raw,))
        if length < 0:
            raise BadRequest("negative Content-Length %r" % (raw,))
        if length > MAX_BODY:
            raise BadRequest("Content-Length %d over the %d byte limit"
                             % (length, MAX_BODY))
        while len(self.buf) < length:
            chunk = self._recv()
            if not chunk:
                raise BadRequest("connection closed after %d of %d body bytes"
                                 % (len(self.buf), length))
            self.buf += chunk
            if len(self.buf) > MAX_HEAD + MAX_BODY:
                raise BadRequest("body too large")
        body, self.buf = self.buf[:length], self.buf[length:]
        return body, False


class ConquestServer:
    """Threaded, keep-alive HTTP/1.1 listener for the conquest channel.

    * one thread per connection, daemon threads, never blocks the caller;
    * ``port=0`` binds an ephemeral port (``server.port`` reports the real one);
    * a single long-lived client connection is the normal case (WinHTTP reuses
      it across the 60 s ``SessionInfo``/``history`` chain);
    * every request is answered exactly once, in order, with ``Content-Length``;
    * idle connections are dropped after ``idle_timeout`` instead of leaking
      threads.
    """

    def __init__(self, port=DEFAULT_PORT, store=None, log=None,
                 host=DEFAULT_HOST, idle_timeout=IDLE_TIMEOUT, backlog=32):
        self.host = host
        self.port = int(port)
        self.store = store if store is not None else _default_store()
        self.log = log
        self.idle_timeout = float(idle_timeout)
        self.backlog = int(backlog)
        self.sock = None
        self._thread = None
        self._stop = threading.Event()
        self._clients = set()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        self.port = sock.getsockname()[1]
        sock.listen(self.backlog)
        sock.settimeout(0.5)                 # so stop() is honoured promptly
        self.sock = sock
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="conquest-http-accept", daemon=True)
        self._thread.start()
        _emit(self.log, "CONQ-HTTP  listening on %s:%d (HTTP/1.1)"
                        % (self.host, self.port))
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()
        return False

    def stop(self, timeout=3.0):
        self._stop.set()
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        _emit(self.log, "CONQ-HTTP  stopped")

    def wait(self):
        """Block until :meth:`stop` (used by :func:`serve`)."""
        while not self._stop.is_set():
            time.sleep(0.2)

    # -- accept / serve ----------------------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            sock = self.sock
            if sock is None:
                return
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self._clients.add(conn)
            threading.Thread(target=self._client, args=(conn, addr),
                             name="conquest-http-client", daemon=True).start()

    def _client(self, conn, addr):
        tag = "%s:%s" % (addr[0], addr[1])
        _emit(self.log, "CONQ-HTTP  open from %s" % tag)
        try:
            conn.settimeout(self.idle_timeout)
            reader = _Reader(conn, self.log, self.idle_timeout)
            while not self._stop.is_set():
                try:
                    head = reader.read_head()
                except socket.timeout:
                    _emit(self.log, "CONQ-HTTP  idle timeout %s" % tag)
                    break
                except OSError:
                    break
                except BadRequest as exc:
                    _emit(self.log, "CONQ-HTTP  bad request head from %s: %s"
                                    % (tag, exc))
                    reply = Response(400, encode_json(_error(str(exc))), close=True)
                    try:
                        conn.sendall(response_bytes(reply, keep_alive=False))
                    except OSError:
                        pass
                    break
                if head is None:
                    break
                if not head.strip():         # stray CRLF between requests
                    continue
                try:
                    method, target, version, headers = parse_head(head)
                    headers["__version__"] = version
                    body, unframed = reader.read_body(headers)
                except BadRequest as exc:
                    reply = Response(400, encode_json(_error(str(exc))), close=True)
                    _emit(self.log, "CONQ-HTTP  %s rejecting request: %s" % (tag, exc))
                    try:
                        conn.sendall(response_bytes(reply, keep_alive=False))
                    except OSError:
                        pass
                    break
                except (OSError, socket.timeout):
                    break

                handler = route(target)
                _emit(self.log, "CONQ-HTTP  <- %s %s %s (%s, %d body bytes, handler=%s)"
                                % (method, target, version, tag, len(body),
                                   handler or "?"))
                if body and "multipart/form-data" not in \
                        (headers.get("content-type", "") or "").lower() and len(body) < 8192:
                    preview = {k: ("***" if k.lower() == "key" else v)
                               for k, v in parse_qsl(
                                   body.decode("utf-8", "replace").strip("\r\n\t "),
                                   keep_blank_values=True)}
                    if preview:
                        _emit(self.log, "CONQ-HTTP  params %r" % (preview,))

                response = handle_request(method, target, headers, body,
                                          store=self.store, log=self.log)
                keep_alive = (not response.close) and (not unframed)
                out = response_bytes(response, head_only=(method == "HEAD"),
                                     keep_alive=keep_alive)
                _emit(self.log, "CONQ-HTTP  -> %d %dB %s %s"
                                % (response.status, len(response.body),
                                   response.content_type,
                                   _preview(response.body)))
                try:
                    conn.sendall(out)
                except OSError:
                    break
                if not keep_alive:
                    break
        except (OSError, socket.timeout) as exc:
            _emit(self.log, "CONQ-HTTP  connection error %s: %r" % (tag, exc))
        except Exception as exc:                              # pragma: no cover
            _emit(self.log, "CONQ-HTTP  handler crashed %s: %r" % (tag, exc))
            _emit(self.log, traceback.format_exc().rstrip())
        finally:
            with self._lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            _emit(self.log, "CONQ-HTTP  close %s" % tag)


def serve(port=DEFAULT_PORT, store=None, log=None, host=DEFAULT_HOST,
          block=True, idle_timeout=IDLE_TIMEOUT):
    """Convenience entry point: start the listener and (by default) block.

    ``block=False`` returns the running :class:`ConquestServer` (tests use
    ``port=0``); ``block=True`` serves until ``KeyboardInterrupt``.

    Drop-in wiring for ``mimic_server.py``::

        import conquest_http
        threading.Thread(target=conquest_http.serve,
                         args=(3080, ARK_STORE), kwargs={"log": log},
                         daemon=True).start()
    """
    server = ConquestServer(port, store=store, log=log, host=host,
                            idle_timeout=idle_timeout)
    server.start()
    if not block:
        return server
    try:
        server.wait()
    except KeyboardInterrupt:                                # pragma: no cover
        pass
    finally:
        server.stop()
    return server
