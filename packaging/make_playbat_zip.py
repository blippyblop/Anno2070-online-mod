#!/usr/bin/env python3
"""Build the PLAY.bat release zip (the build that is verified working).

    python3 tools/make_playbat_zip.py [--out release/Anno2070Online-playbat-v<VERSION>.zip]
    python3 packaging/make_playbat_zip.py --out dist/Anno2070Online-playbat-v0.1.9.zip

Everything is taken from anno2070-online/ (or the repository root when this file
lives in packaging/, which is how the release workflow runs it) except the files that only make sense
in the repository (packaging/, .github/, release/), plus a generated
READ-ME-FIRST.txt written for someone who just wants to play.

The archive is checked before it is written: no credentials, no host names, no
game files, no build leftovers.
"""
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Two layouts are supported: the development tree (this script in tools/, the
# project in ./anno2070-online/) and the repository itself (this script in
# packaging/, the project at the root), which is how CI builds the release.
SRC = os.path.join(ROOT, "anno2070-online")
if not os.path.isdir(SRC):
    SRC = ROOT
TOP = "Anno2070Online"
def _default_out():
    """release/Anno2070Online-playbat-v<VERSION>.zip -- one name per release."""
    return os.path.join(ROOT, "release",
                        "Anno2070Online-playbat-v%s.zip" % version())


DEFAULT_OUT = None            # resolved in main(), once VERSION is known

INCLUDE_FILES = [
    "INSTALL.bat", "UNINSTALL.bat", "PLAY.bat", "PROGRESS.bat", "SELFTEST.bat",
    "config.ini", "README.md", "LICENSE", "VERSION",
]
INCLUDE_DIRS = ["docs", "loader", "server", "tests"]
SKIP_NAMES = {"state.json", "state.json.bak", "state.json.lock",
              "server.log", "server.prev.log", "upc_stub.log"}
SKIP_DIRS = {"__pycache__", ".git"}
# developer archaeology, not needed by a player, and full of internal paths
SKIP_FILES = {"protocol-notes.md"}
ONLY_EXT = {".py", ".md", ".bat", ".sh", ".json", ".c", ".h", ".ps1", ".dll", ".txt"}
SKIP_EXT = {".pyc", ".log", ".zip", ".exe", ".rda", ".sdb"}

# things that must never leave the development box
SECRET_PATTERNS = [
    (re.compile(rb"(?i)\.creds"), "a credentials file reference"),
    (re.compile(rb"github_token", re.I), "a github token"),
    (re.compile(rb"ghp_[A-Za-z0-9]{20,}"), "a github token"),
    (re.compile(rb"BEGIN (RSA|OPENSSH|PRIVATE) "), "a private key"),
    (re.compile(rb"ssh-(rsa|ed25519) AAAA"), "a public key"),
    # a real password assignment; the dummy Ubisoft Kerberos constant the
    # protocol needs is not one
    (re.compile(rb"(?i)\b(pass|passwd|password|secret)\s*[:=]\s*['\"][^'\"\r\n]{6,}"),
     "a password"),
]
SECRET_ALLOW = [
    re.compile(rb"(?i)DEFAULT_PASSWORD\s*=\s*\"UbiDummyPwd\""),
    re.compile(rb"(?i)UbiDummyPwd"),
]

# worth knowing about, but not a reason to refuse
NOTICE_PATTERNS = [
    (re.compile(rb"/workspace/"), "a development path (documentation)"),
    (re.compile(rb"(?i)Anno5\.exe\.orig"), "a game binary name"),
]

README = """\
Anno 2070 - Local Online  (PLAY.bat build, {version})

WHAT THIS IS
  A local replacement for the shut-down Anno 2070 online backend: the online
  session, ARK item box, senate and World Council elections and votes, world
  events and your profile. No Python knowledge needed; Python itself is
  required (see below).

HOW TO USE IT
  1. Extract this folder so it sits INSIDE your Anno 2070 folder, i.e. you end
     up with
         ...\\Anno 2070\\Anno5.exe
         ...\\Anno 2070\\Anno2070Online\\PLAY.bat
     (Extracting it anywhere else also works - PLAY.bat will ask for the game
     folder, or you can put it in config.ini.)

  2. Install Python 3.8+ once: https://www.python.org/downloads/windows/
     Tick "Add python.exe to PATH" during setup.

  3. Double-click  PLAY.bat  in this folder.

  PLAY.bat starts the local server ONCE (it waits until the server answers, then
  launches the game - no retry loop), copies the loader DLL next to Anno5.exe
  and leaves the game alone. If a launch drops to offline mode, start it again:
  the server keeps everything you already earned.

  docs\\PLAYING.md  is the player guide: what works, how the month-long world
                event fills up, how ARK sockets and mission rewards behave,
                what the career page does and does not show, and what to do when
                a launch ends offline.

  PROGRESS.bat  shows what the last session achieved, and
  SELFTEST.bat  checks the whole thing without starting the game: career,
                ARK, world event, mails and the save file, in one go. Nothing
                is written to your own save.

  Your name and settings live in config.ini (plain text):
      [player]
      name = Player

YOUR PROGRESS IS SAVED
  Everything you earn lives in one plain JSON file:
      Anno2070Online\\server\\state.json
  career reputation, the ARK item box, your senate and World Council votes,
  the world event of the day and which missions of it you finished. It is
  written after every change, with state.json.bak kept as the previous copy,
  so quitting, restarting or a power cut does not lose it.
  PROGRESS.bat prints exactly what is in it after a session.

HOW TO REMOVE IT AGAIN
  Double-click  UNINSTALL.bat  in this folder and confirm. It stops a server
  that is still running, puts Ubisoft's original upc_r2_loader64.dll back if it
  replaced one (a copy is kept as upc_r2_loader64.dll.original-backup), keeps
  your online state in %LOCALAPPDATA%\\Anno2070Online-backup, then deletes this
  folder and the shortcuts.

      UNINSTALL.bat            ask, then remove it, keep your state
      UNINSTALL.bat /purge     remove everything including the saved state
      UNINSTALL.bat /yes       no questions

  Your savegames are not touched. If the game is running, quit it from the
  game's own menu first - do not kill it, that can truncate Config\\Engine.ini.

KNOWN LIMITS
  * About 6 launches in 10 do not come up online. The game does NOT crash; it
    sits in offline mode. Quit and start again - 1 to 3 tries normally works.
  * Conquest / multiplayer is not reachable yet.
  * Never end the game with Task Manager: it can truncate Config\\Engine.ini.
    Quit from the game's own menu.

NOT INCLUDED
  No game code, data or assets. The only file placed in the game folder is
  upc_r2_loader64.dll, which is this project's own code.
"""


def version():
    try:
        with open(os.path.join(SRC, "VERSION"), "r", encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def collect():
    """(arcname, abspath) for everything that goes into the zip."""
    items = []

    def add(path, arc):
        items.append((arc, path))

    for name in INCLUDE_FILES:
        path = os.path.join(SRC, name)
        if os.path.isfile(path):
            add(path, f"{TOP}/{name}")
        else:
            print(f"  ! missing: {name}")
    for name in INCLUDE_DIRS:
        base = os.path.join(SRC, name)
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if fname in SKIP_NAMES or fname in SKIP_FILES or ext in SKIP_EXT:
                    continue
                if ONLY_EXT and ext not in ONLY_EXT:
                    continue
                path = os.path.join(root, fname)
                rel = os.path.relpath(path, SRC).replace(os.sep, "/")
                add(path, f"{TOP}/{rel}")
    return items


def main():
    out = _default_out()
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    ver = version()
    items = collect()
    problems = []

    print(f"building {os.path.relpath(out, ROOT)}  ({len(items)} files, version {ver})")
    payload = []
    notices = set()
    for arc, path in items:
        with open(path, "rb") as fh:
            data = fh.read()
        allowed = any(p.search(data) for p in SECRET_ALLOW)
        for pattern, what in SECRET_PATTERNS:
            if pattern.search(data) and not allowed:
                problems.append(f"{arc}: contains {what}")
        for pattern, what in NOTICE_PATTERNS:
            if pattern.search(data):
                notices.add(what)
        payload.append((arc, path, data))

    readme = README.format(version=ver).replace("\n", "\r\n").encode("utf-8")
    payload.append((f"{TOP}/READ-ME-FIRST.txt", None, readme))

    if problems:
        print("REFUSING TO WRITE - the package would leak something:")
        for p in problems:
            print("  -", p)
        return 1
    for what in sorted(notices):
        print(f"  note: the package contains {what}")

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arc, path, data in payload:
            info = zipfile.ZipInfo(arc, date_time=(2026, 11, 6, 12, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    size = os.path.getsize(out)
    print(f"wrote {out}  ({size/1024:.0f} KB, {len(payload)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
