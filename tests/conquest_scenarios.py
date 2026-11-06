#!/usr/bin/env python3
"""The game's real conquest call sequences, as data.

Everything here comes from ``decomp/_adhoc/conquest-requests.md`` (referred to
below as *the report*; RVAs are the report's).  The module is deliberately
**pure data plus a tiny runner**: no sockets, no server import, no I/O.  The
test (``tools/test_conquest_scenarios.py``) drives these scenarios through
``tools/conquest_client.py`` against ``server/conquest_http.py``; the client's
CLI drives the very same data against a live host.

Evidence convention
-------------------
Each step carries two field lists:

``proven``
    Parameter names whose **presence and spelling** are proven by the report
    (section 4 lists every request's fields in body order, straight out of the
    24 request builders).

``inferred``
    Parameter names whose presence is proven but whose **value** is not, plus
    any field that is not in the report at all.  They are also marked with an
    inline ``# INFERRED`` comment at the value.

Nothing is sent that is not in the report's field list, with one deliberate
exception, marked ``# INFERRED (not in the report)``: the *error-path* scenario
needs a request that the server rejects, and the report does not describe one
(the game never sends a bad request), so it uses a file-less save upload.

Expectation vocabulary (all optional, ``kind`` is required)
-----------------------------------------------------------
``{"kind": "object"|"array", "keys": (...), "require": (...), "forbid": (...),
   "types": {...}, "values": {...}, "item_require": (...), "min_items": n,
   "max_bytes": n, "prefix": "{", "key_suffix": "_i", "any_of": (...) }``

* ``keys`` = exact top-level key set (the strongest assertion the corpus can
  support: PHP ``json_encode`` preserves order and every corpus family has a
  fixed key set);
* ``values`` = exact values; a string ``"@name"`` is looked up in the run
  context and compared with its *type* (int stays int);
* ``max_bytes``/``prefix`` are constants of the client, not of the report's
  prose: 255 B and a leading ``{`` (``0x0d8850``).

Context / substitution
----------------------
A step may ``capture`` response fields into the run context.  Params then use
``{name}`` (text substitution, like a form field) and ``values`` use
``@name`` (typed lookup).
"""

import re

__all__ = [
    "SCENARIOS", "ORDER", "StepResult", "Run", "run_scenario",
    "check_expectation", "obj", "arr", "step", "substitute",
]

PLAYER = "ProbePlayer"
#: Report section 5: ``Key`` is 32 hex characters (salted MD5).  The value is
#: opaque to the server - it must not be validated.
KEY = "0123456789abcdef0123456789abcdef"
PORTRAIT = 0
TITLE = 0

MISSION_INDEX = 0


# ---------------------------------------------------------------------------
# expectation builders (readable data, no classes)
# ---------------------------------------------------------------------------

def obj(require=(), keys=None, forbid=(), types=None, values=None,
        max_bytes=None, prefix=None, key_suffix=None):
    """An expected JSON object."""
    expect = {"kind": "object", "require": tuple(require), "forbid": tuple(forbid)}
    if keys is not None:
        expect["keys"] = tuple(keys)
    if types:
        expect["types"] = dict(types)
    if values:
        expect["values"] = dict(values)
    if max_bytes is not None:
        expect["max_bytes"] = int(max_bytes)
    if prefix is not None:
        expect["prefix"] = prefix
    if key_suffix is not None:
        expect["key_suffix"] = key_suffix
    return expect


def arr(item_require=(), min_items=0, max_items=None, types=None):
    """An expected JSON array."""
    expect = {"kind": "array", "min_items": int(min_items),
              "item_require": tuple(item_require)}
    if max_items is not None:
        expect["max_items"] = int(max_items)
    if types:
        expect["types"] = dict(types)
    return expect


def any_of(*expects):
    """Several legal answers; the runner records which one was observed."""
    return {"kind": "any", "any_of": list(expects)}


def step(name, params, expect, path=None, multipart=False, file=None,
         capture=None, source="", proven=(), inferred=(), note=""):
    """One request in a scenario."""
    return {
        "name": name,
        "path": path or ("/ConquestServer/%s.php" % name if "/" not in name
                         else "/ConquestServer/" + name),
        "params": dict(params),
        "expect": expect,
        "multipart": bool(multipart),
        "file": dict(file or {}),
        "capture": dict(capture or {}),
        "source": source,
        "proven": tuple(proven),
        "inferred": tuple(inferred),
        "note": note,
    }


# documents reused below --------------------------------------------------

ACK = obj(keys=("Return",), values={"Return": True},
          types={"Return": "bool"})
SESSION_ACK = obj(keys=("SessionID",), types={"SessionID": "int"})
TIMELIMIT = obj(keys=("timelimit",), types={"timelimit": "int"})
CLOUD_ACK = obj(keys=("CloudEnable", "skey", "bucket"),
                values={"CloudEnable": True},
                types={"CloudEnable": "bool", "skey": "str", "bucket": "str"},
                max_bytes=255, prefix="{")
ERROR_DOC = obj(keys=("error",), types={"error": "dict"})


def _error_doc_expect():
    """``{"error": {version, id, InfoText, vars}}`` with the nested key set."""
    expect = dict(ERROR_DOC)
    expect["nested"] = {"error": obj(keys=("version", "id", "InfoText", "vars"),
                                     types={"version": "int", "id": "int",
                                            "InfoText": "str",
                                            "vars": "list"})}
    return expect


ERROR_DOC = _error_doc_expect()

SESSION_DETAIL = obj(require=("SessionID", "version", "SessionState", "Team",
                              "MissionsDynamic"),
                     types={"SessionID": "int", "version": "int",
                            "SessionState": "int", "Team": "dict",
                            "MissionsDynamic": "list"})

BROWSE_LIST = arr(item_require=("SessionID", "sessionname", "state",
                                "MaxPlayersPerTeam", "SecondsLeft"))


# ---------------------------------------------------------------------------
# (a) startup - the order the corpus proves (report section 7.1, lines 0..5)
# ---------------------------------------------------------------------------

STARTUP = {
    "title": "startup: login, then the menu's chapter/session lists",
    "proves": ("the first bytes on the socket are a POST request line; login is "
               "the first request; the menu lists follow it; every reply is one "
               "JSON document with no trailing whitespace"),
    "context": {},
    "steps": [
        step(
            "PlayerLogin",
            {
                "PlayerName": PLAYER,      # INFERRED: any name; the field is proven
                "Key": KEY,                # PROVEN shape (32 hex), value opaque
                "Portrait": PORTRAIT,      # INFERRED: int, no value in the report
                "Title": TITLE,            # INFERRED: int
            },
            ACK,
            source="report 4 #1 (fields in body order), 3.5 (byte-exact first message), 7.1 line 0",
            proven=("PlayerName", "Key", "Portrait", "Title"),
            inferred=("Portrait", "Title"),
        ),
        step(
            "ConquestChapterList",
            {
                "PlayerName": PLAYER,      # INFERRED: value only
            },
            obj(),
            source="report 4 #2, 7.1 line 1/5 (chapter object at startup)",
            proven=("PlayerName",),
            note="the corpus's 65 KB {'0000':..} catalogue is this endpoint; an "
                 "empty store answers {} - both are objects, and routing is the "
                 "point of this step",
        ),
        step(
            "ConquestSessionList",
            {
                "PlayerName": PLAYER,      # INFERRED: value only
                "Key": KEY,                # PROVEN shape
            },
            BROWSE_LIST,
            source="report 4 #3, 7.1 line 4",
            proven=("PlayerName", "Key"),
        ),
        step(
            "ConquestOpenList",
            {
                "PlayerName": PLAYER,      # INFERRED: value only
                "Key": KEY,                # PROVEN shape
            },
            BROWSE_LIST,
            source="report 4 #4, 7.1 line 4 (public variant)",
            proven=("PlayerName", "Key"),
        ),
    ],
}


# ---------------------------------------------------------------------------
# (b) lobby browse - the menu traffic that never creates anything
# ---------------------------------------------------------------------------

LOBBY_BROWSE = {
    "title": "lobby browse: chapter catalogue, both lists, can-I-join",
    "proves": ("the read-only menu traffic works on the same connection; "
               "ConquestOpenList really is the public subset; CanJoinSession "
               "sends PlayerName only (the server must know the current session)"),
    "context": {},
    "steps": [
        step(
            "ConquestChapterList",
            {"PlayerName": PLAYER},                       # INFERRED: value only
            obj(),
            source="report 4 #2",
            proven=("PlayerName",),
        ),
        step(
            "ConquestSessionList",
            {"PlayerName": PLAYER, "Key": KEY},           # INFERRED: values
            BROWSE_LIST,
            source="report 4 #3",
            proven=("PlayerName", "Key"),
        ),
        step(
            "ConquestOpenList",
            {"PlayerName": PLAYER, "Key": KEY},           # INFERRED: values
            BROWSE_LIST,
            source="report 4 #4",
            proven=("PlayerName", "Key"),
        ),
        step(
            "CanJoinSession",
            {"PlayerName": PLAYER},                       # INFERRED: value only
            obj(keys=("Result",), types={"Result": "int"}),
            source="report 4 #8 ('PlayerName only'), 9 item 6 (Result is read)",
            proven=("PlayerName",),
            note="the value type/shape of Result has no corpus document - "
                 "INFERRED from the handler reading Result",
        ),
    ],
}


# ---------------------------------------------------------------------------
# (c) session create -> join -> start mission -> leave
# ---------------------------------------------------------------------------

SESSION_LIFECYCLE = {
    "title": "create -> join -> start mission -> leave (the in-game flow)",
    "proves": ("NewSession returns the SessionID that every later request must "
               "carry; SessionInfo answers with the full detail; StartMission's "
               "reply is <=255 bytes, starts with '{' and carries "
               "CloudEnable/skey/bucket; the whole flow rides one connection"),
    "context": {"sid": 0},
    "steps": [
        step(
            "NewSession",
            {
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape
                "Portrait": PORTRAIT,        # INFERRED: int
                "Title": TITLE,              # INFERRED: int
                "Team": "Team1",             # INFERRED: field proven, value spelling is not
                "ChapterTypeID": 4,          # INFERRED: int; 4 = corpus bucket
                                             # conquest_chapter_4
                "SessionName": "Probe Session",  # INFERRED: any string
                "SessionDuration": 336,      # INFERRED: int (corpus durations are day counts)
                "GameSpeed": 0,              # INFERRED: int/bool
                "TimeLimit": 0,              # INFERRED: int
                "MaxPlayers": 4,             # INFERRED: int
                "PublicGame": 1,             # INFERRED: int/bool; 1 so OpenList shows it
            },
            SESSION_ACK,
            source="report 4 #6 (12 fields, body order), 7.1 line 30",
            proven=("PlayerName", "Key", "Portrait", "Title", "Team",
                    "ChapterTypeID", "SessionName", "SessionDuration",
                    "GameSpeed", "TimeLimit", "MaxPlayers", "PublicGame"),
            inferred=("Team", "ChapterTypeID", "SessionName", "SessionDuration",
                      "GameSpeed", "TimeLimit", "MaxPlayers", "PublicGame"),
            capture={"sid": "SessionID"},
        ),
        step(
            "CanJoinSession",
            {"PlayerName": PLAYER},          # INFERRED: value only
            obj(keys=("Result",), types={"Result": "int"}),
            source="report 4 #8 ('PlayerName only')",
            proven=("PlayerName",),
            note="value shape INFERRED (no corpus document for Result)",
        ),
        step(
            "AddPlayerToSession",
            {
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape
                "Portrait": PORTRAIT,        # INFERRED: int
                "Title": TITLE,              # INFERRED: int
                "Team": "Team1",             # INFERRED: value spelling
                "SessionID": "{sid}",        # PROVEN field, value from NewSession
            },
            ACK,
            source="report 4 #7",
            proven=("PlayerName", "Key", "Portrait", "Title", "Team", "SessionID"),
            inferred=("Team",),
        ),
        step(
            "SessionInfo",
            {
                "ID": "{sid}",               # PROVEN (report 4 #5 sends ID)
            },
            SESSION_DETAIL,
            source="report 4 #5, 7.1 line 32",
            proven=("ID",),
            capture={"version": "version"},
        ),
        step(
            "StartMission",
            {
                "SessionID": "{sid}",        # PROVEN
                "MissionIndex": MISSION_INDEX,   # INFERRED: int
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape
            },
            CLOUD_ACK,
            source="report 4 #11, 6.4 (<=255 B, starts with '{'), 7.1 line 38",
            proven=("SessionID", "MissionIndex", "PlayerName", "Key"),
            inferred=("MissionIndex",),
            capture={"skey": "skey", "bucket": "bucket"},
        ),
        step(
            "JoinMission",
            {
                "SessionID": "{sid}",        # PROVEN
                "MissionIndex": MISSION_INDEX,   # INFERRED: int
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape
            },
            ACK,
            source="report 4 #12",
            proven=("SessionID", "MissionIndex", "PlayerName", "Key"),
            inferred=("MissionIndex",),
        ),
        step(
            "LeaveMission",
            {
                "SessionID": "{sid}",        # PROVEN
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape
            },
            ACK,
            source="report 4 #13 (also the StartMission fail path)",
            proven=("SessionID", "PlayerName", "Key"),
        ),
    ],
}


# ---------------------------------------------------------------------------
# (d) cloud save: multipart upload, then the download ack
# ---------------------------------------------------------------------------

#: A synthetic ``.ssw`` payload: binary, with NUL and high bytes, so a framing
#: bug (text-mode read, length off by the CRLF) cannot pass unnoticed.
SAVE_BYTES = (b"ANNO2070SWW\x00\x01\x02\xff\xfe" + bytes(range(256)) * 8)

CLOUD_SAVE = {
    "title": "cloud save: UploadSavegame (multipart) then the download ack",
    "proves": ("the one multipart request is framed correctly (binary file "
               "part, constant boundary, application/octet-stream for .ssw); "
               "the reply is the timelimit document; the follow-up StartMission "
               "returns the same cloud key, i.e. the save can be fetched again"),
    "context": {"sid": 0},
    "steps": [
        step(
            "NewSession",
            {
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape
                "Portrait": PORTRAIT,        # INFERRED: int
                "Title": TITLE,              # INFERRED: int
                "Team": "Team1",             # INFERRED: value spelling
                "ChapterTypeID": 4,          # INFERRED: int
                "SessionName": "Save Probe", # INFERRED: any string
                "SessionDuration": 336,      # INFERRED: int
                "GameSpeed": 0,              # INFERRED: int/bool
                "TimeLimit": 0,              # INFERRED: int
                "MaxPlayers": 4,             # INFERRED: int
                "PublicGame": 0,             # INFERRED: int/bool
            },
            SESSION_ACK,
            source="report 4 #6 (needed: a save belongs to a session/mission)",
            proven=("PlayerName", "Key", "Portrait", "Title", "Team",
                    "ChapterTypeID", "SessionName", "SessionDuration",
                    "GameSpeed", "TimeLimit", "MaxPlayers", "PublicGame"),
            inferred=("Team", "ChapterTypeID", "SessionName", "SessionDuration",
                      "GameSpeed", "TimeLimit", "MaxPlayers", "PublicGame"),
            capture={"sid": "SessionID"},
        ),
        step(
            "UploadSavegame",
            {
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape (upload_save's skey)
                "SessionID": "{sid}",        # PROVEN
                "MissionIndex": MISSION_INDEX,   # INFERRED: int
                "VCValue": 0,                # INFERRED: int
                "VCLevel": 0,                # INFERRED: int
                "SecondsPlayed": 60,         # INFERRED: int
            },
            TIMELIMIT,
            multipart=True,
            file={"field": "file", "filename": "myfile.ssw",
                  "content_type": "application/octet-stream", "data": SAVE_BYTES},
            source="report 4 #15, 4.1 (multipart layout), 7.1 line 38",
            proven=("PlayerName", "Key", "SessionID", "MissionIndex", "VCValue",
                    "VCLevel", "SecondsPlayed", "file"),
            inferred=("MissionIndex", "VCValue", "VCLevel", "SecondsPlayed"),
            note="file part: name='file', filename='myfile.png' is the literal "
                 "the client sends; a .ssw is application/octet-stream",
        ),
        step(
            "StartMission",
            {
                "SessionID": "{sid}",        # PROVEN
                "MissionIndex": MISSION_INDEX,   # INFERRED: int
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape
            },
            CLOUD_ACK,
            source="report 4 #11, 3.5/6.4, 7.1 line 38 (upload and download "
                   "are answered by the same family)",
            proven=("SessionID", "MissionIndex", "PlayerName", "Key"),
            inferred=("MissionIndex",),
            capture={"skey": "skey", "bucket": "bucket"},
            note="'download' = the game asks for the cloud key again; the two "
                 "acks must agree, the test compares them with the stored save",
        ),
        step(
            "SessionInfo",
            {
                "ID": "{sid}",               # PROVEN
            },
            SESSION_DETAIL,
            source="report 4 #5 - proves the binary upload did not desync "
                   "the stream (a later request still gets its own document)",
            proven=("ID",),
        ),
    ],
}


# ---------------------------------------------------------------------------
# (e) the 60 s poll pair: SessionInfo -> history
# ---------------------------------------------------------------------------

POLL_PAIR = {
    "title": "60 s poll: SessionInfo (Dynamic) then the chained history.php",
    "proves": ("the self-driven poll works without a reconnect; the history "
               "request is chained from the SessionInfo reply (0x0d5a30) with "
               "Start=<known> and End=-1; history is a 'N_i' map, so an empty "
               "map is legal and a list is not"),
    "context": {"sid": 0, "start": 0},
    "steps": [
        step(
            "SessionInfo",
            {
                "ID": "{sid}",               # PROVEN
                "Dynamic": 1,               # PROVEN field, INFERRED value (1)
            },
            obj(require=("SessionID", "version", "SessionState"),
                forbid=("Missions", "Links"),
                types={"SessionID": "int", "version": "int",
                       "SessionState": "int"}),
            source="report 4 #5 ('the tick/poll variant (0x0d7930) sends ID + "
                   "Dynamic'), 8 (<= one poll every 60 s)",
            proven=("ID", "Dynamic"),
            inferred=("Dynamic",),
            note="dynamic=True drops Missions/Links (shapes.session_detail); the "
                 "value of Dynamic is never shown in the report",
            capture={"version": "version"},
        ),
        step(
            "history",
            {
                "SessionID": "{sid}",        # PROVEN
                "Start": "{start}",          # INFERRED: 0 = 'nothing known yet'
                "End": -1,                   # PROVEN literal (0xffffffff)
            },
            obj(key_suffix="_i"),
            source="report 4 #10, 7 (chained from SessionInfo: Start=<entries "
                   "known>, End=-1)",
            proven=("SessionID", "Start", "End"),
            inferred=("Start",),
            note="Start's *value* depends on how many history entries the client "
                 "already has; the report proves the meaning, not the number",
        ),
    ],
}


# ---------------------------------------------------------------------------
# (f) the error path
# ---------------------------------------------------------------------------

ERROR_PATH = {
    "title": "error path: bad player parameter, then the error document",
    "proves": ("a request the server dislikes still gets one parseable JSON "
               "document on the same connection (never an HTTP error, never a "
               "drop); the error document's shape, whose error.id the game turns "
               "into its error code (0x0d7200)"),
    "context": {},
    "steps": [
        step(
            "ConquestChapterList",
            {
                # INFERRED: the report proves the *field* name, not that the
                # server validates it.  This is the corpus's only error family
                # ("Player parameter are incorrect", 1x), reduced to the HTTP
                # channel, where no request ever carries a bare "Player" field.
                "PlayerName": "",
            },
            any_of(
                ERROR_DOC,
                obj(forbid=("error",)),
            ),
            source="corpus error document (corpus/derived/fixtures/error.json); "
                   "report 7.1 has no error line, so this is the one unproven "
                   "behaviour of the channel",
            proven=(),
            inferred=("PlayerName",),
            note="the empty PlayerName is the 'bad player parameter'. Both "
                 "answers are legal as far as the corpus can prove: the error "
                 "document (2015 behaviour) or a normal reply (no validation). "
                 "The run prints which one was observed - that is the finding.",
        ),
        step(
            "UploadSavegame",
            {
                # INFERRED (not in the report): no *game* request reaches the
                # error document on an HTTP server, so this deliberately omits
                # the file part (the server's only BadRequest on a game path).
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape
                "SessionID": 0,              # INFERRED: value
                "MissionIndex": MISSION_INDEX,   # INFERRED: value
            },
            ERROR_DOC,
            multipart=True,
            file={},                         # no file part at all -> error doc
            source="server behaviour (conquest_http._h_upload_savegame raises "
                   "BadRequest); shape from corpus error.json",
            proven=("PlayerName", "Key", "SessionID", "MissionIndex"),
            inferred=("SessionID", "MissionIndex"),
            note="not a game-realisable request - the game always sends the "
                 "file part. It is the only provocation of the error document "
                 "that keeps the connection usable.",
        ),
        step(
            "PlayerLogin",
            {
                "PlayerName": PLAYER,        # INFERRED: value only
                "Key": KEY,                  # PROVEN shape
                "Portrait": PORTRAIT,        # INFERRED: int
                "Title": TITLE,              # INFERRED: int
            },
            ACK,
            source="report 4 #1",
            proven=("PlayerName", "Key", "Portrait", "Title"),
            inferred=("Portrait", "Title"),
            note="proves the error document did not desync the connection: the "
                 "next request still gets its own reply",
        ),
    ],
}


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

SCENARIOS = {
    "startup": STARTUP,
    "lobby_browse": LOBBY_BROWSE,
    "session_lifecycle": SESSION_LIFECYCLE,
    "cloud_save": CLOUD_SAVE,
    "poll_pair": POLL_PAIR,
    "error_path": ERROR_PATH,
}

#: The order the harness runs them in: the corpus's own order (section 7.1),
#: with the browse after a session exists so the lists have a row to carry.
ORDER = ("startup", "session_lifecycle", "lobby_browse", "cloud_save",
         "poll_pair", "error_path")


# ---------------------------------------------------------------------------
# expectation checking
# ---------------------------------------------------------------------------

_TYPES = {
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "str": lambda v: isinstance(v, str),
    "dict": lambda v: isinstance(v, dict),
    "list": lambda v: isinstance(v, list),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def _type_name(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    if value is None:
        return "null"
    return type(value).__name__


def _resolve(value, ctx):
    """``"@name"`` -> the typed context value; everything else unchanged."""
    if isinstance(value, str) and value.startswith("@"):
        return ctx.get(value[1:], value)
    return value


def _show(value, limit=80):
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def check_expectation(doc, expect, body=b"", ctx=None):
    """Return ``(problems, variant)``; ``variant`` is the index of an ``any_of``."""
    ctx = ctx or {}
    problems = []
    if expect is None:
        return problems, None

    if expect.get("kind") == "any":
        attempts = []
        for index, candidate in enumerate(expect.get("any_of") or ()):
            sub, _ = check_expectation(doc, candidate, body, ctx)
            attempts.append((index, sub))
        for index, sub in attempts:
            if not sub:
                return [], index
        # report the closest attempt
        index, sub = min(attempts, key=lambda item: len(item[1]))
        problems.append("none of the %d accepted shapes matched (closest: #%d)"
                        % (len(attempts), index))
        problems.extend(sub)
        return problems, None

    kind = expect.get("kind")
    if kind == "object" and not isinstance(doc, dict):
        return ["top level is %s, expected an object" % _type_name(doc)], None
    if kind == "array" and not isinstance(doc, list):
        return ["top level is %s, expected an array" % _type_name(doc)], None

    if "max_bytes" in expect:
        if len(body) > expect["max_bytes"]:
            problems.append("body is %d bytes, limit is %d"
                            % (len(body), expect["max_bytes"]))
    prefix = expect.get("prefix")
    if prefix is not None and body and not body.startswith(prefix.encode() if
                                                          isinstance(prefix, str)
                                                          else prefix):
        problems.append("body does not start with %r" % (prefix,))

    if kind == "object":
        keys = set(doc)
        if "keys" in expect and keys != set(expect["keys"]):
            missing = sorted(set(expect["keys"]) - keys)
            extra = sorted(keys - set(expect["keys"]))
            problems.append("key set differs: missing=%s extra=%s"
                            % (missing, extra))
        for name in expect.get("require", ()):
            if name not in doc:
                problems.append("missing required key %r" % (name,))
        for name in expect.get("forbid", ()):
            if name in doc:
                problems.append("key %r must not be present" % (name,))
        for name, want in (expect.get("types") or {}).items():
            if name in doc:
                check = _TYPES.get(want)
                if check is not None and not check(doc[name]):
                    problems.append("%s is %s, expected %s"
                                    % (name, _type_name(doc[name]), want))
        for name, want in (expect.get("values") or {}).items():
            if name not in doc:
                continue
            want = _resolve(want, ctx)
            if doc[name] != want:
                problems.append("%s is %s, expected %s"
                                % (name, _show(doc[name]), _show(want)))
        suffix = expect.get("key_suffix")
        if suffix:
            for name in doc:
                if not str(name).endswith(suffix):
                    problems.append("key %r does not end with %r" % (name, suffix))
                    break
        for name, sub in (expect.get("nested") or {}).items():
            if name in doc:
                sub_problems, _ = check_expectation(doc[name], sub, b"", ctx)
                problems.extend("%s.%s" % (name, item) for item in sub_problems)

    if kind == "array":
        if len(doc) < expect.get("min_items", 0):
            problems.append("array has %d items, at least %d expected"
                            % (len(doc), expect["min_items"]))
        if expect.get("max_items") is not None and len(doc) > expect["max_items"]:
            problems.append("array has %d items, at most %d expected"
                            % (len(doc), expect["max_items"]))
        for index, item in enumerate(doc):
            if not isinstance(item, dict):
                problems.append("item %d is %s, expected an object"
                                % (index, _type_name(item)))
                continue
            for name in expect.get("item_require", ()):
                if name not in item:
                    problems.append("item %d is missing %r" % (index, name))
                    break
    return problems, None


# ---------------------------------------------------------------------------
# running a scenario
# ---------------------------------------------------------------------------

_REF = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute(value, ctx):
    """Replace ``{name}`` in strings; recurse through dicts/lists."""
    if isinstance(value, str):
        def repl(match):
            key = match.group(1)
            return str(ctx[key]) if key in ctx else match.group(0)
        return _REF.sub(repl, value)
    if isinstance(value, dict):
        return {key: substitute(item, ctx) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [substitute(item, ctx) for item in value]
    return value


class StepResult(object):
    """Outcome of one scenario step (transport errors included)."""

    __slots__ = ("step", "reply", "problems", "error", "variant", "params")

    def __init__(self, step, reply=None, problems=None, error=None,
                 variant=None, params=None):
        self.step = step
        self.reply = reply
        self.problems = list(problems or [])
        self.error = error
        self.variant = variant
        self.params = params or {}

    @property
    def ok(self):
        return not self.problems and self.error is None

    @property
    def name(self):
        return self.step.get("name")

    def __repr__(self):
        return "StepResult(%s, %s)" % (self.name,
                                       "ok" if self.ok else self.problems or self.error)


class Run(object):
    """A scenario run: per-step results plus the context they built."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.results = []
        self.ctx = {}
        self.error = None
        self.transport_problems = []

    @property
    def ok(self):
        return self.error is None and all(result.ok for result in self.results)

    @property
    def passed(self):
        return sum(1 for result in self.results if result.ok)

    def __repr__(self):
        return "Run(%s, %d/%d)" % (self.scenario.get("title", "?"),
                                   self.passed, len(self.results))


def _files_for(step):
    spec = step.get("file") or {}
    if not spec:
        return []
    return [(spec.get("field", "file"),
             spec.get("filename", "myfile.png"),
             spec.get("content_type", "application/octet-stream"),
             spec.get("data", b""))]


def run_scenario(client, scenario, ctx=None, overrides=None, stop_on_fail=True):
    """Drive one scenario through ``client``; return a :class:`Run`.

    ``client`` needs only ``post(name, params)`` and
    ``post_multipart(name, params, files)`` - ``conquest_client.ConquestClient``
    provides both.  ``ctx`` (a dict) is updated in place with captured values,
    so a caller can chain scenarios (the SessionID from one is the ``ID`` of the
    next).  ``stop_on_fail`` stops at the first failing step: the channel is
    positional, so a desync makes every later result meaningless.
    """
    run = Run(scenario)
    ctx = ctx if isinstance(ctx, dict) else {}
    for key, value in (scenario.get("context") or {}).items():
        ctx.setdefault(key, value)
    ctx.update(overrides or {})
    run.ctx = ctx

    for item in scenario.get("steps") or ():
        params = substitute(item.get("params") or {}, ctx)
        # overrides (the client CLI's --player) win over the scenario literal,
        # but only where the step actually sends that field
        for key, value in (overrides or {}).items():
            if key in params:
                params[key] = value
        try:
            if item.get("multipart"):
                reply = client.post_multipart(item["name"], params,
                                              _files_for(item))
            else:
                reply = client.post(item["name"], params)
        except Exception as exc:                      # noqa: BLE001 - reported
            result = StepResult(item, error=exc, params=params)
            run.results.append(result)
            run.error = exc
            break

        problems, variant = check_expectation(reply.doc, item.get("expect"),
                                              reply.body, ctx)
        for key, field in (item.get("capture") or {}).items():
            if not isinstance(reply.doc, dict) or field not in reply.doc:
                problems.append("cannot capture %r: not in the response" % (field,))
            else:
                ctx[key] = reply.doc[field]
        result = StepResult(item, reply=reply, problems=problems,
                            variant=variant, params=params)
        run.results.append(result)
        if problems and stop_on_fail:
            break
    return run
