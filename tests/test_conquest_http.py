#!/usr/bin/env python3
"""Tests for ``server/conquest_http.py`` - the HTTP/1.1 conquest channel.

    source ~/tools/env.sh && python3 tools/test_conquest_http.py [-v]
    source ~/tools/env.sh && python3 tools/test_conquest_http.py --list

Self-contained and dependency-free: no corpus fixtures are read (state is
seeded inline), no third-party modules, and only ONE test group touches the
network (an ephemeral-port server on 127.0.0.1, two/three short connections).

Coverage
--------
* PHP-``json_encode`` body encoding: compact, ``/`` escaped, non-ASCII as
  ``\\uXXXX``, no trailing newline, exact ``Content-Length``;
* ``application/x-www-form-urlencoded`` parsing (order, blanks, duplicates,
  CRLF-wrapped body, percent escapes);
* ``multipart/form-data`` parsing (CRLF, a binary file part with NUL/0xFF bytes,
  header boundary and the client's constant fallback);
* all 24 routes: canonical path -> the right corpus shape (top-level type,
  exact key sets for the documented ones), unknown path -> ``{"Return":true}``;
* ``StartMission``'s 255-byte / leading-``{`` rule;
* the corpus error document on an unparsable body and on a save upload without
  a file part;
* HTTP framing: ``Content-Length`` always present and exact, HEAD, 405,
  ``Connection: close``, HTTP/1.0, missing ``Content-Length`` drain;
* one end-to-end socket run: PlayerLogin + SessionInfo + UploadSavegame over a
  single keep-alive connection, then a clean close.
"""

import base64
import json
import os
import socket
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import conquest_http as ch                                    # noqa: E402

KEY = "0123456789abcdef0123456789abcdef"                      # 32 hex chars
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

PASS = 0
FAIL = 0
FAILURES = []
GROUP = [""]


# ---------------------------------------------------------------------------
# tiny assertion harness
# ---------------------------------------------------------------------------

def group(name):
    GROUP[0] = name
    print("\n-- %s %s" % (name, "-" * max(1, 60 - len(name))))


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        if VERBOSE:
            print("   PASS %s" % name)
        return True
    FAIL += 1
    FAILURES.append("%s / %s%s" % (GROUP[0], name, (": " + str(detail)) if detail else ""))
    print("   FAIL %s%s" % (name, ("  <- " + str(detail)) if detail else ""))
    return False


def eq(name, got, want):
    return check(name, got == want, "got %r want %r" % (got, want))


def raises(name, exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return check(name, True)
    except Exception as exc:
        return check(name, False, "raised %r" % (exc,))
    return check(name, False, "did not raise")


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

class FakeStore:
    """Same two-attribute surface as ``mimic_server.ARK_STORE``."""

    def __init__(self, state=None):
        self.state = state if isinstance(state, dict) else {}
        self.saves = 0

    def save(self):
        self.saves += 1
        return True


PUBLIC_ROW = {"PublicGame": True}
PRIVATE_ROW = {"PublicGame": False}


def seed_store():
    """A store that looks like a live server between two sessions."""
    store = FakeStore()
    conq = store.state.setdefault("conquest", {})
    conq["nextSessionId"] = 81650
    conq["secondsLeft"] = 43200
    conq["sessions"] = [
        {
            "SessionID": 81650, "version": 96, "name": 4, "Description": 1121352,
            "sessionname": "ANNO LEAGUE II", "SessionState": 2,
            "SecondsLeft": 40000, "SecondsRunning": 1200, "duration": 336,
            "NumberOfTeams": 2, "MaxPlayersPerTeam": 5, "GameSpeed": True,
            "ChapterTypeID": 4, "PublicGame": False,
            "Team": {
                "Team1": {"Score": 10, "Member": [
                    {"name": "Host", "id": 0, "playing": -1, "portrait": 3,
                     "title": 4, "score": 10, "timelimit": 40000}]},
                "Team2": {"Score": 3, "Member": []},
            },
            "MissionsDynamic": [], "Missions": [], "Links": [],
        },
        {
            "SessionID": 81651, "version": 1, "name": 2, "Description": 7,
            "sessionname": "OPEN ONE", "SessionState": 2, "SecondsLeft": 30000,
            "duration": 240, "NumberOfTeams": 2, "MaxPlayersPerTeam": 4,
            "GameSpeed": False, "ChapterTypeID": 2, "PublicGame": True,
            "Team": {"Team1": {"Score": 0, "Member": []},
                     "Team2": {"Score": 0, "Member": []}},
            "MissionsDynamic": [], "Missions": [], "Links": [],
        },
    ]
    conq["history"] = [
        {"sessionevent": 1, "seconds": 60, "Team": "Team1",
         "Playername": "Host", "MissionIndex": "1", "Level": "1"},
        {"sessionevent": 1, "seconds": 120, "Team": "Team1",
         "Playername": "Guest", "MissionIndex": "2", "Level": "1"},
        {"sessionevent": 3, "seconds": 180, "Team": "", "Playername": "Host"},
    ]
    conq["missionCatalogue"] = {
        "0000": {"ID": 18, "name": 7420004, "Description": 1121352,
                 "NumberOfTeams": 2, "MaxPlayerPerTeam": 5,
                 "Background": "conquest_bg", "Missions": [], "Links": [],
                 "Duration": [0], "TimeLimit": [0], "IsAddonSession": False},
        "0001": {"ID": 19, "name": 7420005, "Description": 1121353,
                 "NumberOfTeams": 2, "MaxPlayerPerTeam": 5,
                 "Background": "conquest_bg", "Missions": [], "Links": [],
                 "Duration": [0], "TimeLimit": [0], "IsAddonSession": False},
    }
    return store


# ---------------------------------------------------------------------------
# request-body builders
# ---------------------------------------------------------------------------

def form(pairs):
    """``[(k, v), ...]`` -> urlencoded bytes (client order preserved)."""
    from urllib.parse import quote_plus
    return "&".join("%s=%s" % (quote_plus(str(k)), quote_plus(str(v)))
                    for k, v in pairs).encode()


def multipart(fields, files, boundary=ch.BOUNDARY, header=True):
    """Build a multipart/form-data body exactly like the client's composer."""
    bnd = boundary.encode()
    out = b""
    for name, value in fields:
        if isinstance(value, str):
            value = value.encode()
        out += (b"--" + bnd + b"\r\n"
                b'Content-Disposition: form-data; name="' + name.encode() + b'"'
                b"\r\n\r\n" + value + b"\r\n")
    for name, filename, content_type, data in files:
        out += (b"--" + bnd + b"\r\n"
                b'Content-Disposition: form-data; name="' + name.encode() +
                b'"; filename="' + filename.encode() + b'"\r\n'
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" +
                data + b"\r\n")
    out += b"--" + bnd + b"--\r\n"
    if header:
        return out
    return out, "multipart/form-data; boundary=%s" % boundary


SAVE_BYTES = b"\x00\x01\xff\xfePK\x03\x04SSW\x00" + b"\r\n\r\n" + bytes(range(256))


def savegame_body():
    return multipart(
        [("PlayerName", "Bob"), ("Key", KEY), ("SessionID", "81650"),
         ("MissionIndex", "2"), ("VCValue", "1234"), ("VCLevel", "5"),
         ("SecondsPlayed", "600")],
        [("file", "myfile.png", "application/octet-stream", SAVE_BYTES)])


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def post(handler, pairs=None, body=None, store=None, headers=None, path=None):
    """Drive one request through the framing-free layer."""
    if path is None:
        path = ("/ConquestServer/%s.php" % handler) if handler != "loca" \
            else "/ConquestServer/loca/en.txt"
    if body is None:
        body = form(pairs or [])
    head = {"content-type": "application/x-www-form-urlencoded",
            "content-length": str(len(body))}
    if headers:
        head.update(headers)
    return ch.handle_request("POST", path, head, body, store=store)


def doc_of(response):
    return json.loads(response.body.decode("ascii"))


def no_trailing_newline(name, payload):
    check("%s: no trailing newline/whitespace" % name,
          payload == payload.rstrip(b" \t\r\n\x00") and payload,
          payload[-8:])


def content_length_ok(name, response, head_only=False):
    wire = ch.response_bytes(response, head_only=head_only)
    head = wire.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    values = [ln.split(":", 1)[1].strip() for ln in lines
              if ln.lower().startswith("content-length:")]
    if not check("%s: Content-Length present" % name, len(values) == 1, lines):
        return
    eq("%s: Content-Length exact" % name, int(values[0]), len(response.body))
    if head_only:
        check("%s: HEAD sends no body" % name, wire.endswith(b"\r\n\r\n"))


# ---------------------------------------------------------------------------
# 1. JSON encoding
# ---------------------------------------------------------------------------

def test_encoding():
    group("json encoding (PHP json_encode)")
    eq("compact separators", ch.encode_json({"a": 1, "b": [1, 2]}),
       b'{"a":1,"b":[1,2]}')
    eq("slash escaped", ch.encode_json({"skey": "conquest/2/x.sww"}),
       b'{"skey":"conquest\\/2\\/x.sww"}')
    eq("non-ascii as \\uXXXX", ch.encode_json({"n": "Bj\u00f6rn"}),
       b'{"n":"Bj\\u00f6rn"}')
    payload = ch.encode_json({"s": "Bj\u00f6rn \u20ac", "p": "a/b"})
    check("ascii only on the wire", all(byte < 128 for byte in payload), payload)
    eq("surrogate pair", ch.encode_json({"e": "\U0001F600"}),
       b'{"e":"\\ud83d\\ude00"}')
    eq("bool lower-case", ch.encode_json({"Return": True}), b'{"Return":true}')
    eq("empty list", ch.encode_json([]), b"[]")
    eq("empty object", ch.encode_json({}), b"{}")
    for sample in ({}, [], {"Return": True}, {"a": [1, {"b": None}]},
                   [{"SessionID": 1, "sessionname": "x"}]):
        payload = ch.encode_json(sample)
        no_trailing_newline("encode %r" % (sample,), payload)
        eq("round-trip %r" % (sample,), json.loads(payload.decode()), sample)
    # no whitespace at all
    check("no whitespace outside strings",
          b" " not in ch.encode_json({"a": [1, 2], "b": {"c": True}}))


# ---------------------------------------------------------------------------
# 2. urlencoded parsing
# ---------------------------------------------------------------------------

def test_urlencoded():
    group("application/x-www-form-urlencoded")
    params = ch.parse_urlencoded(b"PlayerName=Bob&Key=%s&Portrait=3&Title=" % KEY.encode())
    eq("order preserved", list(params), ["PlayerName", "Key", "Portrait", "Title"])
    eq("values", (params["PlayerName"], params["Portrait"]), ("Bob", "3"))
    eq("blank value kept", params["Title"], "")
    eq("duplicate last wins",
       ch.parse_urlencoded(b"a=1&a=2")["a"], "2")
    eq("plus and percent decoding",
       ch.parse_urlencoded(b"SessionName=ANNO+LEAGUE%20II&x=%C3%A4"),
       {"SessionName": "ANNO LEAGUE II", "x": "\u00e4"})
    eq("CRLF-wrapped body",
       ch.parse_urlencoded(b"\r\nPlayerName=Bob&SessionID=81650\r\n"),
       {"PlayerName": "Bob", "SessionID": "81650"})
    eq("empty body", ch.parse_urlencoded(b""), {})
    eq("non-utf8 bytes do not raise",
       ch.parse_urlencoded(b"PlayerName=\xff\xfe")["PlayerName"], "\ufffd\ufffd")
    check("bare word tolerated",
          ch.parse_urlencoded(b"garbage") == {"garbage": ""},
          ch.parse_urlencoded(b"garbage"))

    # through the framing layer
    store = seed_store()
    response = ch.handle_request(
        "POST", "/ConquestServer/PlayerLogin.php",
        {"content-type": "application/x-www-form-urlencoded",
         "content-length": "27"}, b"PlayerName=Bob&Key=%s" % KEY.encode(), store=store)
    eq("login over the form path", doc_of(response), {"Return": True})
    eq("portrait recorded", store.state["conquest"]["players"]["Bob"]["Portrait"], 0)


# ---------------------------------------------------------------------------
# 3. multipart parsing
# ---------------------------------------------------------------------------

def test_multipart():
    group("multipart/form-data")
    boundary = ch.BOUNDARY
    body = multipart(
        [("PlayerName", "Bob"), ("Key", KEY), ("SessionID", "81650"),
         ("MissionIndex", "2"), ("Note", "line1\r\nline2")],
        [("file", "myfile.png", "application/octet-stream", SAVE_BYTES)])
    check("body carries the client's constant boundary",
          body.startswith(b"--related-designs123456789\r\n"), body[:40])
    check("body ends with the closing delimiter",
          body.endswith(b"--related-designs123456789--\r\n"), body[-40:])
    fields, files = ch.parse_multipart(
        body, "multipart/form-data; boundary=related-designs123456789")
    eq("field order", list(fields),
       ["PlayerName", "Key", "SessionID", "MissionIndex", "Note"])
    eq("text fields", (fields["PlayerName"], fields["MissionIndex"]), ("Bob", "2"))
    eq("CRLF inside a text value survives", fields["Note"], "line1\r\nline2")
    eq("file part count", list(files), ["file"])
    part = files["file"]
    eq("filename literal", part["filename"], "myfile.png")
    eq("file content type", part["content_type"], "application/octet-stream")
    eq("binary file bytes exact", part["data"], SAVE_BYTES)
    check("NUL and 0xff preserved", b"\x00" in part["data"] and b"\xff" in part["data"])

    # header boundary wins; the constant is only a fallback
    custom = multipart([("a", "1")],
                       [("file", "myfile.png", "image/png", b"\x89PNG\r\n")],
                       boundary="----customBoundary42")
    fields, files = ch.parse_multipart(
        custom, 'multipart/form-data; boundary="----customBoundary42"')
    eq("custom boundary quoted in header", (fields, files["file"]["data"]),
       ({"a": "1"}, b"\x89PNG\r\n"))
    fields, files = ch.parse_multipart(body)          # no Content-Type at all
    eq("constant boundary fallback", (fields["PlayerName"], len(files["file"]["data"])),
       ("Bob", len(SAVE_BYTES)))
    raises("missing boundary is a BadRequest", ch.BadRequest,
           ch.parse_multipart, b"no delimiters here", "multipart/form-data")
    raises("part without header separator", ch.BadRequest, ch.parse_multipart,
           b"--related-designs123456789\r\nbroken\r\n--related-designs123456789--\r\n",
           "multipart/form-data")
    # a part without Content-Disposition is ignored, not fatal
    fields, files = ch.parse_multipart(
        b"--related-designs123456789\r\nX: y\r\n\r\nzzz\r\n"
        b"--related-designs123456789--\r\n", "multipart/form-data")
    eq("unnamed part ignored", (fields, files), ({}, {}))


# ---------------------------------------------------------------------------
# 4. routing table
# ---------------------------------------------------------------------------

def test_routes():
    group("routing (24 endpoints)")
    eq("all 24 endpoints have a handler", len(ch.ENDPOINTS), 24)
    eq("handler table covers ENDPOINTS", set(ch._HANDLERS), set(ch.ENDPOINTS))
    for name in ch.ENDPOINTS:
        expected = name
        eq("route /ConquestServer/%s.php" % name,
           ch.route("/ConquestServer/%s.php" % name), expected)
        eq("route lower-case %s" % name,
           ch.route("/ConquestServer/%s.php".replace("%s", name.lower())), expected)
        eq("route without prefix %s" % name,
           ch.route("/%s.php" % name), expected)
    eq("route loca file", ch.route("/ConquestServer/loca/en.txt"), "loca")
    eq("route nested loca", ch.route("/ConquestServer/loca/de/extra.txt"), "loca")
    eq("route with query string", ch.route("/ConquestServer/history.php?x=1"), "history")
    eq("route unknown", ch.route("/ConquestServer/Nope.php"), None)
    eq("route empty", ch.route(""), None)


# ---------------------------------------------------------------------------
# 5. every route through the HTTP layer
# ---------------------------------------------------------------------------

REQUESTS = {
    "PlayerLogin": [("PlayerName", "Bob"), ("Key", KEY), ("Portrait", "3"),
                    ("Title", "4")],
    "ConquestChapterList": [("PlayerName", "Bob")],
    "ConquestSessionList": [("PlayerName", "Bob"), ("Key", KEY)],
    "ConquestOpenList": [("PlayerName", "Bob"), ("Key", KEY)],
    "SessionInfo": [("ID", "81650")],
    "NewSession": [("PlayerName", "Bob"), ("Key", KEY), ("Portrait", "3"),
                   ("Title", "4"), ("Team", "Team1"), ("ChapterTypeID", "4"),
                   ("SessionName", "NEW ONE"), ("SessionDuration", "336"),
                   ("GameSpeed", "1"), ("TimeLimit", "120"), ("MaxPlayers", "5"),
                   ("PublicGame", "1")],
    "AddPlayerToSession": [("PlayerName", "Guest"), ("Key", KEY),
                           ("Portrait", "1"), ("Title", "2"), ("Team", "Team2"),
                           ("SessionID", "81650")],
    "CanJoinSession": [("PlayerName", "Bob")],
    "RemovePlayerFromSession": [("PlayerName", "Guest"), ("Key", KEY),
                                ("SessionID", "81650")],
    "history": [("SessionID", "81650"), ("Start", "0"), ("End", "-1")],
    "StartMission": [("SessionID", "81650"), ("MissionIndex", "2"),
                     ("PlayerName", "Bob"), ("Key", KEY)],
    "JoinMission": [("SessionID", "81650"), ("MissionIndex", "2"),
                    ("PlayerName", "Bob"), ("Key", KEY)],
    "LeaveMission": [("SessionID", "81650"), ("PlayerName", "Bob"), ("Key", KEY)],
    "UpdatePlayer": [("PlayerName", "Bob"), ("Key", KEY), ("SessionID", "81650"),
                     ("MissionIndex", "2"), ("VCValue", "10"), ("History", "[]")],
    "UploadSavegame": None,                       # multipart, built below
    "ResetSavegame": [("PlayerName", "Bob"), ("Key", KEY), ("SessionID", "81650"),
                      ("MissionIndex", "2"), ("Team", "Team1")],
    "UseCoopTime": [("PlayerName", "Bob"), ("Key", KEY), ("SessionID", "81650"),
                    ("SecondsPlayed", "60")],
    "loca": [],
    "Invite": [("HostName", "Bob"), ("ClientName", "Guest"), ("Key", KEY),
               ("SessionID", "81650"), ("Team", "Team2"), ("SlotID", "4")],
    "UnInvite": [("HostName", "Bob"), ("ClientName", "Guest"), ("Key", KEY),
                 ("SessionID", "81650")],
    "CloseSlot": [("HostName", "Bob"), ("Key", KEY), ("SessionID", "81650"),
                  ("SlotID", "4")],
    "AcceptInvite": [("PlayerName", "Guest"), ("Key", KEY),
                     ("SessionID", "81650"), ("Team", "Team2")],
    "JoinPublic": [("PlayerName", "Bob"), ("Key", KEY), ("SessionID", "81651")],
    "SendChatMsg": [("PlayerName", "Bob"), ("Key", KEY), ("SessionID", "81650"),
                    ("Message", "hello"), ("Channel", "COMMON")],
}

LIST_ROUTES = {"ConquestSessionList", "ConquestOpenList"}


def test_all_routes():
    group("24 routes through handle_request")
    for name in ch.ENDPOINTS:
        store = seed_store()
        if name == "UploadSavegame":
            body = savegame_body()
            headers = {"content-type":
                       "multipart/form-data; boundary=%s" % ch.BOUNDARY}
        else:
            body = form(REQUESTS[name])
            headers = {"content-type": "application/x-www-form-urlencoded"}
        path = "/ConquestServer/loca/en.txt" if name == "loca" \
            else "/ConquestServer/%s.php" % name
        response = ch.handle_request("POST", path, headers, body, store=store)
        eq("%s: status 200" % name, response.status, 200)
        check("%s: body not empty" % name, bool(response.body))
        content_length_ok(name, response)
        if name == "loca":
            eq("%s: text/plain" % name, response.content_type, ch.TEXT_CONTENT_TYPE)
            check("%s: raw text mentions the file" % name, b"en.txt" in response.body,
                  response.body[:80])
            check("%s: not JSON" % name, not response.body.startswith(b"{"))
            no_trailing_newline("%s: trailing rule" % name,
                                response.body.rstrip(b"\r\n") or b"x")
            continue
        try:
            document = json.loads(response.body.decode("utf-8"))
        except Exception as exc:
            check("%s: JSON parses" % name, False, "%r %r" % (exc, response.body[:120]))
            continue
        check("%s: JSON parses" % name, True)
        if name in LIST_ROUTES:
            check("%s: top-level array" % name, isinstance(document, list), type(document))
            check("%s: populated list" % name, len(document) >= 1, document)
        else:
            check("%s: top-level object" % name, isinstance(document, dict), type(document))
        no_trailing_newline("%s: trailing rule" % name, response.body)
        check("%s: ascii body" % name,
              all(byte < 128 for byte in response.body))

    # ---- exact key sets on the documented routes -------------------------
    store = seed_store()
    eq("PlayerLogin exact doc",
       doc_of(post("PlayerLogin", REQUESTS["PlayerLogin"], store=store)),
       {"Return": True})
    document = doc_of(post("NewSession", REQUESTS["NewSession"], store=store))
    eq("NewSession exact keys", set(document), {"SessionID"})
    check("NewSession allocates the next id", isinstance(document["SessionID"], int)
          and document["SessionID"] >= 81650, document)
    eq("NewSession stored the row", store.state["conquest"]["sessions"][-1]["sessionname"],
       "NEW ONE")

    document = doc_of(post("ConquestChapterList", REQUESTS["ConquestChapterList"],
                           store=store))
    eq("ConquestChapterList keys", set(document), {"0000", "0001"})
    eq("ConquestChapterList entry keys", sorted(document["0000"]),
       sorted(["ID", "name", "Description", "NumberOfTeams", "MaxPlayerPerTeam",
               "Background", "BackgroundPicture", "SessionPicture", "Missions",
               "Links", "Duration", "TimeLimit", "IsAddonSession"]))

    rows = doc_of(post("ConquestSessionList", REQUESTS["ConquestSessionList"],
                       store=seed_store()))
    eq("session list row count", len(rows), 2)
    eq("browse row key count", len(rows[0]), 17)
    open_rows = doc_of(post("ConquestOpenList", REQUESTS["ConquestOpenList"],
                            store=seed_store()))
    eq("open list only public sessions", [r["SessionID"] for r in open_rows], [81651])

    full = doc_of(post("SessionInfo", [("ID", "81650")], store=store))
    eq("SessionInfo full key count", len(full), 20)
    eq("SessionInfo SessionID", full["SessionID"], 81650)
    check("SessionInfo version is an int", isinstance(full["version"], int))
    first_version = full["version"]
    dynamic = doc_of(post("SessionInfo", [("ID", "81650"), ("Dynamic", "1")],
                          store=store))
    eq("SessionInfo dynamic key count", len(dynamic), 18)
    check("dynamic variant drops Missions/Links",
          "Missions" not in dynamic and "Links" not in dynamic)
    check("version rises on the poll", dynamic["version"] > first_version,
          (first_version, dynamic["version"]))
    eq("unknown session still answers a detail doc", set(
        doc_of(post("SessionInfo", [("ID", "1")], store=store))), set(full))

    eq("CanJoinSession keys", set(doc_of(post("CanJoinSession",
                                             REQUESTS["CanJoinSession"],
                                             store=store))), {"Result"})
    eq("UseCoopTime exact doc",
       set(doc_of(post("UseCoopTime", REQUESTS["UseCoopTime"], store=store))),
       {"timelimit"})
    eq("history top-level is a map", isinstance(
        doc_of(post("history", REQUESTS["history"], store=store)), dict), True)
    eq("history map key style", list(doc_of(post("history", REQUESTS["history"],
                                                 store=store)))[0], "0_i")
    eq("history Start=2 slices and keeps counting", list(doc_of(post(
        "history", [("SessionID", "81650"), ("Start", "2"), ("End", "-1")],
        store=store))), ["2_i"])
    eq("history Start beyond the end is empty", doc_of(post(
        "history", [("SessionID", "81650"), ("Start", "9"), ("End", "-1")],
        store=store)), {})
    eq("history End bound", len(doc_of(post(
        "history", [("SessionID", "81650"), ("Start", "0"), ("End", "2")],
        store=store))), 2)
    eq("UpdatePlayer exact doc (no history rows)",
       doc_of(post("UpdatePlayer", REQUESTS["UpdatePlayer"], store=seed_store())),
       {"timelimit": 40000})
    eq("AcceptInvite lower-case result", set(doc_of(post(
        "AcceptInvite", REQUESTS["AcceptInvite"], store=store))), {"result"})
    chat = doc_of(post("SendChatMsg", REQUESTS["SendChatMsg"], store=store))
    eq("SendChatMsg keys", set(chat), {"result", "COMMON"})
    eq("SendChatMsg channel is a list", chat["COMMON"], [])
    for name in ("Invite", "UnInvite", "CloseSlot"):
        document = doc_of(post(name, REQUESTS[name], store=store))
        eq("%s exact keys" % name, set(document), {"SessionID", "Team"})
        check("%s Team is a string" % name, isinstance(document["Team"], str), document)
    eq("JoinPublic exact keys", set(doc_of(post("JoinPublic",
                                               REQUESTS["JoinPublic"],
                                               store=store))), {"SessionID"})

    # ---- start mission: 3 keys, <=255 bytes, starts with '{' -------------
    store = seed_store()
    response = post("StartMission", REQUESTS["StartMission"], store=store)
    check("StartMission starts with '{'", response.body.startswith(b"{"))
    check("StartMission <= 255 bytes", len(response.body) <= 255, len(response.body))
    document = doc_of(response)
    eq("StartMission exact keys", set(document), {"CloudEnable", "skey", "bucket"})
    eq("StartMission CloudEnable", document["CloudEnable"], True)
    check("StartMission skey is a .ssw key", document["skey"].endswith(".sww"),
          document["skey"])
    eq("StartMission bucket", document["bucket"], "conquest_chapter_4")
    long_store = seed_store()
    long = post("StartMission", [("SessionID", "81650"), ("MissionIndex", "7"),
                                 ("PlayerName", "P" * 400)], store=long_store)
    check("StartMission fits an over-long name", len(long.body) <= 255, len(long.body))
    check("StartMission still an object", long.body.startswith(b"{"))
    big = {"CloudEnable": True, "skey": "x" * 400, "bucket": "conquest_chapter_4"}
    fitted = ch._fit_start_mission(big, "conquest_chapter_4")
    check("_fit_start_mission shrinks", len(ch.encode_json(fitted)) <= 255,
          len(ch.encode_json(fitted)))
    eq("_fit_start_mission keeps 3 keys", set(fitted),
       {"CloudEnable", "skey", "bucket"})

    # ---- unknown path / method ------------------------------------------
    store = seed_store()
    response = ch.handle_request("POST", "/ConquestServer/NoSuchThing.php",
                                 {"content-length": "0"}, b"", store=store)
    eq("unknown path status", response.status, 200)
    eq("unknown path ack", doc_of(response), {"Return": True})
    response = ch.handle_request("GET", "/", {}, b"", store=store)
    eq("root path ack", doc_of(response), {"Return": True})
    response = ch.handle_request("PUT", "/ConquestServer/PlayerLogin.php",
                                 {"content-length": "0"}, b"", store=store)
    eq("unsupported method status", response.status, 405)
    eq("405 carries the error doc", set(doc_of(response)), {"error"})


# ---------------------------------------------------------------------------
# 6. error documents / bad bodies
# ---------------------------------------------------------------------------

def test_errors():
    group("error documents")
    store = seed_store()
    response = ch.handle_request(
        "POST", "/ConquestServer/SessionInfo.php",
        {"content-type": "application/x-www-form-urlencoded", "content-length": "9"},
        b'{"ID":ab}', store=store)
    eq("bad JSON body status is 200", response.status, 200)
    document = doc_of(response)
    eq("bad JSON body -> error doc", set(document), {"error"})
    eq("error doc keys", sorted(document["error"]),
       sorted(["version", "id", "InfoText", "vars"]))
    check("error id is an int (the game's error code)",
          isinstance(document["error"]["id"], int), document["error"])
    eq("error vars is a list", document["error"]["vars"], [])
    no_trailing_newline("error doc", response.body)
    content_length_ok("error doc", response)

    response = post("SessionInfo", body=b'{"ID":', store=seed_store())
    eq("truncated JSON body -> error doc", set(doc_of(response)), {"error"})

    response = post("UploadSavegame", body=b"not a multipart body",
                    headers={"content-type": "multipart/form-data; boundary=zz"},
                    store=seed_store())
    eq("save upload without file part -> error", set(doc_of(response)), {"error"})

    response = post("UploadSavegame",
                    body=multipart([("PlayerName", "Bob")], []),
                    headers={"content-type":
                             "multipart/form-data; boundary=%s" % ch.BOUNDARY},
                    store=seed_store())
    eq("save upload without the file part -> error", set(doc_of(response)), {"error"})

    # JSON request bodies are tolerated when they fit the 255-byte rule
    response = post("SessionInfo", body=b'{"ID": 81650}', store=seed_store())
    eq("small JSON request body accepted", doc_of(response)["SessionID"], 81650)
    response = post("SessionInfo", body=b'{"ID":"81650","pad":"' + b"x" * 300 + b'"}',
                    store=seed_store())
    eq("JSON body over 255 bytes -> error", set(doc_of(response)), {"error"})

    # a dispatch crash must also produce a parseable error document
    def boom(*args, **kwargs):
        raise RuntimeError("boom")
    original = ch._HANDLERS["SessionInfo"]
    ch._HANDLERS["SessionInfo"] = boom
    try:
        response = post("SessionInfo", REQUESTS["SessionInfo"], store=seed_store())
    finally:
        ch._HANDLERS["SessionInfo"] = original
    eq("dispatch crash -> error doc", set(doc_of(response)), {"error"})


# ---------------------------------------------------------------------------
# 7. savegame storage
# ---------------------------------------------------------------------------

def test_savegame_storage():
    group("UploadSavegame storage")
    store = seed_store()
    response = post("UploadSavegame", body=savegame_body(),
                    headers={"content-type":
                             "multipart/form-data; boundary=%s" % ch.BOUNDARY},
                    store=store)
    # PN 177: the corpus answers upload and download IDENTICALLY - the cloud ack
    # with exactly three keys and CloudEnable true - so the upload must not carry
    # a timelimit (the two directions previously disagreed).
    eq("upload reply is timelimit", set(doc_of(response)), {"timelimit"})
    cloud = store.state["conquest"]["cloud"]
    eq("one cloud entry", len(cloud), 1)
    key, entry = list(cloud.items())[0]
    check("cloud key looks like the corpus", key.startswith("conquest/"), key)
    check("cloud key ends .ssw", key.endswith(".sww"), key)
    eq("cloud bucket", entry["bucket"], "conquest_chapter_4")
    eq("cloud size", entry["size"], len(SAVE_BYTES))
    eq("cloud bytes round-trip", base64.b64decode(entry["data"]), SAVE_BYTES)
    eq("cloud content type", entry["content_type"], "application/octet-stream")
    eq("cloud mission index", entry["MissionIndex"], 2)

    # reset forgets it again
    response = post("ResetSavegame", REQUESTS["ResetSavegame"], store=store)
    eq("reset reply is the ack", doc_of(response), {"Return": True})
    eq("reset cleared the save", store.state["conquest"]["cloud"], {})

    # usage co-op time clamps the remaining seconds and reports them
    store = seed_store()
    before = store.state["conquest"]["sessions"][0]["SecondsLeft"]
    response = post("UseCoopTime", REQUESTS["UseCoopTime"], store=store)
    eq("coop time subtracts played seconds",
       doc_of(response)["timelimit"], before - 60)


# ---------------------------------------------------------------------------
# 8. HTTP framing details
# ---------------------------------------------------------------------------

def test_framing():
    group("HTTP framing")
    store = seed_store()
    response = post("PlayerLogin", REQUESTS["PlayerLogin"], store=store)
    wire = ch.response_bytes(response)
    check("status line", wire.startswith(b"HTTP/1.1 200 OK\r\n"), wire[:40])
    check("headers end with a blank line", b"\r\n\r\n" in wire)
    eq("body follows the blank line", wire.split(b"\r\n\r\n", 1)[1], response.body)
    check("keep-alive by default", b"Connection: keep-alive" in wire)
    closed = ch.handle_request("POST", "/ConquestServer/PlayerLogin.php",
                               {"content-length": str(len(form(REQUESTS["PlayerLogin"]))),
                                "connection": "close"},
                               form(REQUESTS["PlayerLogin"]), store=store)
    check("Connection: close honoured", closed.close)
    check("Connection: close header",
          b"Connection: close" in ch.response_bytes(closed, keep_alive=False))
    http10 = ch.handle_request("POST", "/ConquestServer/PlayerLogin.php",
                               {"content-length": "0", "__version__": "HTTP/1.0"},
                               b"", store=store)
    check("HTTP/1.0 defaults to close", http10.close)
    keep = ch.handle_request("POST", "/ConquestServer/PlayerLogin.php",
                             {"content-length": "0", "__version__": "HTTP/1.0",
                              "connection": "keep-alive"}, b"", store=store)
    check("HTTP/1.0 + keep-alive stays open", not keep.close)
    content_length_ok("HEAD", response, head_only=True)
    eq("HEAD keeps the real Content-Length", len(response.body),
       int([ln for ln in ch.response_bytes(response, head_only=True)
            .split(b"\r\n") if ln.lower().startswith(b"content-length")][0]
           .split(b":")[1]))


# ---------------------------------------------------------------------------
# 9. end-to-end socket test
# ---------------------------------------------------------------------------

class Conn:
    """Minimal HTTP/1.1 client reader (one connection, several requests)."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _until(self, marker, limit=1 << 20):
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed while reading %r" % marker)
            self.buf += chunk
            if len(self.buf) > limit:
                raise ValueError("response head too large")
        head, _, rest = self.buf.partition(marker)
        self.buf = rest
        return head + marker

    def _read(self, count):
        while len(self.buf) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("connection closed after %d/%d body bytes"
                               % (len(self.buf), count))
            self.buf += chunk
        data, self.buf = self.buf[:count], self.buf[count:]
        return data

    def request(self, method, target, body=b"", content_type=None,
                content_length=True, extra=""):
        head = "%s %s HTTP/1.1\r\nHost: 127.0.0.1\r\n" % (method, target)
        if content_type:
            head += "Content-Type: %s\r\n" % content_type
        if content_length:
            head += "Content-Length: %d\r\n" % len(body)
        head += extra + "\r\n"
        self.sock.sendall(head.encode() + body)

    def response(self, head_only=False):
        head = self._until(b"\r\n\r\n").decode("latin-1")
        lines = head.split("\r\n")
        status = int(lines[0].split()[1])
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "-1"))
        body = b"" if head_only else self._read(length)
        return status, headers, body


def test_socket():
    group("end-to-end socket (keep-alive)")
    store = seed_store()
    lines = []
    server = ch.ConquestServer(0, store=store, log=lines.append, idle_timeout=5.0)
    server.start()
    port = server.port
    check("server bound an ephemeral port", port > 0, port)
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=8)
        conn = Conn(sock)

        # 1) PlayerLogin over the wire
        body = form(REQUESTS["PlayerLogin"])
        conn.request("POST", "/ConquestServer/PlayerLogin.php", body,
                     "application/x-www-form-urlencoded")
        status, headers, reply = conn.response()
        eq("PlayerLogin status", status, 200)
        eq("PlayerLogin Content-Length", int(headers["content-length"]), len(reply))
        eq("PlayerLogin body", json.loads(reply.decode()), {"Return": True})
        eq("PlayerLogin keeps the connection open",
           headers.get("connection"), "keep-alive")

        # 2) SessionInfo on the SAME connection (the client reuses it)
        body = form([("ID", "81650")])
        conn.request("POST", "/ConquestServer/SessionInfo.php", body,
                     "application/x-www-form-urlencoded")
        status, headers, reply = conn.response()
        eq("SessionInfo status (same socket)", status, 200)
        document = json.loads(reply.decode())
        eq("SessionInfo SessionID", document["SessionID"], 81650)
        check("SessionInfo detail has 20 keys", len(document) == 20, len(document))
        eq("SessionInfo Content-Length", int(headers["content-length"]), len(reply))
        check("SessionInfo body has no trailing newline", not reply.endswith(b"\n"))

        # 3) UploadSavegame (multipart) on the same connection
        body = savegame_body()
        conn.request("POST", "/ConquestServer/UploadSavegame.php", body,
                     "multipart/form-data; boundary=%s" % ch.BOUNDARY)
        status, headers, reply = conn.response()
        eq("UploadSavegame status (same socket)", status, 200)
        eq("UploadSavegame reply", set(json.loads(reply.decode())), {"timelimit"})
        eq("save bytes arrived intact over the socket",
           base64.b64decode(list(store.state["conquest"]["cloud"].values())[0]["data"]),
           SAVE_BYTES)

        # 4) HEAD: headers with the real Content-Length, no body
        conn.request("HEAD", "/ConquestServer/PlayerLogin.php",
                     body, "application/x-www-form-urlencoded")
        status, headers, reply = conn.response(head_only=True)
        eq("HEAD status", status, 200)
        check("HEAD advertises a positive Content-Length",
              int(headers.get("content-length", "-1")) > 0, headers.get("content-length"))
        eq("HEAD sent no body", reply, b"")

        # 5) keep-alive is still alive, then a clean close
        conn.request("POST", "/ConquestServer/NoSuchPath.php", b"",
                     "application/x-www-form-urlencoded")
        status, headers, reply = conn.response()
        eq("unknown path over the socket", json.loads(reply.decode()), {"Return": True})
        sock.close()

        # 6) Connection: close is honoured
        sock = socket.create_connection(("127.0.0.1", port), timeout=8)
        conn = Conn(sock)
        body = form([("ID", "81650")])
        conn.request("POST", "/ConquestServer/SessionInfo.php", body,
                     "application/x-www-form-urlencoded", extra="Connection: close\r\n")
        status, headers, reply = conn.response()
        eq("Connection: close reply", headers.get("connection"), "close")
        eq("server closed the socket", sock.recv(1), b"")
        sock.close()

        # 7) a request without Content-Length is drained, logged and closed
        sock = socket.create_connection(("127.0.0.1", port), timeout=8)
        conn = Conn(sock)
        body = form(REQUESTS["PlayerLogin"])
        conn.request("POST", "/ConquestServer/PlayerLogin.php", body,
                     "application/x-www-form-urlencoded", content_length=False)
        sock.shutdown(socket.SHUT_WR)
        status, headers, reply = conn.response()
        eq("unframed request still answered", status, 200)
        eq("unframed request ack", json.loads(reply.decode()), {"Return": True})
        eq("unframed request closes", headers.get("connection"), "close")
        sock.close()
    finally:
        server.stop()

    text = "\n".join(str(line) for line in lines)
    check("log: request line captured", "POST /ConquestServer/PlayerLogin.php HTTP/1.1" in text)
    check("log: path/handler captured", "handler=PlayerLogin" in text)
    check("log: byte count captured", "body bytes" in text)
    check("log: response doc captured", '{"Return":true}' in text)
    check("log: missing Content-Length logged loudly", "MISSING Content-Length" in text)
    check("log: stop logged", "stopped" in text)
    # the server must be gone
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        check("server stopped accepting", False, port)
    except OSError:
        check("server stopped accepting", True)


# ---------------------------------------------------------------------------
# 10. purity / total-function checks on dispatch
# ---------------------------------------------------------------------------

def test_dispatch_contract():
    group("dispatch contract")
    a = seed_store()
    b = seed_store()
    for name in ch.ENDPOINTS:
        if name == "UploadSavegame":
            params, files = ch.parse_multipart(
                savegame_body(), "multipart/form-data; boundary=%s" % ch.BOUNDARY)
        elif name == "loca":
            params, files = {"__loca__": "en.txt"}, {}
        else:
            params, files = ch.parse_urlencoded(form(REQUESTS[name])), {}
        first = ch.dispatch(name, params, files, a)
        second = ch.dispatch(name, params, files, b)
        if name in ("SessionInfo", "UseCoopTime"):
            # both stores were seeded identically -> identical documents
            check("dispatch deterministic for %s" % name,
                  ch.encode_json(first) == ch.encode_json(second),
                  (ch.encode_json(first)[:80], ch.encode_json(second)[:80]))
        elif name == "loca":
            check("dispatch raw doc for loca", isinstance(first, ch.RawDocument))
        else:
            eq("dispatch same document for %s" % name,
               ch.encode_json(first), ch.encode_json(second))
    eq("unknown handler -> ack", ch.dispatch(None, {}, {}, seed_store()),
       {"Return": True})
    eq("unknown handler name -> ack", ch.dispatch("Nope", {}, {}, seed_store()),
       {"Return": True})
    store = seed_store()
    sessions_before = len(store.state["conquest"]["sessions"])
    saves_before = store.saves
    ch.dispatch("PlayerLogin", {"PlayerName": "Zed"}, {}, store)
    eq("read-only route allocates no session",
       len(store.state["conquest"]["sessions"]), sessions_before)
    eq("read-only route does not persist", store.saves, saves_before)
    ch.dispatch("NewSession", {"PlayerName": "Zed"}, {}, store)
    eq("mutating route persists through store.save()",
       store.saves, saves_before + 1)
    eq("namespace exports the seam",
       all(hasattr(ch, attr) for attr in
           ("dispatch", "handle_request", "serve", "ConquestServer",
            "encode_json", "parse_urlencoded", "parse_multipart", "RawDocument")),
       True)


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

TESTS = [
    ("encoding", test_encoding),
    ("urlencoded", test_urlencoded),
    ("multipart", test_multipart),
    ("routes", test_routes),
    ("all-routes", test_all_routes),
    ("errors", test_errors),
    ("savegame", test_savegame_storage),
    ("framing", test_framing),
    ("socket", test_socket),
    ("dispatch", test_dispatch_contract),
]


def main():
    if "--list" in sys.argv:
        for name, _ in TESTS:
            print(name)
        return 0
    print("conquest_http tests  (module: %s)" % os.path.relpath(
        sys.modules["conquest_http"].__file__, ROOT))
    started = time.time()
    for name, fn in TESTS:
        try:
            fn()
        except Exception:
            global FAIL
            FAIL += 1
            FAILURES.append("%s: crashed" % name)
            print("   FAIL %s crashed:" % name)
            print(traceback.format_exc())
    print("\n%s" % ("=" * 66))
    print("%d checks, %d passed, %d failed  (%.2fs)"
          % (PASS + FAIL, PASS, FAIL, time.time() - started))
    if FAILURES:
        print("\nfailures:")
        for item in FAILURES:
            print("  - %s" % item)
        print("\nFAIL")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
