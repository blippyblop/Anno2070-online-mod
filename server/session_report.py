#!/usr/bin/env python3
"""Read a session log and say which online services were exercised, and which
of them the server could only answer with an empty document.

    python3 server/session_report.py [logs/server.log]

This is the diagnostic that turns "I played and something looked off" into a
list of method names, because the client is the only thing that knows what it
asked for.  For every ``Proto::Method`` the game called it prints:

  * how many times it was called,
  * the smallest/largest reply body the server sent,
  * whether this server has a handler for it at all
    (``rmc_services.METHODS``), i.e. whether the answer was real data or the
    stateless fallback,
  * and, when a handler exists, whether it answered with the name block only -
    the signature of an empty document.

The last block is the useful one: everything under "not implemented" is a
concrete gap in the online service, named by the client itself.

No game, no network, no state file needed: it only reads the log.
"""
import collections
import os
import re
import sys

CALL_RE = re.compile(r"RV-RMC \[[^\]]*\] ([A-Za-z0-9_]+)::[A-Za-z0-9_]*::"
                     r"([A-Za-z0-9_]+) call=(\d+)")
REPLY_RE = re.compile(r"RV-RMC reply mode=(\w+) status=(\d) call=(\d+) body=(\d+)B")


#: the client login protocol is answered by the RV login path in
#: ``mimic_server.rv_method_payload``, not by rmc_services' table
LOGIN_HANDLED = {"LoginProtocol::LoginWithToken_V1",
                 "LoginProtocol::LoginWithS2SToken_V1",
                 "LoginProtocol::Register_V1",
                 "LoginProtocol::RedirectLogin_V1",
                 "LoginProtocol::RenewWithTicket_V1"}


def load_methods():
    """``({"Proto::Method", ...}, {"Method", ...} void)`` the server answers."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import rmc_services
        void = {m.rsplit("::", 1)[1] for m in rmc_services.VOID_METHODS}
        return set(rmc_services.METHODS), void
    except Exception:                                # noqa: BLE001
        return set(), set()


def parse(path):
    """``{(proto, method): {"calls": n, "bodies": [sizes], "modes": set()}}``."""
    seen = collections.OrderedDict()
    calls = {}
    for raw in open(path, "r", encoding="utf-8", errors="replace"):
        line = raw.rstrip("\n")
        m = CALL_RE.search(line)
        if m:
            proto, method, call = m.group(1), m.group(2), m.group(3)
            key = (proto, method)
            row = seen.setdefault(key, {"calls": 0, "bodies": [], "modes": set()})
            row["calls"] += 1
            calls[call] = key
            continue
        r = REPLY_RE.search(line)
        if r:
            mode, status, call, body = r.group(1), r.group(2), r.group(3), int(r.group(4))
            key = calls.get(call)
            if key:
                row = seen.setdefault(key, {"calls": 0, "bodies": [], "modes": set()})
                row["bodies"].append(body)
                row["modes"].add(mode + "/" + status)
    return seen


def name_block_only(body, proto, method):
    """True when the reply is just the name block (an empty document)."""
    shortest = len("%s::%s*" % (proto, method)) + 3      # u16 + name + NUL
    return body <= shortest + 16


def main(argv):
    path = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "logs", "server.log")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        print("no server log at %s" % path)
        return 1
    seen = parse(path)
    known, void = load_methods()
    known = set(known) | LOGIN_HANDLED
    if not seen:
        print("no RMC traffic in %s" % path)
        return 1

    print("=" * 78)
    print(" session report: %s" % path)
    print("=" * 78)
    protocols = collections.OrderedDict()
    for (proto, method), row in seen.items():
        protocols.setdefault(proto, []).append((method, row))

    for proto, rows in protocols.items():
        print("\n%s" % proto)
        for method, row in sorted(rows, key=lambda r: (-r[1]["calls"], r[0])):
            bodies = row["bodies"] or [0]
            full = "%s::%s" % (proto, method)
            state = "handler" if full in known else "NO HANDLER"
            if (full in known and method not in void
                    and all(name_block_only(b, proto, method) for b in bodies)):
                state += ", empty reply"
            print("   %-42s %4d call(s)  body %d-%d B  %s"
                  % (method, row["calls"], min(bodies), max(bodies), state))

    missing = [("%s::%s" % (p, m), row["calls"])
               for (p, m), row in seen.items() if ("%s::%s" % (p, m)) not in known]
    empty = [("%s::%s" % (p, m), row["calls"])
             for (p, m), row in seen.items()
             if ("%s::%s" % (p, m)) in known and m not in void
             and row["bodies"] and all(name_block_only(b, p, m) for b in row["bodies"])]
    print("\n" + "-" * 78)
    if missing:
        print("asked for but NOT implemented (%d) - the client got an empty reply:"
              % len(missing))
        for name, count in sorted(missing, key=lambda x: -x[1]):
            print("   %-56s %4d call(s)" % (name, count))
    else:
        print("every method the client asked for is implemented")
    if empty:
        print("implemented but answered empty (%d):" % len(empty))
        for name, count in sorted(empty, key=lambda x: -x[1]):
            print("   %-56s %4d call(s)" % (name, count))
    print("-" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
