#!/usr/bin/env python3
"""End-to-end test of the conquest HTTP channel, driven like the real client.

    source ~/tools/env.sh && python3 tools/test_conquest_scenarios.py [-v]

What this test does
-------------------
1. Starts the real server, ``conquest_http.serve(0, store, log)``, on an
   **ephemeral loopback port** with an in-memory store and a log sink.
2. Runs every scenario of ``conquest_scenarios.py`` (the game's own call
   sequences, see ``decomp/_adhoc/conquest-requests.md``) through
   ``conquest_client.ConquestClient`` - a real TCP socket, HTTP/1.1,
   keep-alive, exactly like WinHTTP.
3. For **every step** asserts the properties the 2015 client relies on:

   * HTTP 200 (the game ignores the status, but the probe must not);
   * the body is valid JSON and parses independently (``json.loads`` again);
   * the body has **no trailing whitespace** and no NUL (0/3332 corpus
     documents end in space/CR/LF);
   * ``Content-Length`` is present, is ``len(body)``, and is the value the
     server actually wrote in the head;
   * the top-level type/key set matches the scenario's expectation;
   * ``StartMission`` bodies are <= 255 bytes and start with ``{`` (0x0d8850);
   * the socket stays alive across the whole run: one connect, zero
     reconnects, every reply on the same socket, no bytes left in the buffer;
   * the server log recorded ``<- POST <path> HTTP/1.1`` with the exact request
     body length and the right handler, followed by exactly one ``-> 200``.

4. Cross-scenario checks that only a stateful run can make: the created
   SessionID appears in the browse list, the cloud save round-trips byte for
   byte and the ``skey`` from ``StartMission`` is the stored key, and the
   ``SessionInfo`` version counter rises monotonically (corpus 96 -> 99 -> 100).
5. A negative control: the client is pointed at stub sockets that answer with
   bare JSON (the old, wrong protocol), a trailing newline, a missing
   ``Content-Length``, a short body and a 500 - every one must be rejected,
   otherwise a green run would mean nothing.

Exit code 0 = every check passed; 1 = at least one failure.
"""

import base64
import json
import os
import socket
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server")
for path in (HERE, SERVER):
    if path not in sys.path:
        sys.path.insert(0, path)

import conquest_http as ch                                    # noqa: E402
import conquest_client as cc                                  # noqa: E402
import conquest_scenarios as sc                               # noqa: E402

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
    print("\n-- %s" % name)


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % label)
        return True
    FAIL += 1
    FAILURES.append("%s: %s%s" % (GROUP[0], label,
                                  (" - %s" % (detail,)) if detail else ""))
    print("  FAIL %s%s" % (label, (" - %s" % (detail,)) if detail else ""))
    return False


def eq(label, got, want):
    return check(label, got == want, "got %r, want %r" % (got, want))


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


# ---------------------------------------------------------------------------
# server log sink
# ---------------------------------------------------------------------------

class LogSink(object):
    """Thread-safe ``log=`` hook; the test reads it back like a capture file."""

    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()

    def __call__(self, message):
        text = str(message)
        with self.lock:
            self.lines.append(text)
        if VERBOSE:
            print("     log| %s" % (text,))

    def snapshot(self):
        with self.lock:
            return list(self.lines)

    def wait_for(self, needle, start=0, timeout=2.0):
        """Index of the first line at/after ``start`` containing ``needle``."""
        deadline = time.time() + timeout
        while True:
            lines = self.snapshot()
            for index in range(start, len(lines)):
                if needle in lines[index]:
                    return index, lines[index]
            if time.time() >= deadline:
                return None, None
            time.sleep(0.01)


# ---------------------------------------------------------------------------
# per-step verification
# ---------------------------------------------------------------------------

class Verifier(object):
    def __init__(self, client, log):
        self.client = client
        self.log = log
        self.cursor = 0
        self.first_connection = None
        self.steps = 0

    def step(self, result):
        """All transport assertions for one scenario step."""
        self.steps += 1
        group("step %d: %s" % (self.steps, result.name))
        item = result.step
        reply = result.reply

        if reply is None:
            check("step produced a reply", False,
                  "error: %r" % (result.error,))
            return

        # -- HTTP status ------------------------------------------------
        eq("HTTP status", reply.status, 200)
        check("status line is HTTP/1.1", reply.raw_head.startswith(b"HTTP/1.1 "),
              reply.raw_head[:60])
        check("reason phrase present", bool(reply.reason), reply.reason)

        # -- framing ----------------------------------------------------
        raw_cl = reply.headers.get("content-length")
        check("Content-Length header present", raw_cl is not None, reply.headers)
        if raw_cl is not None:
            try:
                declared = int(raw_cl)
            except ValueError:
                declared = -1
            eq("Content-Length == body length", declared, len(reply.body))
            eq("client's Content-Length view", reply.content_length, len(reply.body))
            check("Content-Length matches the head the server wrote",
                  ("Content-Length: %d\r\n" % len(reply.body)).encode("latin-1")
                  in reply.raw_head, reply.raw_head[-120:])
        check("no chunked encoding",
              reply.headers.get("transfer-encoding", "").lower() in ("", "identity"),
              reply.headers.get("transfer-encoding"))
        eq("no unread bytes left in the socket buffer", self.client.pending_bytes, 0)

        # -- body -------------------------------------------------------
        body = reply.body
        check("body is not empty", bool(body))
        check("no trailing whitespace",
              body == body.rstrip(b" \t\r\n"), repr(body[-12:]))
        check("no NUL byte", b"\x00" not in body)
        try:
            independent = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            independent = None
            check("body parses as JSON (independent parse)", False, repr(exc))
        else:
            check("body parses as JSON (independent parse)", True)
        check("client parsed the same document", reply.doc == independent)

        # -- the scenario's expectation ---------------------------------
        problems, variant = sc.check_expectation(reply.doc, item.get("expect"),
                                                 body, {})
        check("expected top-level shape", not problems, "; ".join(problems))

        # -- connection ------------------------------------------------
        if self.first_connection is None:
            self.first_connection = reply.connection_id
        eq("same TCP connection", reply.connection_id, self.first_connection)
        eq("one request per step", reply.request_index, self.steps)
        eq("one connect so far", self.client.connects, 1)
        eq("no reconnect so far", self.client.reconnects, 0)
        check("socket still open", self.client.connected)

        # -- the server log ---------------------------------------------
        path = item["path"]
        needle = "<- POST %s HTTP/1.1" % path
        index, line = self.log.wait_for(needle, self.cursor)
        check("server logged the request line + path", index is not None, needle)
        if index is not None:
            self.cursor = index + 1
            eq("logged body byte count", ", %d body bytes" %
               reply.request_body_length in line, True)
            check("logged handler", ("handler=%s" % result.name) in line, line)
            check("logged request line is the first bytes on the wire",
                  reply.request_line == "POST %s HTTP/1.1" % path,
                  reply.request_line)
            # exactly one response for this request
            response_index, response_line = self.log.wait_for(
                "-> 200 %dB " % len(reply.body), index + 1, timeout=1.0)
            check("server logged exactly one response for it",
                  response_index is not None, "no '-> 200 %dB' line after %r"
                  % (len(reply.body), needle))

        # -- step-specific constants ------------------------------------
        if result.name == "StartMission":
            check("StartMission body <= 255 bytes", 0 < len(body) <= 255,
                  "%d bytes" % len(body))
            check("StartMission body starts with '{'", body.startswith(b"{"),
                  repr(body[:10]))
        if result.name == "history":
            check("history keys are 'N_i'",
                  all(str(key).endswith("_i") for key in (reply.doc or {})),
                  list(reply.doc or {})[:5])
        if "error" in (reply.doc or {}):
            err = reply.doc["error"]
            check("error.id is an int (the game's error code)",
                  isinstance(err.get("id"), int) and not isinstance(err.get("id"), bool),
                  err)
            check("error keys are the corpus set",
                  sorted(err) == ["InfoText", "id", "vars", "version"], sorted(err))

        if VERBOSE:
            print("     reply| HTTP %d %dB %r"
                  % (reply.status, len(body), body[:100]))


# ---------------------------------------------------------------------------
# negative control: stub sockets the client must reject
# ---------------------------------------------------------------------------

class StubServer(object):
    """One-shot listener: accepts one connection, reads one request, replies."""

    def __init__(self, response):
        self.response = response
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(2.0)
            try:
                conn.recv(65536)
            except OSError:
                pass
            if self.response:
                conn.sendall(self.response)
            conn.close()
        except OSError:
            pass
        finally:
            try:
                self.sock.close()
            except OSError:
                pass


def _stub_case(label, response, expected_error):
    server = StubServer(response)
    client = cc.ConquestClient("127.0.0.1", server.port, timeout=2.0,
                               connect_timeout=2.0)
    raised = None
    doc = None
    try:
        reply = client.post("PlayerLogin", {"PlayerName": "X"})
        doc = reply.doc
    except cc.ConquestClientError as exc:
        raised = exc
    except Exception as exc:                                  # noqa: BLE001
        raised = exc
    finally:
        client.close()
        server.thread.join(2.0)
    if expected_error is None:
        check(label, raised is None and doc == {"Return": True},
              "raised %r, doc %r" % (raised, doc))
    else:
        check(label, isinstance(raised, expected_error),
              "raised %r, expected %s" % (raised, expected_error.__name__))


def test_negative_control():
    """If these pass, the harness cannot accidentally accept a broken channel."""
    group("control: the checks reject a broken channel")
    doc = b'{"Return":true}'
    _stub_case("a correct reply is accepted",
               b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\n\r\n" + doc, None)
    _stub_case("bare JSON (the old, wrong protocol) is rejected",
               doc, cc.ProtocolError)
    _stub_case("a trailing newline is rejected",
               b"HTTP/1.1 200 OK\r\nContent-Length: 16\r\n\r\n" + doc + b"\n",
               cc.ProtocolError)
    _stub_case("a missing Content-Length is rejected",
               b"HTTP/1.1 200 OK\r\n\r\n" + doc, cc.ProtocolError)
    _stub_case("chunked encoding is rejected",
               b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"f\r\n" + doc + b"\r\n0\r\n\r\n", cc.ProtocolError)
    _stub_case("a short body is rejected",
               b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + doc,
               cc.ProtocolError)
    _stub_case("a 500 is rejected (the probe must not hide it)",
               b"HTTP/1.1 500 Nope\r\nContent-Length: 15\r\n\r\n" + doc,
               cc.HTTPStatusError)


# ---------------------------------------------------------------------------
# the main run
# ---------------------------------------------------------------------------

def main():
    started = time.time()
    store = ch.MemoryStore()
    # a small, static mission catalogue: the startup chapter list is the only
    # endpoint whose real payload (65 KB) is client-side data, not server state
    store.state["conquest"] = {"missionCatalogue": {"0000": {}, "0001": {}}}
    log = LogSink()

    server = ch.serve(0, store, log, block=False, idle_timeout=30.0)
    print("conquest_http on 127.0.0.1:%d (ephemeral), %d scenarios"
          % (server.port, len(sc.ORDER)))
    client = cc.ConquestClient("127.0.0.1", server.port, timeout=20.0,
                               connect_timeout=5.0)
    verifier = Verifier(client, log)
    ctx = {}
    versions = []
    runs = {}

    try:
        for name in sc.ORDER:
            scenario = sc.SCENARIOS[name]
            group("scenario %s - %s" % (name, scenario["title"]))
            run = sc.run_scenario(client, scenario, ctx=ctx)
            runs[name] = run
            for result in run.results:
                verifier.step(result)
                if result.reply is not None and \
                        isinstance(result.reply.doc, dict) and \
                        result.name == "SessionInfo" and \
                        "version" in result.reply.doc:
                    versions.append(result.reply.doc["version"])
            if run.error is not None:
                check("scenario %s completed" % name, False, repr(run.error))
            else:
                check("scenario %s completed" % name, True)

        total_steps = sum(len(run.results) for run in runs.values())
        expected_steps = sum(len(sc.SCENARIOS[n]["steps"]) for n in sc.ORDER)

        # ---- state carried across scenarios --------------------------
        def doc_of(run, name):
            """First document of a named step ({} when the step never ran)."""
            for result in run.results:
                if result.name == name:
                    doc = getattr(result.reply, "doc", None)
                    if doc is not None:
                        return doc
            return {}

        group("state carried across scenarios")
        eq("every scenario ran every step", total_steps, expected_steps)
        lifecycle_sid = doc_of(runs["session_lifecycle"], "NewSession").get("SessionID")
        cloud_sid = doc_of(runs["cloud_save"], "NewSession").get("SessionID")
        check("NewSession returned a positive SessionID",
              isinstance(lifecycle_sid, int) and lifecycle_sid > 0, lifecycle_sid)
        check("cloud-save session has its own SessionID",
              isinstance(cloud_sid, int) and cloud_sid != lifecycle_sid,
              (lifecycle_sid, cloud_sid))

        browse = [result for result in runs["lobby_browse"].results
                  if result.name in ("ConquestSessionList", "ConquestOpenList")
                  and isinstance(getattr(result.reply, "doc", None), list)]
        check("both browse lists answered", len(browse) == 2, len(browse))
        for result in browse:
            ids = [row.get("SessionID") for row in (result.reply.doc or [])]
            check("%s lists the session we created" % result.name,
                  lifecycle_sid in ids, ids)

        catalogue = doc_of(runs["startup"], "ConquestChapterList")
        eq("chapter catalogue served the seeded maps", set(catalogue or {}),
           {"0000", "0001"})

        # ---- the cloud save round trip -------------------------------
        group("cloud save round trip")
        cloud = (store.state.get("conquest") or {}).get("cloud") or {}
        eq("exactly one cloud save stored", len(cloud), 1)
        if len(cloud) == 1:
            skey, entry = list(cloud.items())[0]
            start_mission = doc_of(runs["cloud_save"], "StartMission")
            eq("StartMission returned the stored key", start_mission.get("skey"), skey)
            eq("StartMission bucket", start_mission.get("bucket"),
               (entry or {}).get("bucket"))
            eq("stored size", (entry or {}).get("size"), len(sc.SAVE_BYTES))
            eq("stored bytes round-trip",
               base64.b64decode((entry or {}).get("data") or ""), sc.SAVE_BYTES)
            eq("stored content type", (entry or {}).get("content_type"),
               "application/octet-stream")
            eq("stored filename", (entry or {}).get("filename"), "myfile.ssw")
        else:
            check("cloud save round trip", False, "no cloud entry to inspect")

        # ---- the poll pair and the version counter -------------------
        group("poll pair")
        check("SessionInfo replies bump the version monotonically",
              versions == sorted(set(versions)), versions)
        check("at least three SessionInfo replies (corpus 96->99->100)",
              len(versions) >= 3, versions)
        poll = runs["poll_pair"].results
        check("the poll pair ran both steps", len(poll) == 2, len(poll))
        if len(poll) == 2 and isinstance(getattr(poll[1].reply, "doc", None), dict):
            check("history is a map, not a list", True)
            check("history keys are 'N_i'",
                  all(str(key).endswith("_i") for key in poll[1].reply.doc),
                  list(poll[1].reply.doc)[:5])
        else:
            check("history is a map, not a list", False,
                  "history step did not answer an object")

        # ---- the error path ------------------------------------------
        group("error path")
        error_results = runs["error_path"].results
        if len(error_results) >= 2:
            bad_player = error_results[0]
            observed = bad_player.variant
            label = {0: "error document (2015 behaviour)",
                     1: "normal reply / no player validation"}.get(
                         observed, "no legal shape")
            print("  note: bad PlayerName on ConquestChapterList -> %s" % (label,))
            check("bad player parameter got a legal answer", observed is not None,
                  bad_player.problems)
            error_step = (getattr(error_results[1].reply, "doc", None) or {})
            # PN 177: a file-less UploadSavegame is a DOWNLOAD now, so this step
            # legitimately answers with the corpus cloud ack instead of the error
            # document. Accept either shape and say which one was seen.
            _is_err = (set(error_step) == {"error"} and
                       sorted(error_step.get("error") or {}) ==
                       ["InfoText", "id", "vars", "version"])
            _is_ack = set(error_step) == {"CloudEnable", "skey", "bucket"}
            check("the error step returned a legal corpus shape (error doc or "
                  "cloud ack)", _is_err or _is_ack,
                  "error=%s ack=%s %r" % (_is_err, _is_ack, error_step))
        else:
            check("the error path ran", False, "scenario stopped early")

        # ---- the channel as a whole -----------------------------------
        group("the channel as a whole")
        lines = log.snapshot()
        requests_logged = sum(1 for line in lines if "<- POST " in line)
        responses_logged = sum(1 for line in lines if "-> 200 " in line)
        eq("server logged one request line per step", requests_logged, total_steps)
        eq("server logged exactly one response per request", responses_logged,
           total_steps)
        eq("the whole run used one TCP connection", client.connects, 1)
        eq("no reconnect", client.reconnects, 0)
        eq("one reply object per step", len(client.replies), total_steps)
        check("the socket is still open at the end", client.connected)
        check("every request was a POST /ConquestServer/... path",
              all("POST /ConquestServer/" in reply.request_line
                  for reply in client.replies))
        eq("no unknown-path ack was needed",
           sum(1 for line in lines if "unknown path" in line), 0)
        check("server made progress on its store", store.saves > 0, store.saves)
    finally:
        client.close()
        server.stop()

    # ---- negative control (separate sockets, after the main run) -----
    test_negative_control()

    elapsed = time.time() - started
    print("\n%d passed, %d failed in %.2fs (%d steps, %d scenarios)"
          % (PASS, FAIL, elapsed, verifier.steps, len(sc.ORDER)))
    if FAILURES:
        print("\nfailures:")
        for item in FAILURES:
            print("  - %s" % (item,))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:                                         # pragma: no cover
        traceback.print_exc()
        print("\nHARNESS ERROR - the test itself broke")
        sys.exit(2)
