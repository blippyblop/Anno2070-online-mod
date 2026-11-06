#!/usr/bin/env python3
"""Offline validator for Anno 2070 ARK item records (`rdv::ArkItem`).

An ArkItem is 16 bytes on the wire: four independent u32 little-endian

    {socket_quality, socket_index, item_id, count}

The client's ark deserializers (FUN_1400b8890 / FUN_1400b55e0) drop a record
unless the quality is 1..3 and the ordinal fits the socket group, and they
resolve ``item_id`` against the shipped item DB (features/items.xml), so an id
that is not an item GUID renders as an empty/unknown socket.  This module
restates those rules as a pure function and runs them over candidate sets:

  * quality 1/2/3 -> sockets A/B/C; 4 = WorldPatron is REJECTED by the ark
    deserializer (0 = "All" is rejected too);
  * socket_index must be < the socket count of that quality (A:1, B:2, C:3,
    read from ``socket_layout`` in tools/ark_item_ids.json);
  * item_id must be in ``valid_ids`` (the shipped item GUIDs) and its resolved
    English name is printed; ids listed in ``invalid_ranges`` are refused;
  * count >= 1 (0 means "empty slot" and every reader skips the record);
  * duplicate (quality, socket_index) pairs are refused: one stack per socket.

Advisory (rendering is unaffected, the *insert* check would disagree):
an item whose own ItemQuality does not match the socket's quality would be
refused by the game's own "may go into this socket" test (FUN_1400b8ca0); it
still renders, so this is a warning, not a failure.

Default run validates the server's current starter box (imported read-only
from server/rmc_services.py) plus both starter sets shipped in
tools/ark_item_ids.json, prints a per-item table, then a summary.  Exit status
is non-zero if any item fails, if a negative control is NOT rejected, or if
the table cannot be loaded.

Usage
-----
    source ~/tools/env.sh && python3 tools/ark_selftest.py
    python3 tools/ark_selftest.py --set starter --verbose
    python3 tools/ark_selftest.py --file my_box.json --items '[[3,0,60353,1]]'

Candidate sets can be given as a JSON array of 4-element arrays, of objects
with keys a/b/c/d (the server's on-disk form), f0..f3, w0..w3, or
socket_quality/socket_index/item_id/count.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER_DIR = os.path.join(ROOT, "server")
DEFAULT_TABLE = os.path.join(HERE, "ark_item_ids.json")

#: ItemSocketQuality dataset index -> letter (4 = WorldPatron, not accepted)
QUALITY_NAMES = {0: "All", 1: "A", 2: "B", 3: "C", 4: "WorldPatron"}

#: the six addressable sockets of every Ark asset, in canonical order
SOCKET_ORDER = ((1, 0), (2, 0), (2, 1), (3, 0), (3, 1), (3, 2))

#: ItemQuality choice -> the socket quality it may be inserted into
#: (FUN_1400b8ca0: ItemQuality A/B/C == enum index 0/1/2 -> socket 1/2/3)
ITEM_QUALITY_TO_SOCKET = {"A": 1, "B": 2, "C": 3}

U32_MAX = 0xFFFFFFFF

#: keys the different producers use for the same four words
_FIELD_KEYS = (
    ("a", "b", "c", "d"),                       # rmc_services / rv_shapes
    ("f0", "f1", "f2", "f3"),                   # rv_shapes docstring
    ("w0", "w1", "w2", "w3"),                   # ark-ids.md
    ("socket_quality", "socket_index", "item_id", "count"),
)


# --------------------------------------------------------------------------
# the id table
# --------------------------------------------------------------------------
class ArkTable:
    """tools/ark_item_ids.json, indexed for lookups.

    ``warnings`` carries inconsistencies found while loading (they are data
    problems, not item problems -- see the report for the addon band).
    """

    def __init__(self, path=None):
        self.path = os.path.abspath(path or DEFAULT_TABLE)
        if not os.path.isfile(self.path):
            # The full id table is extracted from the player's own game data
            # and is deliberately not redistributed. Structure validation still
            # runs; id validation is reported as skipped.
            self.raw = {"valid_ids": [], "invalid_ranges": [],
                        "_note": "item table absent - id checks skipped"}
        else:
            with open(self.path, "r", encoding="utf-8") as f:
                self.raw = json.load(f)
        if not isinstance(self.raw, dict) or "valid_ids" not in self.raw:
            raise ValueError("%s: not an ark id table (no valid_ids)" % self.path)

        self.by_id = {}
        for entry in self.raw.get("valid_ids") or []:
            try:
                self.by_id[int(entry["id"])] = entry
            except (KeyError, TypeError, ValueError):
                continue
        # an absent table means "id checks skipped" (see __init__ above);
        # an EMPTY table that was actually present is still an error
        self.no_ids = not self.by_id and bool(self.raw.get("_note"))
        if not self.by_id and not self.no_ids:
            raise ValueError("%s: valid_ids is empty" % self.path)

        layout = (self.raw.get("socket_layout") or {}).get(
            "sockets_per_quality") or {"A": 1, "B": 2, "C": 3}
        self.sockets = {}
        for letter, count in (layout or {}).items():
            q = {v: k for k, v in QUALITY_NAMES.items()}.get(str(letter).strip())
            if q is None:
                continue
            try:
                self.sockets[int(q)] = int(count)
            except (TypeError, ValueError):
                continue
        for q in (1, 2, 3):                       # the ark always has these
            self.sockets.setdefault(q, {1: 1, 2: 2, 3: 3}[q])

        self.invalid_ranges = []
        for pair in self.raw.get("invalid_ranges") or []:
            try:
                lo, hi = int(pair[0]), int(pair[1])
            except (TypeError, ValueError, IndexError):
                continue
            self.invalid_ranges.append((lo, hi))

        self.valid_ids = frozenset(self.by_id)
        self.warnings = self._cross_check()

    # -- helpers --------------------------------------------------------
    def name_of(self, item_id):
        entry = self.by_id.get(int(item_id))
        return entry.get("name", "") if entry else ""

    def entry_of(self, item_id):
        return self.by_id.get(int(item_id))

    def socket_count(self, quality):
        return int(self.sockets.get(int(quality), 0))

    def in_invalid_range(self, item_id):
        item_id = int(item_id)
        for lo, hi in self.invalid_ranges:
            if lo <= item_id <= hi:
                return (lo, hi)
        return None

    def _cross_check(self):
        """Data problems worth printing (and reporting) — not item failures."""
        out = []
        if self.no_ids:
            out.append("valid_ids: table absent (id checks skipped by design)")
            return out
        lo, hi = min(self.valid_ids), max(self.valid_ids)
        out.append("valid_ids: %d ids, range %d..%d" % (len(self.valid_ids), lo, hi))
        for q in sorted(self.sockets):
            out.append("socket_layout: %s=%d" % (QUALITY_NAMES.get(q, q),
                                                 self.sockets[q]))
        overlap = [i for i in sorted(self.valid_ids)
                   if self.in_invalid_range(i) is not None]
        if overlap:
            out.append(
                "TABLE BUG: %d ids in valid_ids also fall inside "
                "invalid_ranges %r (addon band %d..%d). valid_ids is "
                "authoritative here; the range is stale (see "
                "decomp/_adhoc/ark-verify.md)."
                % (len(overlap), self.invalid_ranges, overlap[0], overlap[-1]))
        declared_max = max((hi for _, hi in self.invalid_ranges), default=None)
        if declared_max is not None and declared_max < hi:
            out.append("valid_ids reach %d, past every declared invalid range "
                       "(max %d)" % (hi, declared_max))
        # quality histogram, so a bad table is obvious
        hist = {}
        for entry in self.by_id.values():
            hist[entry.get("quality", "?")] = hist.get(entry.get("quality", "?"), 0) + 1
        out.append("item qualities: " + ", ".join(
            "%s=%d" % (k, hist[k]) for k in sorted(hist)))
        return out


# --------------------------------------------------------------------------
# record coercion + validation
# --------------------------------------------------------------------------
def coerce_record(el):
    """``((w0,w1,w2,w3), problem)`` for one ArkItem-ish value.

    Accepts the four list/dict spellings in use and a raw 16-byte record.
    ``problem`` is None on success, else a human-readable string.
    """
    if isinstance(el, (bytes, bytearray, memoryview)):
        raw = bytes(el)
        if len(raw) != 16:
            return None, "raw record is %d bytes, ArkItem is exactly 16" % len(raw)
        return struct.unpack("<4I", raw), None

    if isinstance(el, dict):
        for keys in _FIELD_KEYS:
            if any(k in el for k in keys):
                vals = [el.get(k, 0) for k in keys]
                break
        else:
            vals = list(el.values())[:4]
        if len(vals) != 4:
            return None, "dict has %d fields, ArkItem needs 4" % len(vals)
        try:
            vals = [int(v) for v in vals]
        except (TypeError, ValueError):
            return None, "non-integer field in %r" % (el,)
        bad = [v for v in vals if not 0 <= v <= U32_MAX]
        if bad:
            return None, "field out of u32 range: %r" % (bad,)
        return tuple(vals), None

    if isinstance(el, (list, tuple)):
        if len(el) != 4:
            return None, "%s has %d fields, ArkItem needs 4" % (
                type(el).__name__, len(el))
        try:
            vals = tuple(int(v) for v in el)
        except (TypeError, ValueError):
            return None, "non-integer field in %r" % (el,)
        bad = [v for v in vals if not 0 <= v <= U32_MAX]
        if bad:
            return None, "field out of u32 range: %r" % (bad,)
        return vals, None

    return None, "not an ArkItem (got %s)" % type(el).__name__


def _record_quality_note(entry, quality):
    """Advisory: the client's insert check (FUN_1400b8ca0), not rendering."""
    if not entry:
        return None
    iq = str(entry.get("quality", ""))
    want = ITEM_QUALITY_TO_SOCKET.get(iq)
    if want is None:
        return ("item quality %r has no A/B/C socket (client insert check "
                "would refuse it; the deserializer still renders it)" % iq)
    if quality in (1, 2, 3) and want != quality:
        return ("item quality %s does not match socket %s (client insert "
                "check would refuse it; deserializer still renders)"
                % (iq, QUALITY_NAMES.get(quality, quality)))
    if entry.get("socketable") is False:
        return "item is not from a socketable template (advisory only)"
    return None


def validate_items(items, table=None):
    """Validate a sequence of ArkItem-ish values.

    Returns ``(rows, ok, table)``.  Each row is a dict::

        {"n", "quality", "index", "item_id", "name", "count",
         "verdict", "problems", "warnings", "raw"}

    ``ok`` is True only when every row is renderable and no duplicate
    (quality, index) pair exists.
    """
    table = table or ArkTable()
    rows = []

    for n, el in enumerate(items or [], 1):
        fields, problem = coerce_record(el)
        row = {"n": n, "raw": el, "quality": None, "index": None,
               "item_id": None, "name": "", "count": None,
               "problems": [], "warnings": []}
        if fields is None:
            row["problems"].append(problem)
            rows.append(row)
            continue

        quality, index, item_id, count = fields
        row.update(quality=quality, index=index, item_id=item_id, count=count)

        if quality not in (1, 2, 3):
            if quality == 4:
                row["problems"].append(
                    "quality 4 = WorldPatron: the ark deserializer only "
                    "accepts 1..3, so the record is dropped")
            elif quality == 0:
                row["problems"].append(
                    "quality 0 = All: the ark deserializer only accepts 1..3 "
                    "(record dropped)")
            else:
                row["problems"].append(
                    "quality %d is not a socket category (need 1=A, 2=B, 3=C)"
                    % quality)
        else:
            nsock = table.socket_count(quality)
            if not 0 <= index < nsock:
                row["problems"].append(
                    "socket_index %d outside %s group, which has %d socket(s) "
                    "0..%d" % (index, QUALITY_NAMES[quality], nsock, nsock - 1))

        entry = table.entry_of(item_id)
        if entry is not None:
            row["name"] = entry.get("name", "")
        rng = table.in_invalid_range(item_id)
        if entry is None:
            if rng is not None:
                row["problems"].append(
                    "item_id %d is inside invalid range %d..%d and is not an "
                    "item GUID" % (item_id, rng[0], rng[1]))
            else:
                row["problems"].append(
                    "item_id %d is not in valid_ids (no such item asset: the "
                    "client resolves it to CInvalidItem and shows nothing)"
                    % item_id)
        elif rng is not None:
            # valid_ids wins: the shipped table's ranges predate the addon ids
            row["warnings"].append(
                "item_id %d is in valid_ids but also inside the table's stale "
                "invalid range %d..%d (table bug, not an item bug)"
                % (item_id, rng[0], rng[1]))
        note = _record_quality_note(entry, quality)
        if note:
            row["warnings"].append(note)

        if count is not None:
            if count < 1:
                row["problems"].append(
                    "count 0 = empty slot (every reader skips it): the socket "
                    "would stay empty")
            elif count > 1:
                row["warnings"].append(
                    "count %d > 1: the client's socket writer hard-codes 1 and "
                    "the stack is a de-duplicating id vector, so >1 probably "
                    "collapses to one item" % count)

        rows.append(row)

    # duplicates: one stack per (quality, index), so every copy is rejected
    by_socket = {}
    for row in rows:
        if row["quality"] is None or row["index"] is None:
            continue
        by_socket.setdefault((row["quality"], row["index"]), []).append(row)
    for (quality, index), group in by_socket.items():
        if len(group) < 2:
            continue
        where = ", ".join("#%d" % r["n"] for r in group)
        for row in group:
            row["problems"].append(
                "duplicate (quality=%d, index=%d) pair (%s): each socket holds "
                "one stack" % (quality, index, where))

    for row in rows:
        row["verdict"] = "OK" if not row["problems"] else "FAIL"

    ok = bool(rows) and all(r["verdict"] == "OK" for r in rows)
    return rows, ok, table


# --------------------------------------------------------------------------
# negative controls: the validator must reject known-bad records
# --------------------------------------------------------------------------
def negative_controls(table=None):
    """``[(label, rejected_bool, detail)]`` — proof the checks have teeth."""
    table = table or ArkTable()
    good = {"id": 60530} if 60530 in table.valid_ids else {
        "id": next(iter(sorted(table.valid_ids)))}
    gid = good["id"]
    cases = [
        ("quality 4 (WorldPatron) is rejected", [[4, 0, gid, 1]]),
        ("quality 0 (All) is rejected", [[0, 0, gid, 1]]),
        ("quality 9 is rejected", [[9, 0, gid, 1]]),
        ("B socket_index 2 is rejected (B has 2)", [[2, 2, gid, 1]]),
        ("A socket_index 1 is rejected (A has 1)", [[1, 1, gid, 1]]),
        ("id 4353 (0x1101) is rejected", [[1, 0, 4353, 1]]),
        ("id 0 is rejected", [[1, 0, 0, 1]]),
        ("u32 sentinel 4294967295 is rejected", [[1, 0, 0xFFFFFFFF, 1]]),
        ("count 0 is rejected", [[1, 0, gid, 0]]),
        ("duplicate (1,0) pair is rejected", [[1, 0, gid, 1], [1, 0, gid, 1]]),
        ("3-field record is rejected", [[1, 0, gid]]),
        ("non-integer field is rejected", [["a", 0, gid, 1]]),
    ]
    out = []
    for label, items in cases:
        rows, ok, _ = validate_items(items, table)
        out.append((label, not ok, rows))
    return out


# --------------------------------------------------------------------------
# candidate sets
# --------------------------------------------------------------------------
def server_defaults():
    """The server's starter box, imported read-only from rmc_services."""
    if SERVER_DIR not in sys.path:
        sys.path.insert(0, SERVER_DIR)
    import rmc_services                        # noqa: E402  (read-only use)
    items = rmc_services._PLAYER_DEFAULTS.get("arkItems", [])
    return [dict(i) if isinstance(i, dict) else i for i in items]


def load_json_set(path):
    """A JSON file holding a list, or an object with ``arkItems``/``items``."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("arkItems", "items", "starter_items"):
            if key in data:
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError("%s: expected a list of ArkItem records" % path)
    return data


def collect_sets(args, table):
    """``[(label, items, required[, error])]`` for every requested set."""
    out = []

    def add(label, loader):
        try:
            out.append((label, loader(), True))
        except Exception as exc:              # noqa: BLE001
            out.append((label, [], True, exc))

    wanted = args.set or ["server", "starter", "alt"]
    if "all" in wanted:
        wanted = ["server", "starter", "alt"]
    if "server" in wanted:
        add("server defaults (rmc_services._PLAYER_DEFAULTS)", server_defaults)
    if "starter" in wanted:
        add("json starter_items", lambda: list(table.raw.get("starter_items") or []))
    if "alt" in wanted:
        add("json starter_items_alt",
            lambda: list(table.raw.get("starter_items_alt") or []))
    for path in args.file or []:
        add(os.path.basename(path), lambda p=path: load_json_set(p))
    for n, expr in enumerate(args.items or [], 1):
        def load_expr(expr=expr):
            data = json.loads(expr)
            if isinstance(data, dict):
                data = data.get("arkItems") or data.get("items") or []
            return data
        add("--items #%d" % n, load_expr)
    return out


# --------------------------------------------------------------------------
# printing
# --------------------------------------------------------------------------
def _fmt_socket(quality, index):
    if quality is None or index is None:
        return "?"
    return "%s%s" % (QUALITY_NAMES.get(quality, str(quality)), index)


def print_rows(label, rows, verbose=False):
    print("\n== %s ==" % label)
    print("  #  sock  w0  w1  %9s  cnt  %-6s %s"
          % ("item_id", "verdict", "name"))
    for row in rows:
        name = row["name"] or ("(unresolved)" if row["item_id"] is not None
                               else "(malformed)")
        if not verbose and len(name) > 52:
            name = name[:49] + "..."
        print("  %-2d %-5s %-3s %-3s %9s  %-3s  %-6s %s" % (
            row["n"], _fmt_socket(row["quality"], row["index"]),
            "-" if row["quality"] is None else row["quality"],
            "-" if row["index"] is None else row["index"],
            "-" if row["item_id"] is None else row["item_id"],
            "-" if row["count"] is None else row["count"],
            row["verdict"], name))
        for problem in row["problems"]:
            print("        ^ FAIL: %s" % problem)
        for warning in row["warnings"]:
            print("        ~ warn: %s" % warning)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        description="Validate Anno 2070 ARK item records offline "
                    "(quality/index/id/count/duplicates).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage\n-----\n", 1)[-1])
    p.add_argument("--table", default=DEFAULT_TABLE,
                   help="id table (default: tools/ark_item_ids.json)")
    p.add_argument("--set", action="append",
                   choices=["server", "starter", "alt", "all"],
                   help="candidate set to validate; repeatable "
                        "(default: all three)")
    p.add_argument("--file", action="append", metavar="JSON",
                   help="extra JSON file with a list of ArkItem records")
    p.add_argument("--items", action="append", metavar="JSON",
                   help="inline JSON list of ArkItem records")
    p.add_argument("--no-controls", action="store_true",
                   help="skip the negative controls")
    p.add_argument("--verbose", action="store_true",
                   help="do not truncate names")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        table = ArkTable(args.table)
    except Exception as exc:                  # noqa: BLE001
        print("FAIL: cannot load id table %s: %r" % (args.table, exc))
        return 2

    print("ARK validator - %s" % table.path)
    for line in table.warnings:
        print("  table: %s" % line)

    sets = collect_sets(args, table)
    total = failed = warned = 0
    broken = []

    for entry in sets:
        label, items = entry[0], entry[1]
        err = entry[3] if len(entry) > 3 else None
        if err is not None:
            print("\n== %s ==" % label)
            print("  FAIL: could not read the set: %r" % (err,))
            failed += 1
            broken.append(label)
            continue
        rows, ok, _ = validate_items(items, table)
        print_rows(label, rows, args.verbose)
        total += len(rows)
        failed += sum(1 for r in rows if r["verdict"] != "OK")
        warned += sum(len(r["warnings"]) for r in rows)
        if not items:
            print("  (empty set: legal, the ARK shows empty sockets)")
        print("  -- %d item(s): %d ok, %d failed"
              % (len(rows), sum(1 for r in rows if r["verdict"] == "OK"),
                 sum(1 for r in rows if r["verdict"] != "OK")))
        if not ok and items:
            broken.append(label)

    controls = []
    if not args.no_controls:
        controls = negative_controls(table)
        print("\n== negative controls (bad records MUST be rejected) ==")
        for label, rejected, _rows in controls:
            print("  %-4s %s" % ("OK" if rejected else "BAD", label))
        bad_controls = [c[0] for c in controls if not c[1]]
        if bad_controls:
            broken.append("negative controls")

    print("\n== summary ==")
    print("  sets        : %d" % len(sets))
    print("  items       : %d" % total)
    print("  failed      : %d" % failed)
    print("  warnings    : %d" % warned)
    if controls:
        print("  controls    : %d/%d rejected as required"
              % (sum(1 for c in controls if c[1]), len(controls)))
    print("  id table    : %d ids" % len(table.valid_ids))
    if broken:
        print("  RESULT      : FAIL (%s)" % "; ".join(broken))
        return 1
    print("  RESULT      : PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
