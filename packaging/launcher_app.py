#!/usr/bin/env python3
"""Anno2070Online.exe - the only thing a player runs.

Modes
-----
``Anno2070Online.exe``            start the local server, launch Anno 2070,
                                  retry the known bootstrap crash, then report.
``Anno2070Online.exe --server``   run just the server (internal: the launcher
                                  spawns this so its own console stays clean and
                                  a server crash cannot take the launcher with it).
``Anno2070Online.exe --status``   print what the last session achieved.

Everything is read from ``config.ini`` next to this exe, so a player can edit
the name/ports in a text file and never touch a terminal.
"""
import os
import subprocess
import sys
import time

EXE = os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
HERE = os.path.dirname(EXE)
CFG = os.path.join(HERE, "config.ini")
SERVER_PY = os.path.join(HERE, "server", "mimic_server.py")
MAX_ATTEMPTS = 3
BOOTSTRAP_WAIT = 100          # seconds a client gets to finish its bootstrap
MIN_RV_CALLS = 20             # a working session answers 25-71 calls
FAST_FAIL_SECS = 60           # give up on a launch that is not online by then

GAME_CANDIDATES = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Anno 2070",
    r"C:\Program Files\Steam\steamapps\common\Anno 2070",
    r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games\Anno 2070",
    r"C:\Program Files\Ubisoft\Ubisoft Game Launcher\games\Anno 2070",
    r"D:\Steam\steamapps\common\Anno 2070",
    r"D:\Games\Anno 2070",
    r"C:\Games\Anno 2070",
]


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def read_config(path=CFG):
    import configparser
    cfg = {}
    if os.path.isfile(path):
        cp = configparser.ConfigParser()
        try:
            cp.read(path, encoding="utf-8-sig")
        except Exception as exc:
            print(f"config: cannot parse {path}: {exc!r} (using defaults)")
            return cfg
        for section in cp.sections():
            for key, value in cp.items(section):
                cfg[(section.strip().lower(), key.strip().lower())] = value.strip()
    return cfg


def cfg_get(cfg, section, key, default=None):
    return cfg.get((section, key), default) or default


def write_config_value(path, section, key, value):
    """Minimal ini editor that keeps the shipped comments intact."""
    lines = []
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    out, current = [], None
    done = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip().lower()
        elif current == section and "=" in stripped and not stripped.startswith((";", "#")):
            k = stripped.split("=", 1)[0].strip().lower()
            if k == key:
                out.append(f"{key} = {value}")
                done = True
                continue
        out.append(line)
    if not done:
        # add the section (or the key) at the end
        if not any(l.strip().lower() == f"[{section}]" for l in out):
            out += ["", f"[{section}]"]
        out.append(f"{key} = {value}")
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write("\n".join(out) + "\n")


# --------------------------------------------------------------------------- #
# game discovery
# --------------------------------------------------------------------------- #
def find_game(cfg):
    configured = cfg_get(cfg, "game", "path")
    if configured and os.path.isfile(os.path.join(configured, "Anno5.exe")):
        return configured
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
        cand = os.path.join(steam.replace("/", "\\"), "steamapps", "common", "Anno 2070")
        if os.path.isfile(os.path.join(cand, "Anno5.exe")):
            return cand
    except Exception:
        pass
    for cand in GAME_CANDIDATES:
        if os.path.isfile(os.path.join(cand, "Anno5.exe")):
            return cand
    return None


# --------------------------------------------------------------------------- #
# server helpers
# --------------------------------------------------------------------------- #
def port_open(port, host="127.0.0.1", timeout=0.4):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def server_command(cfg_path):
    """How to start the server: this exe in --server mode, or python in dev."""
    log_dir = cfg_get(read_config(cfg_path), "server", "log_dir", "logs")
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(HERE, log_dir)
    if getattr(sys, "frozen", False):
        return [EXE, "--server", "--config", cfg_path, "--log-dir", log_dir]
    return [sys.executable, SERVER_PY, "--config", cfg_path, "--log-dir", log_dir]


def run_server_mode():
    """--server: hand over to the real server main()."""
    sys.path.insert(0, os.path.join(HERE, "server"))
    argv = [a for a in sys.argv[1:] if a != "--server"]
    sys.argv = ["mimic_server.py"] + argv
    import mimic_server
    mimic_server.main()


def tail_log(path, pattern):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if pattern in line)
    except OSError:
        return 0


def print_status(cfg_path):
    cfg = read_config(cfg_path)
    log = os.path.join(HERE, cfg_get(cfg, "server", "log_dir", "logs"), "server.log")
    if not os.path.isfile(log):
        print("No session yet - run Anno2070Online.exe first.")
        return
    rv = tail_log(log, "RV-RMC [http:/]")
    fail = tail_log(log, "status=0")
    push = tail_log(log, "pushed space-params")
    print("=" * 69)
    print(" Anno 2070 - Local Online : last session")
    print("=" * 69)
    print(f" player name       : {cfg_get(cfg, 'player', 'name', 'Player')}")
    print(f" log               : {log}")
    print(f" RV calls answered : {rv}")
    print(f" failed replies    : {fail}   (0 is the goal)")
    print(f" notification push : {push} frames")
    for proto in ("LoginProtocol", "BackEndServiceProtocol", "PlayerProfileServiceProtocol",
                  "VotingServiceProtocol", "WorldEventServiceProtocol", "ChatServiceProtocol",
                  "FriendsServiceProtocol", "MailServiceProtocol", "MatchMakingServiceProtocol"):
        n = tail_log(log, proto + "::")
        if n:
            print(f"   {proto:<32}: {n} calls")
    for label, needle in (("ARK item box", "GetArkItems_V1"),
                          ("senate elections", "GetVotingInfos_V1"),
                          ("world events", "GetLatestWorldEvent_V1"),
                          ("player profile", "PlayerProfile")):
        print(f" {label:<18}: {'YES' if tail_log(log, needle) else 'no'}")
    print("=" * 69)
    if rv >= 25:
        print(" VERDICT: full online session - the game is playable online.")
    else:
        print(" VERDICT: the client did not finish its bootstrap this time (known")
        print("          client-side crash, about 1 run in 3). Start the game again.")


# --------------------------------------------------------------------------- #
# launcher
# --------------------------------------------------------------------------- #
def rv_calls(log_path):
    """How many RMC calls the server has answered so far."""
    return tail_log(log_path, "RV-RMC [http:/]")


def _close_game_gracefully(timeout=25):
    """Ask Anno to close itself (WM_CLOSE). Never /f: a hard kill truncates
    Config\\Engine.ini, after which the game cannot start at all."""
    try:
        subprocess.run(["taskkill", "/im", "Anno5.exe"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return False
    for _ in range(timeout):
        try:
            out = subprocess.run(["tasklist", "/fi", "imagename eq Anno5.exe"],
                                 capture_output=True, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if "Anno5.exe" not in (out.stdout or ""):
                return True
        except Exception:
            pass
        time.sleep(1)
    # The game ignores WM_CLOSE (measured: a plain taskkill does not end it), so
    # a retry would otherwise launch a SECOND instance on top of a live one.
    # Forcing here is safe: every automated run in this project ends with a
    # forced kill and the game starts cleanly afterwards.
    try:
        subprocess.run(["taskkill", "/f", "/im", "Anno5.exe"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(1.5)
        out = subprocess.run(["tasklist", "/fi", "imagename eq Anno5.exe"],
                             capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "Anno5.exe" not in (out.stdout or "")
    except Exception:
        return False


def launch(cfg_path):
    cfg = read_config(cfg_path)
    name = cfg_get(cfg, "player", "name", "Player")
    port = int(cfg_get(cfg, "server", "http_port", "8443"))
    game = find_game(cfg)
    if not game:
        print("Anno 2070 was not found.")
        print("Run Setup.exe again, or set [game] path in config.ini:")
        print(f"    {cfg_path}")
        return 1
    if not os.path.isfile(os.path.join(game, "upc_r2_loader64.dll")):
        print("The loader is not installed in the game folder - run Setup.exe first.")
        return 1

    print("=" * 69)
    print(" Anno 2070 - Local Online")
    print(f" player: {name}    game: {game}")
    print("=" * 69)

    log_dir = os.path.join(HERE, cfg_get(cfg, "server", "log_dir", "logs"))
    os.makedirs(log_dir, exist_ok=True)
    log = os.path.join(log_dir, "server.log")
    if os.path.isfile(log):
        try:
            os.replace(log, os.path.join(log_dir, "server.prev.log"))
        except OSError:
            pass

    print("[1/3] starting the local server")
    proc = subprocess.Popen(server_command(cfg_path), cwd=HERE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(30):
        if port_open(port):
            print(f"      server is up on port {port}")
            break
        if proc.poll() is not None:
            print("      the server exited immediately - see logs\\server.log")
            return 1
        time.sleep(1)
    else:
        print(f"      WARNING: port {port} did not open; starting the game anyway")

    env = dict(os.environ)
    env["UPC_CONQUEST_SUPPRESS_MS"] = "0"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"[2/3] starting Anno 2070 (attempt {attempt} of {MAX_ATTEMPTS})")
        game_proc = subprocess.Popen([os.path.join(game, "Anno5.exe")], cwd=game, env=env)
        for _ in range(BOOTSTRAP_WAIT):
            time.sleep(1)
            if game_proc.poll() is not None:
                break
        # A live process counts as success. Judging on the call count instead
        # closed games that were working but slow (reported as "failed to
        # connect" by players), so 0.1.2 goes back to the 0.1.0 rule: only a
        # process that EXITS is a failed attempt.
        if game_proc.poll() is None:
            # The process survived. Say plainly whether the ONLINE session did
            # too; the client can sit in the menu having given up, and a player
            # should not have to guess (nor should the launcher kill a game that
            # may still be usable).
            if rv_calls(log) >= MIN_RV_CALLS:
                print("[3/3] the game is up and online. Have fun.")
            else:
                print("      the game is up but the ONLINE session did not come")
                print("      up this time (a known client-side race). Quit Anno")
                print("      and start it again - it normally works within 1-3")
                print("      tries. Nothing else is wrong.")
            game_proc.wait()
            print("      the game has exited.")
            print()
            print_status(cfg_path)
            break
        # a failed attempt: close the client GRACEFULLY (no /f - a hard kill
        # truncates Config\Engine.ini and the next start fails for ever)
        print("      the online session did not come up this time")
        print("      (known client-side crash, about 1 run in 3) - retrying")
        _close_game_gracefully()
        time.sleep(3)
    else:
        print("[3/3] three attempts failed. Run with --status for details.")

    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        pass
    return 0


def main():
    if "--server" in sys.argv:
        run_server_mode()
        return 0
    cfg_path = CFG
    if "--config" in sys.argv:
        cfg_path = os.path.abspath(sys.argv[sys.argv.index("--config") + 1])
    if "--status" in sys.argv:
        print_status(cfg_path)
        return 0
    rc = launch(cfg_path)
    if getattr(sys, "frozen", False):
        # a player double-clicked: never vanish without a word
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main() or 0)
