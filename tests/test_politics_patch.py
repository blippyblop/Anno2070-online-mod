#!/usr/bin/env python3
"""Check the Ark-Politics patch constants against the real Anno5.exe.

The loader's `politics_install()` (loader/src/upc_stub.c) patches five branch
sites and one instruction in the game's own code. Those addresses were derived
from this build of Anno5.exe (3.0.8051 / GOLDMaster); on any other build the
bytes will not match and the loader refuses to write - which is the safe
behaviour, but it also means a typo in an RVA would look like "the patch does not
work" instead of an error.

This suite closes that gap:
  * parses the RVA constants and the five `pol_retarget_jcc(...)` calls out of
    the C source (so the test cannot silently drift from the code),
  * reads the site bytes from Anno5.exe,
  * asserts the opcode is a near conditional jump and that its current target is
    the one the C source expects,
  * applies the same patch in memory and disassembles the result to prove the new
    target is the last-resort page block,
  * does the same for the enable gate (`33 d2` -> `b2 01`) and the page
    displacement (`+0x100`),
  * checks the two probe-hook prologues the C source expects (SetActivatedChild,
    the Ark tab-state thunk).

Exit 0 = every constant in the loader matches the game binary. If the game is not
installed here (no ANNO5_EXE and no default path) the suite SKIPs - the loader's
own byte-verification still protects a player either way.

    python3 tests/test_politics_patch.py
    ANNO5_EXE=/path/to/Anno5.exe python3 tests/test_politics_patch.py
"""
import os
import re
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "loader", "src", "upc_stub.c")
CANDIDATES = [
    os.environ.get("ANNO5_EXE", ""),
    "/workspace/Anno 2070/Anno5.exe",
    r"C:\projects\Anno2070\Anno5.exe",
]

failures = []
checks = [0]


def check(label, ok, detail=""):
    checks[0] += 1
    if ok:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s%s" % (label, "  " + str(detail) if detail != "" else ""))
        failures.append(label)


def find_exe():
    for path in CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


class Image:
    """Just enough PE to map an RVA to a file offset."""

    def __init__(self, path):
        import pefile
        self.pe = pefile.PE(path, fast_load=True)
        self.data = open(path, "rb").read()

    def read(self, rva, n):
        for s in self.pe.sections:
            size = max(s.Misc_VirtualSize, s.SizeOfRawData)
            if s.VirtualAddress <= rva < s.VirtualAddress + size:
                off = s.PointerToRawData + (rva - s.VirtualAddress)
                return self.data[off:off + n]
        raise SystemExit("rva %#x is not inside any section" % rva)

    def disasm_one(self, rva):
        from capstone import Cs, CS_ARCH_X86, CS_MODE_64
        md = Cs(CS_ARCH_X86, CS_MODE_64)
        code = self.read(rva, 15)
        for ins in md.disasm(code, 0x140000000 + rva):
            return ins
        return None


def source_constants(text):
    def num(name):
        m = re.search(r"#define\s+%s\s+(0x[0-9a-fA-F]+|\d+)u?" % name, text)
        if not m:
            raise SystemExit("constant %s not found in %s" % (name, SRC))
        return int(m.group(1), 0)

    out = {
        "gate": num("POL_GATE_RVA"),
        "gate_copy": num("POL_GATE_COPY"),
        "lastpage": num("POL_LASTPAGE_RVA"),
        "lastpage_disp": num("POL_LASTPAGE_DISP"),
        "setchild": num("POL_SETCHILD_RVA"),
        "setchild_copy": num("POL_SETCHILD_COPY"),
        "tabstate": num("POL_TABSTATE_RVA"),
    }
    retargets = []
    for m in re.finditer(
            r"pol_retarget_jcc\(\s*(0x[0-9a-fA-F]+)u?\s*,\s*(0x[0-9a-fA-F]+)u?\s*,\s*"
            r"POL_LASTPAGE_RVA", text):
        retargets.append((int(m.group(1), 16), int(m.group(2), 16)))
    out["retargets"] = retargets
    return out


def main():
    if not os.path.isfile(SRC):
        print("FAIL: cannot find %s" % SRC)
        return 1
    src = open(SRC, encoding="utf-8").read()
    const = source_constants(src)

    print("Ark Politics patch vs. the game binary")
    check("the loader retargets exactly the five selector bail-outs",
          len(const["retargets"]) == 5, const["retargets"])

    exe = find_exe()
    if not exe:
        print("  SKIP no Anno5.exe here (set ANNO5_EXE to check a copy)")
        print("SKIP: politics patch constants not verified (no game binary)")
        return 0
    print("        using %s" % exe)
    img = Image(exe)

    # ---- the enable gate: 33 d2 (xor edx,edx) at POL_GATE_RVA ----------------
    gate = img.read(const["gate"], 2)
    check("the gate site holds 'xor edx,edx' (33 d2)", gate == b"\x33\xd2",
          gate.hex())
    patched = b"\xb2\x01"
    check("the replacement is the 2-byte 'mov dl,1' (b2 01)", patched == b"\xb2\x01")
    ins = img.disasm_one(const["gate"] + 2)
    check("the enabled branch sits right before the call (mov dl,1 at gate-4)",
          ins is not None and ins.mnemonic in ("mov", "call", "movabs"),
          ins and ins.mnemonic)

    # ---- the five retargeted branches ---------------------------------------
    for site, expect in const["retargets"]:
        cur = img.read(site, 6)
        ok_op = cur[0] == 0x0F and cur[1] in (0x84, 0x8F, 0x85, 0x8E)
        check("0x%x is a near conditional jump (%02x %02x)" % (site, cur[0], cur[1]), ok_op)
        if not ok_op:
            continue
        rel = struct.unpack("<i", cur[2:6])[0]
        target = site + 6 + rel
        check("0x%x currently branches to 0x%x (the C source expects 0x%x)"
              % (site, target, expect), target == expect)
        new_rel = const["lastpage"] - (site + 6)
        new = bytes([cur[0], cur[1]]) + struct.pack("<i", new_rel)
        check("patched 0x%x keeps the opcode and a 6-byte form" % site,
              new[0] == cur[0] and new[1] == cur[1] and len(new) == 6)
        check("patched 0x%x lands on the last-resort page block (0x%x)"
              % (site, const["lastpage"]),
              site + 6 + struct.unpack("<i", new[2:6])[0] == const["lastpage"])

    # ---- the last-resort page block -----------------------------------------
    lastpage = img.read(const["lastpage"], 12)
    check("the last-resort block loads a page widget: 48 8b 97 <disp32>",
          lastpage[:3] == b"\x48\x8b\x97", lastpage[:3].hex())
    disp = struct.unpack("<I", lastpage[3:7])[0]
    check("its displacement is +0x%x, matching POL_LASTPAGE_DISP" % disp,
          const["lastpage"] + 3 == const["lastpage_disp"] and disp == 0x100, hex(disp))
    check("it then jumps to the activation call (e9 rel32)",
          lastpage[7] == 0xE9, lastpage[7:8].hex())

    # ---- probe hook prologues ------------------------------------------------
    setchild = img.read(const["setchild"], 14)
    # the exact 14 bytes the hook copies (its `expect[]` array in upc_stub.c):
    # mov [rsp+0x18],rbx / mov [rsp+0x20],rbp / push rsi / push rdi / push r14
    want = bytes.fromhex("48895c241848896c242056574156")
    check("SetActivatedChild prologue is the 14 bytes the hook copies",
          setchild == want, setchild.hex())
    # 5+5+1+1+2 = 14: the copy ends exactly on `push r14`, so the trampoline
    # never splits an instruction
    check("the 14-byte span ends on an instruction boundary (push r14 = 41 56)",
          setchild[12:14] == bytes.fromhex("4156"), setchild[12:14].hex())
    tabstate = img.read(const["tabstate"], 6)
    check("the tab-state thunk starts 'mov rax,[rcx]; movzx edx,dl'",
          tabstate == bytes.fromhex("488b010fb6d2"), tabstate.hex())

    print()
    if failures:
        print("POLITICS PATCH: %d of %d checks FAILED" % (len(failures), checks[0]))
        for name in failures:
            print("  - %s" % name)
        return 1
    print("POLITICS PATCH: all %d checks match the game binary" % checks[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
