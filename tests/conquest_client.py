#!/usr/bin/env python3
"""A minimal, game-faithful HTTP/1.1 client for the Anno 2070 conquest channel.

Why this file exists
--------------------
``tools/conquest_probe.py`` speaks the *old* hypothesis (bare JSON documents over
a raw TCP stream) and therefore cannot talk to the real client's server.  The
channel is plain **HTTP/1.1 over WinHTTP** (see
``decomp/_adhoc/conquest-requests.md``)::

    POST /ConquestServer/<Name>.php HTTP/1.1
    Content-Type: application/x-www-form-urlencoded
    Content-Length: <n>

    PlayerName=<name>&Key=<32 hex>&Portrait=<int>&Title=<int>

and the response is ``HTTP/1.1 200 OK`` + ``Content-Length`` + the **exact**
JSON document (no trailing newline, no NUL, no chunking).  ``UploadSavegame.php``
is the one ``multipart/form-data`` request, boundary
``related-designs123456789``, file part ``name="file"; filename="myfile.png"``.

What this module is
-------------------
* one TCP socket, reused for every request (HTTP/1.1 keep-alive) - exactly like
  WinHTTP, which opens the TCP connection on the first ``WinHttpSendRequest``
  and reuses it afterwards;
* request bodies built the way ``FUN_140d81540`` builds them (``k=v&k=v``, body
  order = call order, **no percent-encoding**);
* the response is read by the letter of ``WINHTTP_QUERY_CONTENT_LENGTH``
  (``0x0d8722e``): ``Content-Length`` is mandatory, the body is read for
  exactly that many bytes, anything else is a protocol error;
* the body is validated: it must be a JSON document with **no trailing
  whitespace** (the 2015 corpus has 0/3332 documents ending in space/CR/LF);
* statistics (``connects``/``reconnects``/``requests``) so a harness can prove
  that a whole scenario ran on a single connection.

Usage
-----
In-process against ``conquest_http.serve(0, ...)`` on an ephemeral port::

    from conquest_client import ConquestClient
    with ConquestClient.in_process() as client:
        reply = client.post("PlayerLogin",
                            {"PlayerName": "Probe", "Key": "00" * 16,
                             "Portrait": 0, "Title": 0})
        assert reply.doc == {"Return": True}

Against a remote (live) host::

    python3 tools/conquest_client.py 127.0.0.1:3080 --scenario startup
    python3 tools/conquest_client.py 127.0.0.1:3080 --scenario all --verbose

The command-line mode drives the scenarios from ``conquest_scenarios.py``;
``--verbose`` prints each step's request line - the exact bytes a packet capture
of the real client should show (report section 3.5).

Standard library only.
"""

import argparse
import json
import os
import socket
import sys
import time
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
if os.path.join(ROOT, "server") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "server"))

__all__ = [
    "BOUNDARY", "CONQUEST_PREFIX", "CONTENT_TYPE_FORM", "CONTENT_TYPE_OCTET",
    "Reply", "ConquestClient", "ConquestClientError", "ProtocolError",
    "HTTPStatusError", "encode_form", "encode_multipart",
    "content_type_for", "endpoint_path", "main",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3080                      # AnnoURLs.ConquestServer (scheme-less)

CONQUEST_PREFIX = "/ConquestServer/"
BOUNDARY = "related-designs123456789"    # constant literal in the client (0x0d80cf5)
CONTENT_TYPE_FORM = "application/x-www-form-urlencoded"
CONTENT_TYPE_OCTET = "application/octet-stream"

#: The game treats a slow/absent answer as failure after 90 s (0x0d2b70) and the
#: SessionInfo poll has a 20 s budget; 90 s is the honest default.
DEFAULT_TIMEOUT = 90.0
DEFAULT_CONNECT_TIMEOUT = 5.0
MAX_HEAD = 64 * 1024
MAX_BODY = 64 * 1024 * 1024

JSON_WHITESPACE = b" \t\r\n"

# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class ConquestClientError(Exception):
    """Base class: anything that means "this is not the conquest channel"."""


class ProtocolError(ConquestClientError):
    """The peer did not speak HTTP/1.1 the way WinHTTP requires.

    Missing/duplicate ``Content-Length``, chunked encoding, a malformed status
    line, a short body, or a JSON body with trailing whitespace.
    """

    def __init__(self, message, reply=None):
        super().__init__(message)
        self.reply = reply


class HTTPStatusError(ConquestClientError):
    """A response with a non-2xx status (the game itself ignores the status)."""

    def __init__(self, reply):
        super().__init__("HTTP %s %s for %s"
                         % (reply.status, reply.reason, reply.target))
        self.reply = reply


# ---------------------------------------------------------------------------
# request encoding (mirrors FUN_1400d80980 / FUN_140d81540 / FUN_140d81750)
# ---------------------------------------------------------------------------

def _encode_value(value):
    """Form value -> text, the way the game converts values (``%d`` for ints).

    Booleans: the client has no dedicated bool encoder in the path that was
    reverse-engineered; the corpus request side was never logged.  ``1``/``0``
    is the PHP/WinHTTP-world spelling and is marked INFERRED here.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _items(params):
    """``dict`` *or* list of pairs - a list keeps duplicates and order."""
    if params is None:
        return []
    if isinstance(params, dict):
        return list(params.items())
    return list(params)


def encode_form(params, quote_values=False):
    """``a=1&b=2`` exactly as ``FUN_140d81540`` appends it.

    The report found **no percent-encoding** in the client path (medium
    confidence, section 3.2), so by default values are written verbatim - that
    is what the game puts on the wire and what the server's
    ``urllib.parse.parse_qsl`` accepts.  ``quote_values=True`` is available for
    hand-written probes that need to send ``&``/``=``/spaces safely; the game
    never does.
    """
    parts = []
    for key, value in _items(params):
        key = _encode_value(key)
        text = _encode_value(value)
        if quote_values:
            parts.append("%s=%s" % (quote(key, safe=""), quote(text, safe="")))
        else:
            parts.append("%s=%s" % (key, text))
    return "&".join(parts)


def encode_multipart(params, files=(), boundary=BOUNDARY):
    """Build the ``multipart/form-data`` body of ``UploadSavegame.php``.

    ``files`` is an iterable of ``(field, filename, content_type, data)``.
    Field parts come first, in order, then the file parts, then the closing
    delimiter - exactly the layout of ``FUN_140d81540``/``FUN_140d81750``.
    ``data`` must be ``bytes`` (a ``str`` is UTF-8 encoded).
    """
    bnd = boundary.encode("latin-1")
    out = bytearray()
    for key, value in _items(params):
        out += b"--" + bnd + b"\r\n"
        out += ('Content-Disposition: form-data; name="%s"\r\n\r\n'
                % _encode_value(key)).encode("utf-8")
        out += _encode_value(value).encode("utf-8") + b"\r\n"
    for field, filename, content_type, data in files:
        if isinstance(data, str):
            data = data.encode("utf-8")
        out += b"--" + bnd + b"\r\n"
        out += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (field, filename)).encode("utf-8")
        out += ("Content-Type: %s\r\n\r\n"
                % (content_type or CONTENT_TYPE_OCTET)).encode("latin-1")
        out += bytes(data) + b"\r\n"
    out += b"--" + bnd + b"--\r\n"
    return bytes(out)


def content_type_for(filename):
    """File part content type: ``png`` -> image/png, ``jpg`` -> image/jpg, else octet.

    Report section 4.1: the choice is made from the file *extension*, and a
    ``.sww`` save therefore arrives as ``application/octet-stream``.
    """
    ext = os.path.splitext(str(filename))[1].lower().lstrip(".")
    if ext == "png":
        return "image/png"
    if ext in ("jpg", "jpeg"):
        return "image/jpg"
    return CONTENT_TYPE_OCTET


def endpoint_path(name):
    """``"PlayerLogin"`` -> ``"/ConquestServer/PlayerLogin.php"``.

    Also accepts ``"loca/en.txt"`` (-> ``/ConquestServer/loca/en.txt``), a
    name that already ends in ``.php``, and a full ``/``-rooted path (returned
    unchanged, so a probe can hit an unknown path deliberately).
    """
    text = str(name or "")
    if text.startswith("/"):
        return text
    if text.startswith("ConquestServer/"):
        return "/" + text
    if "/" in text or text.endswith(".php"):
        return CONQUEST_PREFIX + text
    return CONQUEST_PREFIX + text + ".php"


# ---------------------------------------------------------------------------
# one response
# ---------------------------------------------------------------------------

class Reply(object):
    """One complete HTTP response plus the request that produced it."""

    __slots__ = ("name", "target", "request_line", "status", "reason",
                 "headers", "body", "doc", "content_length", "elapsed",
                 "request_bytes", "request_body_length", "raw_head",
                 "connection_id", "request_index")

    def __init__(self, name=None, target="", request_line="", status=0,
                 reason="", headers=None, body=b"", doc=None,
                 content_length=None, elapsed=0.0, request_bytes=0,
                 request_body_length=0, raw_head=b"", connection_id=0,
                 request_index=0):
        self.name = name
        self.target = target
        self.request_line = request_line
        self.status = status
        self.reason = reason
        self.headers = headers if isinstance(headers, dict) else {}
        self.body = body
        self.doc = doc
        self.content_length = content_length
        self.elapsed = elapsed
        self.request_bytes = request_bytes
        self.request_body_length = request_body_length
        self.raw_head = raw_head
        self.connection_id = connection_id
        self.request_index = request_index

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    @property
    def body_length(self):
        return len(self.body)

    @property
    def keep_alive(self):
        return (self.headers.get("connection", "").lower() != "close")

    def __repr__(self):
        return ("Reply(%s %d, %dB, %r)"
                % (self.name or self.target, self.status, len(self.body),
                   self.body[:60]))


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------

class ConquestClient(object):
    """HTTP/1.1 keep-alive client for ``/ConquestServer/*``.

    ``host``/``port`` are the split ``AnnoURLs.ConquestServer`` value (no
    scheme - report section 2).  ``strict_status`` raises on a non-2xx answer;
    the game does not look at the status, so set it to ``False`` only when you
    want to *inspect* a bad reply.
    """

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 timeout=DEFAULT_TIMEOUT, connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 strict_status=True, validate_json=True):
        self.host = str(host)
        self.port = int(port)
        self.timeout = float(timeout)
        self.connect_timeout = float(connect_timeout)
        self.strict_status = bool(strict_status)
        self.validate_json = bool(validate_json)

        self.sock = None
        self._buf = b""
        self._peer_closed = False
        self.server = None                 # set by in_process()

        # statistics a harness can assert on
        self.connects = 0
        self.reconnects = 0
        self.requests = 0
        self.bytes_sent = 0
        self.bytes_received = 0
        self.socket_serial = 0
        self.replies = []

    # -- construction ------------------------------------------------------
    @classmethod
    def in_process(cls, store=None, log=None, idle_timeout=60.0, **kwargs):
        """Start ``conquest_http.serve(0, store, log)`` and point at it.

        The returned client owns the server as ``client.server``; call
        :meth:`close` (or use it as a context manager) to stop both.
        """
        import conquest_http as ch

        server = ch.serve(0, store, log, block=False, idle_timeout=idle_timeout)
        client = cls(server.host, server.port, **kwargs)
        client.server = server
        return client

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # -- connection --------------------------------------------------------
    @property
    def connected(self):
        return self.sock is not None

    @property
    def pending_bytes(self):
        """Bytes read past the current reply (must be 0 on a correct channel)."""
        return len(self._buf)

    @property
    def host_header(self):
        if self.port == 80:
            return self.host
        return "%s:%d" % (self.host, self.port)

    def connect(self):
        """Open the socket if it is not open.  Reconnects are counted."""
        if self.sock is not None:
            return self.sock
        sock = socket.create_connection((self.host, self.port),
                                       timeout=self.connect_timeout)
        sock.settimeout(self.timeout)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:                                  # pragma: no cover
            pass
        if self.socket_serial:
            self.reconnects += 1
        self.socket_serial += 1
        self.connects += 1
        self.sock = sock
        self._buf = b""
        self._peer_closed = False
        return sock

    def close(self):
        """Close the socket (and the in-process server, if we started one)."""
        sock, self.sock = self.sock, None
        self._buf = b""
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        server, self.server = self.server, None
        if server is not None:
            try:
                server.stop()
            except Exception:                            # pragma: no cover
                pass

    def _drop(self):
        """Forget the socket *without* touching the server (peer closed)."""
        sock, self.sock = self.sock, None
        self._buf = b""
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # -- request building --------------------------------------------------
    def _build(self, target, body, content_type):
        head = ("POST %s HTTP/1.1\r\n"
                "Host: %s\r\n"
                "Content-Type: %s\r\n"
                "Content-Length: %d\r\n"
                "Connection: Keep-Alive\r\n"
                "\r\n") % (target, self.host_header, content_type, len(body))
        return head.encode("latin-1") + body

    # -- the two public verbs ---------------------------------------------
    def post(self, name, params=None, quote_values=False):
        """``POST /ConquestServer/<name>.php`` with a urlencoded body."""
        target = endpoint_path(name)
        body = encode_form(params, quote_values=quote_values).encode("utf-8")
        return self._request(name, target, body, CONTENT_TYPE_FORM)

    def post_multipart(self, name, params=None, files=(), boundary=BOUNDARY):
        """``POST`` a ``multipart/form-data`` body (``files`` may be empty)."""
        target = endpoint_path(name)
        body = encode_multipart(params, files, boundary=boundary)
        content_type = "multipart/form-data; boundary=%s" % boundary
        return self._request(name, target, body, content_type)

    def upload_save(self, skey, data, params=None, filename="myfile.png",
                    content_type=None):
        """``UploadSavegame.php``: the one multipart request of the channel.

        ``skey`` is the player's credential value, sent as the ``Key`` form
        field (the report's *response* field ``skey`` is the cloud key and is
        produced by the server, see ``StartMission``).  ``params`` supplies the
        remaining fields in the report's order: ``PlayerName``, ``SessionID``,
        ``MissionIndex``, ``VCValue``, ``VCLevel``, ``SecondsPlayed``; extras
        are appended after them.  A ``Key`` already present in ``params`` wins.

        ``data`` is the raw ``.ssw`` bytes; the file part is built with the
        literal ``filename="myfile.png"`` and ``application/octet-stream``
        (report section 4.1), unless a different type is given.
        """
        extra = dict(params or {})
        fields = {}
        if "PlayerName" in extra:
            fields["PlayerName"] = extra.pop("PlayerName")
        fields["Key"] = extra.pop("Key", skey)
        for key in ("SessionID", "MissionIndex", "VCValue", "VCLevel",
                    "SecondsPlayed"):
            if key in extra:
                fields[key] = extra.pop(key)
        fields.update(extra)                        # extras keep their order
        ctype = content_type or content_type_for(filename)
        return self.post_multipart(
            "UploadSavegame", fields,
            [("file", filename, ctype, data)])

    def loca(self, name):
        """``loca/<name>.txt`` - plain text, *not* JSON (handler 0x0ddab0)."""
        return self.post_raw("/ConquestServer/loca/" + str(name), b"",
                             CONTENT_TYPE_FORM, expect_json=False)

    def post_raw(self, target, body=b"", content_type=CONTENT_TYPE_FORM,
                 expect_json=None, name=None):
        """Escape hatch: a raw target/body (unknown paths, loca, bad bodies).

        ``expect_json=None`` means "JSON unless the path is ``loca/``".
        """
        if expect_json is None:
            expect_json = "loca" not in str(target).lower()
        if isinstance(body, str):
            body = body.encode("utf-8")
        return self._request(name, str(target), body, content_type,
                             expect_json=expect_json)

    # -- transport ---------------------------------------------------------
    def _request(self, name, target, body, content_type, expect_json=True):
        self.connect()
        payload = self._build(target, body, content_type)
        request_line = payload.split(b"\r\n", 1)[0].decode("latin-1")
        started = time.time()
        try:
            self.sock.sendall(payload)
        except OSError as exc:
            self._drop()
            raise ProtocolError("send failed for %s: %r" % (target, exc))
        self.requests += 1
        self.bytes_sent += len(payload)
        reply = self._read_reply(name, target, request_line, len(payload),
                                 started, expect_json=expect_json,
                                 request_body_length=len(body))
        reply.request_index = self.requests
        self.replies.append(reply)
        return reply

    def _recv(self):
        data = self.sock.recv(65536)
        if not data:
            self._peer_closed = True
        return data

    def _read_reply(self, name, target, request_line, request_bytes, started,
                    expect_json=True, request_body_length=0):
        sock = self.sock
        buf = self._buf

        # ---- head ------------------------------------------------------
        while b"\r\n\r\n" not in buf:
            if len(buf) > MAX_HEAD:
                self._drop()
                raise ProtocolError("response head over %d bytes for %s"
                                    % (MAX_HEAD, target))
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                self._drop()
                raise ProtocolError("timed out after %.0fs waiting for %s"
                                    % (self.timeout, target))
            except OSError as exc:
                self._drop()
                raise ProtocolError("recv failed for %s: %r" % (target, exc))
            if not chunk:
                self._drop()
                raise ProtocolError("connection closed before the response "
                                    "head of %s (is this really HTTP/1.1?)"
                                    % (target,))
            buf += chunk
            self.bytes_received += len(chunk)

        raw_head, _, buf = buf.partition(b"\r\n\r\n")
        status, reason, headers = self._parse_head(raw_head, target)

        # ---- Content-Length is mandatory (0x0d8722e) -------------------
        if "content-length" not in headers:
            self._drop()
            raise ProtocolError(
                "%s: response has no Content-Length (WinHTTP fails the request; "
                "chunked is unsupported)" % (target,), None)
        try:
            length = int(str(headers["content-length"]).split(",")[0].strip())
        except (TypeError, ValueError):
            self._drop()
            raise ProtocolError("%s: bad Content-Length %r"
                                % (target, headers["content-length"]))
        if length < 0 or length > MAX_BODY:
            self._drop()
            raise ProtocolError("%s: Content-Length %d out of range"
                                % (target, length))
        if headers.get("transfer-encoding", "").strip().lower() not in ("", "identity"):
            self._drop()
            raise ProtocolError("%s: chunked response (the game cannot read it)"
                                % (target,))

        # ---- exactly `length` body bytes -------------------------------
        while len(buf) < length:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                self._drop()
                raise ProtocolError("%s: timed out after %d/%d body bytes"
                                    % (target, len(buf), length))
            except OSError as exc:
                self._drop()
                raise ProtocolError("%s: recv failed: %r" % (target, exc))
            if not chunk:
                self._drop()
                raise ProtocolError("%s: peer closed after %d of %d body bytes"
                                    % (target, len(buf), length))
            buf += chunk
            self.bytes_received += len(chunk)
        body, self._buf = buf[:length], buf[length:]

        reply = Reply(name=name, target=target, request_line=request_line,
                      status=status, reason=reason, headers=headers, body=body,
                      content_length=length, elapsed=time.time() - started,
                      request_bytes=request_bytes, raw_head=raw_head,
                      request_body_length=request_body_length,
                      connection_id=self.socket_serial)

        # ---- body must be exactly one JSON document --------------------
        if expect_json:
            self._validate_body(reply)

        if reply.headers.get("connection", "").lower() == "close":
            self._drop()                       # WinHTTP would reconnect
        if self.strict_status and not (200 <= status < 300):
            raise HTTPStatusError(reply)
        return reply

    @staticmethod
    def _parse_head(raw_head, target):
        lines = raw_head.split(b"\r\n")
        if not lines or not lines[0]:
            raise ProtocolError("%s: empty status line" % (target,))
        parts = lines[0].decode("latin-1").split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            raise ProtocolError("%s: not an HTTP status line: %r"
                                % (target, lines[0][:80]))
        if parts[0] != "HTTP/1.1":
            # tolerated (WinHTTP accepts 1.0 too) but worth knowing about
            pass
        try:
            status = int(parts[1])
        except ValueError:
            raise ProtocolError("%s: bad status code %r" % (target, parts[1]))
        reason = parts[2] if len(parts) > 2 else ""
        headers = {}
        for line in lines[1:]:
            if not line.strip():
                continue
            name, sep, value = line.partition(b":")
            if not sep:
                raise ProtocolError("%s: malformed header line %r"
                                    % (target, line[:80]))
            key = name.decode("latin-1").strip().lower()
            text = value.decode("latin-1").strip()
            if key in headers:
                headers[key] = headers[key] + ", " + text
            else:
                headers[key] = text
        return status, reason, headers

    def _validate_body(self, reply):
        body = reply.body
        if not body:
            raise ProtocolError("%s: empty response body (the game's JSON parser "
                                "would return -2)" % (reply.target,), reply)
        if body != body.rstrip(JSON_WHITESPACE):
            raise ProtocolError(
                "%s: JSON body has trailing whitespace %r - the client writes "
                "the body verbatim and the 2015 server never sent one"
                % (reply.target, body[-8:]), reply)
        try:
            reply.doc = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolError("%s: body is not valid JSON (%s): %r"
                                % (reply.target, exc, body[:120]), reply)

    # -- convenience -------------------------------------------------------
    def stats(self):
        """One-line summary a human can paste into a bug report."""
        return ("requests=%d connects=%d reconnects=%d sent=%dB recv=%dB"
                % (self.requests, self.connects, self.reconnects,
                   self.bytes_sent, self.bytes_received))


# ---------------------------------------------------------------------------
# command line: the live-verification entry point
# ---------------------------------------------------------------------------

def _split_hostport(text):
    text = str(text or "").strip()
    if not text:
        raise ValueError("empty host:port")
    if "://" in text:
        raise ValueError("AnnoURLs.ConquestServer is scheme-less; use "
                         "host:port (got %r)" % text)
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host or DEFAULT_HOST, int(port)
    return text, DEFAULT_PORT


def _print_result(result, problems, ok, verbose=False):
    mark = "PASS" if ok else "FAIL"
    reply = result.reply
    detail = ""
    if reply is not None:
        detail = "HTTP %d %dB %s" % (reply.status, len(reply.body),
                                     _preview(reply.body))
    elif result.error:
        detail = "!! %s" % (result.error,)
    print("  %-4s %-22s %s" % (mark, result.step.get("name", "?"), detail))
    for problem in problems:
        print("       - %s" % (problem,))
    if verbose and reply is not None:
        print("       request : %s" % (reply.request_line,))
        print("       headers : Content-Length=%s Connection=%s"
              % (reply.headers.get("content-length"),
                 reply.headers.get("connection")))
        print("       body    : %s" % (reply.text[:400],))


def _preview(body, limit=120):
    text = body.decode("utf-8", "replace").replace("\n", " ")
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Drive the real conquest call sequences against a host.")
    parser.add_argument("hostport", nargs="?", default="127.0.0.1:3080",
                        help="host:port of AnnoURLs.ConquestServer "
                             "(default 127.0.0.1:3080; no scheme!)")
    parser.add_argument("--scenario", action="append", default=None,
                        metavar="NAME",
                        help="scenario to run (repeatable, 'all' = every one). "
                             "Default: startup")
    parser.add_argument("--list", action="store_true",
                        help="list the scenarios and exit")
    parser.add_argument("--in-process", action="store_true",
                        help="ignore host:port and start conquest_http on an "
                             "ephemeral loopback port (self-check of the probe)")
    parser.add_argument("--player", default=None,
                        help="override the PlayerName of the scenarios")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    import conquest_scenarios as scenarios

    if args.list:
        for name in scenarios.ORDER:
            scenario = scenarios.SCENARIOS[name]
            print("%-16s %2d steps  %s"
                  % (name, len(scenario["steps"]), scenario["title"]))
        return 0

    try:
        host, port = _split_hostport(args.hostport)
    except ValueError as exc:
        print("error: %s" % (exc,), file=sys.stderr)
        return 2

    selected = args.scenario or ["startup"]
    if "all" in selected:
        selected = list(scenarios.ORDER)
    unknown = [name for name in selected if name not in scenarios.SCENARIOS]
    if unknown:
        print("error: unknown scenario(s) %s; known: %s"
              % (", ".join(unknown), ", ".join(scenarios.ORDER)), file=sys.stderr)
        return 2

    if args.in_process:
        client = ConquestClient.in_process(timeout=args.timeout)
        where = "in-process conquest_http on 127.0.0.1:%d" % client.port
    else:
        client = ConquestClient(host, port, timeout=args.timeout)
        where = "%s:%d" % (host, port)

    print("conquest client -> %s" % (where,))
    overrides = {}
    if args.player:
        overrides["PlayerName"] = args.player

    total = passed = 0
    aborted = False
    started = time.time()
    try:
        for name in selected:
            scenario = scenarios.SCENARIOS[name]
            print("\n== %s - %s ==" % (name, scenario["title"]))
            run = scenarios.run_scenario(client, scenario, overrides=overrides)
            for result in run.results:
                total += 1
                # transport-level checks a live run must also satisfy
                problems = list(result.problems)
                if result.reply is not None:
                    reply = result.reply
                    if reply.status != 200:
                        problems.append("status is %d, not 200" % reply.status)
                    if reply.content_length != len(reply.body):
                        problems.append("Content-Length %s != %d body bytes"
                                        % (reply.content_length, len(reply.body)))
                problems += run.transport_problems
                ok = result.error is None and not problems
                if ok:
                    passed += 1
                _print_result(result, problems, ok, verbose=args.verbose)
            if run.error is not None:
                aborted = True
                print("  scenario aborted: %r" % (run.error,))
                if isinstance(run.error, OSError):
                    print("  hint: nothing accepted the TCP connection on %s - "
                          "is the server up?  On Windows also check "
                          "`netsh winhttp show proxy` (WinHTTP may proxy "
                          "127.0.0.1, report section 9 item 12)." % (where,))
    finally:
        stats = client.stats()
        connects = client.connects
        reconnects = client.reconnects
        client.close()

    channel_ok = passed == total and total > 0 and not aborted
    print("\n%s: %d/%d steps, %s, %.2fs"
          % ("CHANNEL OK" if channel_ok else "CHANNEL FAILED",
             passed, total, stats, time.time() - started))
    if channel_ok:
        print("one TCP connection: connects=%d reconnects=%d"
              % (connects, reconnects))
        if reconnects:
            print("note: the channel reconnected mid-run - WinHTTP survives it, "
                  "but it costs a connect and is worth a look at the server log")
    return 0 if channel_ok else 1


if __name__ == "__main__":
    sys.exit(main())
