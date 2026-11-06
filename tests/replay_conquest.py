#!/usr/bin/env python3
"""Replay the corpus-implied conquest request forms against the dispatcher.

    source ~/tools/env.sh && python3 tools/replay_conquest.py            # replay cases
    source ~/tools/env.sh && python3 tools/replay_conquest.py --self-test # shapes rebuild

What it does
------------
1. Imports ``conquest_dispatch`` from ``server/mimic_server.py``.  The import is
   side-effect free (verified: ``main()`` is guarded by ``if __name__ ==
   "__main__"``, no socket/file/argparse work at module level), so no
   re-implementation is needed.  A tiny in-memory stand-in for ``ARK_STORE`` is
   installed so the stateful cloud-save branch can be exercised without
   touching ``server/data/mimic_state.json``.  Nothing is started, no socket is
   opened, no file is written.
2. Replays one request document per corpus fixture through the real
   ``conquest_dispatch`` and prints ``PASS``/``FAIL`` for each case.
3. Compares *shapes* the same way ``tools/test_conquest_shapes.py`` does:
   key sets recursively, value types recursively (bool is not int), array
   lengths ignored, a zero-length array where the fixture has elements is a
   failure (a builder that always returns ``[]`` must not pass).  Key *order*
   differences are reported as an advisory ``order`` note, not as a failure.
   Values themselves are not compared, except for the handful of fields the
   corpus only ever showed one value of (``Return``/``CloudEnable`` = true,
   ``timelimit`` never 0) - those are reported on a separate ``value:`` line
   so a shape PASS with a wrong value is still visible.

Request forms: the real log contains *responses only* (0 request bytes, 0
``GET``/``POST``/``HTTP/``/``://`` occurrences in 3 332 documents), so every
request document below is **inferred**.  Each case carries an ``origin`` note
saying which dispatcher branch / corpus family it comes from, and cases whose
request spelling cannot be derived from the corpus at all are marked
``INFERRED`` in the output.

Exit code: 0 = every case passed, 1 = at least one failure.
"""

import argparse
import contextlib
import importlib
import inspect
import io
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(ROOT, "corpus", "derived", "fixtures")
SERVER = os.path.join(ROOT, "server")

# ---------------------------------------------------------------------------
# shape comparison (same semantics as tools/test_conquest_shapes.py)
# ---------------------------------------------------------------------------

FAILS = []
CASES = 0
ORDER_NOTES = []
VALUE_NOTES = []


def _tname(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    return type(value).__name__


def shape_problems(actual, expected, path="$", problems=None):
    """Recursive key-set + value-type comparison.  Arrays: length-insensitive."""
    if problems is None:
        problems = []
    if isinstance(expected, bool):
        if not isinstance(actual, bool):
            problems.append("%s: expected bool, got %s" % (path, _tname(actual)))
    elif isinstance(expected, int):
        if isinstance(actual, bool) or not isinstance(actual, int):
            problems.append("%s: expected int, got %s" % (path, _tname(actual)))
    elif isinstance(expected, float):
        if not isinstance(actual, float):
            problems.append("%s: expected float, got %s" % (path, _tname(actual)))
    elif isinstance(expected, str):
        if not isinstance(actual, str):
            problems.append("%s: expected str, got %s" % (path, _tname(actual)))
    elif expected is None:
        if actual is not None:
            problems.append("%s: expected null, got %s" % (path, _tname(actual)))
    elif isinstance(expected, list):
        if not isinstance(actual, list):
            problems.append("%s: expected list, got %s" % (path, _tname(actual)))
        else:
            if expected and not actual:
                problems.append("%s: expected %d+ element(s), got []"
                                % (path, len(expected)))
            for index in range(min(len(expected), len(actual))):
                shape_problems(actual[index], expected[index],
                               "%s[%d]" % (path, index), problems)
    elif isinstance(expected, dict):
        if not isinstance(actual, dict):
            problems.append("%s: expected dict, got %s" % (path, _tname(actual)))
        else:
            expected_keys = list(expected.keys())
            actual_keys = list(actual.keys())
            missing = [k for k in expected_keys if k not in actual]
            extra = [k for k in actual_keys if k not in expected]
            if missing:
                problems.append("%s: missing keys %r" % (path, missing))
            if extra:
                problems.append("%s: unexpected keys %r" % (path, extra))
            for key in expected_keys:
                if key in actual:
                    shape_problems(actual[key], expected[key],
                                   "%s.%s" % (path, key), problems)
            if actual_keys != expected_keys:
                ORDER_NOTES.append("%s: key order %r != %r"
                                   % (path, actual_keys, expected_keys))
    else:  # pragma: no cover - fixtures only use the types above
        problems.append("%s: unhandled fixture type %s" % (path, _tname(expected)))
    return problems


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def load(name):
    path = os.path.join(FIXTURES, name)
    if not os.path.isfile(path):
        print(
            "SKIP: this replay uses fixtures derived from a real 2015 server "
            "log, which is not redistributed with this package "
            "(see docs/known-issues.md). The protocol tests that do not need "
            "them still run: tests/test_conquest_http.py, "
            "tests/test_conquest_scenarios.py.")
        raise SystemExit(0)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def fixture_value(name, path):
    """Value at a dotted path in a fixture (``[0]`` allowed as a token)."""
    doc = load(name)
    for token in path.split("."):
        if token.startswith("[") and token.endswith("]"):
            doc = doc[int(token[1:-1])]
        else:
            doc = doc[token]
    return doc


def resolve(doc, path):
    """Value at a dotted path of the *reply*; returns (found, value)."""
    cur = doc
    for token in path.split("."):
        if token.startswith("[") and token.endswith("]"):
            if not isinstance(cur, list):
                return False, None
            index = int(token[1:-1])
            if index >= len(cur):
                return False, None
            cur = cur[index]
        else:
            if not isinstance(cur, dict) or token not in cur:
                return False, None
            cur = cur[token]
    return True, cur


# ---------------------------------------------------------------------------
# dispatch loading (import must be side-effect free)
# ---------------------------------------------------------------------------

class FakeStore:
    """Stand-in for rmc_services' store: just the surface dispatch touches."""

    def __init__(self):
        self.state = {}
        self.saves = 0

    def save(self):
        self.saves += 1


def load_dispatch():
    if SERVER not in sys.path:
        sys.path.insert(0, SERVER)
    module = importlib.import_module("mimic_server")
    dispatch = getattr(module, "conquest_dispatch", None)
    if not callable(dispatch):
        raise SystemExit("mimic_server.conquest_dispatch is missing")
    try:
        _, lineno = inspect.getsourcelines(dispatch)
        where = "%s:%d" % (os.path.relpath(inspect.getfile(dispatch), ROOT), lineno)
    except (OSError, TypeError):  # pragma: no cover
        where = "<unknown>"
    store = FakeStore()
    had_store = getattr(module, "ARK_STORE", None) is not None
    if not had_store:
        module.ARK_STORE = store
    seed_state(store)
    return module, dispatch, store, where


def seed_state(store):
    """Give the dispatcher the state a live server would hold.

    ``conquest_dispatch`` only decides WHICH document to send - the spelling
    lives in ``conquest_shapes``.  With an empty store every collection is
    legitimately empty, which would test nothing: a routing bug (falling
    through to the ack) would look the same as correct-but-empty output.
    So the store is seeded from the corpus fixtures and the case list then
    requires the populated documents to come back out of the *dispatcher*.

    Layout (see ``server/mimic_server.py:conquest_dispatch``):
        conquest["sessions"]         list of session rows (browse/league/detail)
        conquest["history"]          bare league-history map
        conquest["missionCatalogue"] {"0000".."0016"}
    """
    try:
        detail = load("sessiondetail.json")
        browse = dict(load("sessionlist.json")[0])
        league = dict(load("sessionlist_league.json")[0])
        history = load("league_history.json")
        row = load("leaguerow.json")
        catalogue = load("missions.json")
    except Exception as exc:  # pragma: no cover - fixtures are committed
        print("seed: fixtures unavailable (%r) - running with an empty store" % exc)
        return
    conq = store.state.setdefault("conquest", {})
    conq["sessions"] = [browse, league, detail]
    conq["history"] = history                 # incremental page (8 entries)
    conq["leagueRow"] = row                    # single-row poll carries its own history
    conq["missionCatalogue"] = catalogue
    conq.setdefault("nextSessionId", 81651)


def call(dispatch, request):
    """Invoke dispatch, json round-trip the reply, return (reply, emitted_log)."""
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink):
        try:
            reply = dispatch(request)
        except Exception:
            return ("<raised>", traceback.format_exc().strip().splitlines()[-1],
                    sink.getvalue())
    try:
        reply = json.loads(json.dumps(reply))
    except (TypeError, ValueError) as exc:
        return ("<unserialisable>", str(exc), sink.getvalue())
    return reply, None, sink.getvalue()


# ---------------------------------------------------------------------------
# the cases
# ---------------------------------------------------------------------------
# Each case: id, fixture, request (or callable), origin, values.
# `values` entries: (path_in_reply, expected_value) or (path, ("nonzero",)) or
#                   (path, ("nonempty",)).

CLOUD_KEY = None  # filled from the fixture so the skey echo is checkable


def build_cases():
    global CLOUD_KEY
    ack = load("cloudsave_ack.json")
    CLOUD_KEY = ack["skey"]
    bucket = ack["bucket"]
    detail = load("sessiondetail.json")
    sid_detail = detail["SessionID"]

    return [
        dict(id="empty-list  (bare array query)",
             fixture="empty_list.json",
             request=[],
             origin="dispatcher list branch; corpus family '[]' (279x)"),

        dict(id="generic-ack (unmapped action dict)",
             fixture="return_true.json",
             request={"method": "someUnmappedAction", "params": []},
             origin="dispatcher fallback; corpus family {\"Return\":true} (242x)",
             values=[("Return", True)]),

        dict(id="cloud-ack   (upload, skey+data)",
             fixture="cloudsave_ack.json",
             request={"skey": CLOUD_KEY, "bucket": bucket, "data": "savedgameblob"},
             origin="dispatcher cloud branch (upload); corpus answered upload and "
                    "download identically (17x)",
             values=[("CloudEnable", True), ("skey", CLOUD_KEY)]),

        dict(id="cloud-ack   (download, cached skey)",
             fixture="cloudsave_ack.json",
             request={"skey": CLOUD_KEY, "bucket": bucket},
             origin="dispatcher cloud branch (download, previously stored)",
             values=[("CloudEnable", True), ("skey", CLOUD_KEY)]),

        dict(id="cloud-ack   (download, unknown skey)",
             fixture="cloudsave_ack.json",
             request={"skey": "conquest/never-uploaded.sww", "bucket": bucket},
             origin="dispatcher cloud branch (miss); corpus has no false variant",
             values=[("CloudEnable", True)]),

        dict(id="session-ack (create, no id, no version)",
             fixtures=["session_ack.json", "session_version_ack.json"],
             request={"sessionname": "ANNO LEAGUE II", "name": 7420004,
                      "Description": 1121352, "NumberOfTeams": 2,
                      "MaxPlayersPerTeam": 5, "GameSpeed": True, "duration": 336},
             origin="INFERRED: create payload modelled on sessiondetail.json "
                    "header; corpus has both ack variants and cannot say which "
                    "belongs to create (1x plain vs 47x versioned)"),

        dict(id="session-ack (create, with version)",
             fixtures=["session_ack.json", "session_version_ack.json"],
             request={"sessionname": "ANNO LEAGUE II", "version": 0,
                      "name": 7420004},
             origin="INFERRED: same create request carrying 'version'"),

        dict(id="session-ack (attach, int SessionID)",
             fixtures=["session_ack.json", "session_version_ack.json"],
             request={"SessionID": 81650},
             origin="dispatcher session branch; both ack variants are legal "
                    "replies here in the corpus -> form ambiguous"),

        dict(id="session-ack (attach, str SessionID)",
             fixtures=["session_ack.json", "session_version_ack.json"],
             request={"SessionID": "81650"},
             origin="same request with a JSON string id (type drift probe)"),

        dict(id="session-poll (attach + version)",
             fixture="session_version_ack.json",
             request={"SessionID": 81650, "version": 99},
             origin="dispatcher session branch; corpus version ack (47x, version "
                    "rises monotonically) - the polling form"),

        dict(id="league-row  (leaderboard poll)",
             fixture="leaguerow.json",
             request={"Orders": [3]},
             origin="INFERRED: single-row poll; corpus family 'league row poll' "
                    "is a bare OBJECT (2240x). Trigger key is a guess",
             values=[("SessionState", 2), ("history", ("nonempty",))]),

        dict(id="league-history (incremental page)",
             fixture="league_history.json",
             request={"History": 1, "League": 2},
             origin="INFERRED: bare history map (21x); no dispatcher branch, "
                    "request spelling unknown"),

        dict(id="session-list (browse)",
             fixture="sessionlist.json",
             request={"SessionList": 1},
             origin="dispatcher list branch under a guessed trigger key; "
                    "corpus family browse list (48x)"),

        dict(id="session-list (league browser)",
             fixture="sessionlist_league.json",
             request={"League": 2},
             origin="dispatcher list branch under a guessed trigger key; "
                    "corpus family league list (37x)"),

        dict(id="session-detail (join / full)",
             fixture="sessiondetail.json",
             request={"SessionID": sid_detail, "detail": 1},
             origin="INFERRED: join request carries SessionID; corpus family "
                    "full detail (58x)"),

        dict(id="session-detail (in-game poll, dynamic)",
             fixture="sessiondetail_dynamic.json",
             request={"SessionID": sid_detail, "version": 1, "detail": 1,
                      "dynamic": 1},
             origin="INFERRED: in-game poll carries SessionID+version; corpus "
                    "family dynamic detail (19x)"),

        dict(id="mission-catalogue (static map list)",
             fixture="missions.json",
             request={"Missions": 1},
             origin="INFERRED: no dispatcher branch, request spelling unknown; "
                    "corpus family catalogue (37x)"),

        dict(id="timelimit   (remaining seconds)",
             fixture="timelimit.json",
             request={"timelimit": 1},
             origin="dispatcher timelimit branch; corpus family {\"timelimit\":N} "
                    "(285x, 32816..147134)",
             values=[("timelimit", ("nonzero",))]),

        dict(id="error-doc   (bad player parameter)",
             fixture="error.json",
             request={"League": 2, "Player": ""},
             origin="INFERRED: corpus error text 'Player parameter are incorrect' "
                    "(1x); no dispatcher branch"),
    ]


def unwrap_hint(reply, expected):
    """Diagnostic: would the reply match if a 1-element array were unwrapped?"""
    if isinstance(expected, dict) and isinstance(reply, list) and len(reply) == 1:
        if not shape_problems(reply[0], expected):
            return ("note: the reply's only element matches the corpus object - "
                    "the corpus sends a bare object here, not a 1-element array")
    if isinstance(expected, list) and expected and isinstance(reply, dict):
        if not shape_problems(reply, expected[0]):
            return ("note: the reply is a single object; the corpus sends an "
                    "array of them")
    return None


def print_case(case, reply, err):
    global CASES
    CASES += 1
    names = case.get("fixtures") or [case["fixture"]]
    matched = None
    problems = []
    if err:
        problems.append("$: dispatch raised/unserialisable -> %s" % err)
    else:
        attempts = [(name, shape_problems(reply, load(name))) for name in names]
        for name, attempt in attempts:
            if not attempt and matched is None:
                matched = name
        if matched is None:
            # report the closest candidate's diff; all candidates disagree
            problems = min(attempts, key=lambda item: len(item[1]))[1]

    for path, want in case.get("values", []):
        doc = None if err else reply
        found, value = resolve(doc, path)
        if not found and isinstance(doc, list) and len(doc) == 1:
            found, value = resolve(doc[0], path)  # 1-element wrapper
        if not found:
            continue  # shape failure already reported
        if isinstance(want, tuple):
            kind = want[0]
            if kind == "nonzero" and not value:
                VALUE_NOTES.append((case["id"], "%s is %r, corpus never sends it"
                                    % (path, value)))
            elif kind == "nonempty" and not value:
                VALUE_NOTES.append((case["id"], "%s is empty" % path))
        elif value != want:
            VALUE_NOTES.append((case["id"], "%s is %r, corpus always sends %r"
                                % (path, value, want)))

    status = "FAIL" if problems else "PASS"
    label = (" or ".join(names) if len(names) > 1 else names[0])
    print("[%2d] %s  %-44s fixture=%s" % (CASES, status, case["id"], label))
    print("        origin: %s" % case["origin"])
    if matched and len(names) > 1:
        print("        matched: %s (corpus allows either ack shape here)" % matched)
    if problems and not err:
        hint = unwrap_hint(reply, load(names[0]))
        if hint:
            print("        %s" % hint)
    for problem in problems[:10]:
        print("        %s" % problem)
    if len(problems) > 10:
        print("        ... %d more" % (len(problems) - 10))
    if problems:
        FAILS.append((case["id"], problems))
    return not problems


def invariants(dispatch, store):
    """Transport/state invariants that no fixture shape can express."""
    global CASES
    print("-- invariants (no fixture: state / totality) " + "-" * 26)

    def report(name, problem):
        global CASES
        CASES += 1
        print("[%2d] %s  %s" % (CASES, "FAIL" if problem else "PASS", name))
        if problem:
            print("        %s" % problem)
            FAILS.append((name, [problem]))

    conq = store.state.setdefault("conquest", {})
    before = conq.get("nextSessionId")
    call(dispatch, {"SessionID": 81650})
    after = conq.get("nextSessionId")
    report("I1 read-only query allocates no session id",
           None if before == after else
           "nextSessionId %r -> %r after a read-only {'SessionID':81650} "
           "(mimic_server.py:1719)" % (before, after))

    junk = [None, 0, 1.5, "x", b"bytes", True, {}, [], {"SessionID": None},
            {"timelimit": "abc"}, {"skey": None}, {"League": {}}, [[[]]]]
    raised = []
    for item in junk:
        reply, err, _ = call(dispatch, item)
        if err:
            raised.append((item, err))
    report("I2 dispatch is total (never raises / always JSON)",
           None if not raised else "%r" % (raised[:3],))

    request = {"SessionID": 81650, "detail": 1}
    frozen = json.dumps(request, sort_keys=True)
    call(dispatch, request)
    report("I3 request document is not mutated",
           None if json.dumps(request, sort_keys=True) == frozen else
           "dispatch modified the request dict")
    print()


def replay(verbose=False):
    module, dispatch, store, where = load_dispatch()
    print("dispatch under test : %s" % where)
    print("mimic_server version: %s" % getattr(module, "SERVER_VERSION", "?"))
    print("ark store           : in-memory FakeStore stand-in "
          "(state is not persisted, server/data is untouched)")
    print()

    for case in build_cases():
        request = case["request"]
        if callable(request):
            request = request()
        reply, err, log_text = call(dispatch, request)
        print_case(case, reply, err)
        if verbose and log_text.strip():
            print("        log: %s" % log_text.strip().replace("\n", " | "))
    print()
    invariants(dispatch, store)
    summary(store)


def summary(store):
    print("-" * 72)
    print("cases: %d   PASS: %d   FAIL: %d"
          % (CASES, CASES - len(FAILS), len(FAILS)))
    if VALUE_NOTES:
        print("value deviations (shape ok, value not corpus-conform): %d"
              % len(VALUE_NOTES))
        for case_id, note in VALUE_NOTES:
            print("   - %-44s %s" % (case_id, note))
    if ORDER_NOTES:
        print("key-order notes (advisory, PHP order not reproduced): %d"
              % len(ORDER_NOTES))
        for note in ORDER_NOTES[:6]:
            print("   - %s" % note)
    print("dispatch state after replay: nextSessionId=%r cloud_keys=%d "
          "saves=%d" % (store.state.get("conquest", {}).get("nextSessionId"),
                        len(store.state.get("conquest", {}).get("cloud", {})),
                        store.saves))
    print("-" * 72)


# ---------------------------------------------------------------------------
# --self-test: rebuild every fixture through server/conquest_shapes.py
# ---------------------------------------------------------------------------

def self_test():
    if SERVER not in sys.path:
        sys.path.insert(0, SERVER)
    cs = importlib.import_module("conquest_shapes")
    print("rebuilding every corpus fixture through conquest_shapes.py")
    print("(if these pass, the fixtures are reachable with the builders the "
          "patch list names)")
    print()

    def check(name, built, fixture, note=""):
        global CASES
        CASES += 1
        problems = shape_problems(built, load(fixture))
        status = "FAIL" if problems else "PASS"
        print("[%2d] %s  %-44s fixture=%s" % (CASES, status, name, fixture))
        if note:
            print("        %s" % note)
        for problem in problems[:8]:
            print("        %s" % problem)
        if problems:
            FAILS.append((name, problems))

    ack = load("cloudsave_ack.json")
    s_ack = load("session_ack.json")
    v_ack = load("session_version_ack.json")
    tl = load("timelimit.json")
    err = load("error.json")
    row = load("leaguerow.json")
    hist = load("league_history.json")
    stats = load("playerstats.json")

    check("empty_list()", cs.empty_list(), "empty_list.json")
    check("ack()", cs.ack(), "return_true.json")
    check("cloud_ack(skey, bucket)",
          cs.cloud_ack(ack["skey"], ack["bucket"]), "cloudsave_ack.json")
    check("session_ack(SessionID)",
          cs.session_ack(s_ack["SessionID"]), "session_ack.json")
    check("version_ack(SessionID, version)",
          cs.version_ack(v_ack["SessionID"], v_ack["version"]),
          "session_version_ack.json")
    check("timelimit(seconds)", cs.timelimit(tl["timelimit"]), "timelimit.json")
    check("error_doc(InfoText, id, version)",
          cs.error_doc(err["error"]["InfoText"], err["error"]["id"],
                       err["error"]["version"]), "error.json")
    check("league_row(Order, SessionState, Team1, Team2, history)",
          cs.league_row(row["Order"], row["SessionState"], row["Team1"],
                        row["Team2"], row["history"]), "leaguerow.json")
    check("history_map(history)", cs.history_map(hist), "league_history.json")
    check("session_list(rows, 'browse')",
          cs.session_list(load("sessionlist.json"), "browse"), "sessionlist.json")
    check("session_list(rows, 'league')",
          cs.session_list(load("sessionlist_league.json"), "league"),
          "sessionlist_league.json")
    check("session_detail(session)",
          cs.session_detail(load("sessiondetail.json")), "sessiondetail.json")
    check("session_detail(session, dynamic=True)",
          cs.session_detail(load("sessiondetail_dynamic.json"), dynamic=True),
          "sessiondetail_dynamic.json")
    check("mission_catalogue(entries)",
          cs.mission_catalogue(load("missions.json")), "missions.json")
    check("session_detail({'Team': ...})['Team']",
          cs.session_detail({"Team": stats})["Team"], "playerstats.json",
          note="playerstats.json is the Team sub-document, not a reply of its own")

    print()
    summary(FakeStore())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-test", action="store_true",
                        help="rebuild the fixtures through conquest_shapes.py "
                             "instead of replaying requests")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="print the dispatcher's own log lines")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
    else:
        replay(verbose=args.verbose)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
