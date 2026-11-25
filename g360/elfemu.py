"""Minimal AArch64 shared-object runner on top of Unicorn.

Loads a self-contained Android arm64 .so (one that only imports a handful of libc functions), applies its
relocations, provides those libc functions in Python, runs its constructors, and lets Python call exported
functions with the AAPCS64 calling convention. Written to run Samsung's libwsm2.so (the Gear 360 pairing
authentication library) on a PC; the library itself is not distributed with this project.

Requires: unicorn, pyelftools (pip install unicorn pyelftools).
"""
from __future__ import annotations

import os
import struct
import sys
from typing import Callable

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
from unicorn import UC_ARCH_ARM64, UC_HOOK_CODE, UC_HOOK_MEM_INVALID, UC_MODE_ARM, Uc, UcError
from unicorn.arm64_const import (UC_ARM64_REG_LR, UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_TPIDR_EL0,
                                 UC_ARM64_REG_X0)

PAGE = 0x1000
LIB_BASE = 0x4000_0000
STUB_BASE = 0x7000_0000           # one 4-byte "ret" per imported function; hooked to Python
HEAP_BASE, HEAP_SIZE = 0x8000_0000, 64 << 20
STACK_TOP, STACK_SIZE = 0x9000_0000, 1 << 20
DATA_BASE = 0x9100_0000           # imported data objects (e.g. __stack_chk_guard) and TLS
RETURN_ADDR = 0x7FFF_F000         # calls return here; the run stops when PC reaches it

R_AARCH64_ABS64, R_AARCH64_GLOB_DAT, R_AARCH64_JUMP_SLOT, R_AARCH64_RELATIVE = 257, 1025, 1026, 1027
RET = b"\xc0\x03\x5f\xd6"         # AArch64 "ret"


def _align(x: int, a: int = PAGE) -> int:
    return (x + a - 1) & ~(a - 1)


class ElfEmu:
    def __init__(self, path: str, trace_log: bool = False):
        self.uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self.symbols: dict[str, int] = {}
        self.trace_log = trace_log
        self._heap = HEAP_BASE
        self._stubs: dict[int, tuple[str, Callable]] = {}
        self._data_next = DATA_BASE + 0x100
        self.log_lines: list[str] = []

        for base, size in ((STUB_BASE, PAGE * 4), (HEAP_BASE, HEAP_SIZE), (STACK_TOP - STACK_SIZE, STACK_SIZE),
                           (DATA_BASE, PAGE * 4), (RETURN_ADDR & ~(PAGE - 1), PAGE)):
            self.uc.mem_map(base, size)
        self.uc.mem_write(RETURN_ADDR, RET)
        self.uc.reg_write(UC_ARM64_REG_TPIDR_EL0, DATA_BASE)   # bionic TLS slots (stack guard etc.)
        self.uc.mem_write(DATA_BASE + 0x28, struct.pack("<Q", 0x5A5A5A5A5A5A5A5A))
        self.uc.hook_add(UC_HOOK_CODE, self._on_stub, begin=STUB_BASE, end=STUB_BASE + PAGE * 4)
        self.uc.hook_add(UC_HOOK_MEM_INVALID, self._on_bad_mem)

        self._load(path)

    # ---- loading ---------------------------------------------------------------------------------
    def _load(self, path: str) -> None:
        with open(path, "rb") as f:
            elf = ELFFile(f)
            if elf["e_machine"] != "EM_AARCH64":
                raise ValueError(f"{path}: not an AArch64 library")
            loads = [s for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
            top = max(s["p_vaddr"] + s["p_memsz"] for s in loads)
            self.uc.mem_map(LIB_BASE, _align(top))
            for s in loads:
                self.uc.mem_write(LIB_BASE + s["p_vaddr"], s.data())
            dynsym = elf.get_section_by_name(".dynsym")
            syms = list(dynsym.iter_symbols())
            for s in syms:
                if s.name and s["st_shndx"] != "SHN_UNDEF":
                    self.symbols[s.name] = LIB_BASE + s["st_value"]
            imports: dict[str, int] = {}
            for sec in elf.iter_sections():
                if not isinstance(sec, RelocationSection):
                    continue
                for r in sec.iter_relocations():
                    t, off = r["r_info_type"], LIB_BASE + r["r_offset"]
                    addend = r["r_addend"] if r.is_RELA() else 0
                    if t == R_AARCH64_RELATIVE:
                        val = LIB_BASE + addend
                    elif t in (R_AARCH64_GLOB_DAT, R_AARCH64_JUMP_SLOT, R_AARCH64_ABS64):
                        sym = syms[r["r_info_sym"]]
                        if sym["st_shndx"] != "SHN_UNDEF":
                            val = LIB_BASE + sym["st_value"] + addend
                        else:
                            if sym.name not in imports:
                                imports[sym.name] = self._import(sym.name)
                            val = imports[sym.name] + addend
                    else:
                        raise NotImplementedError(f"relocation type {t}")
                    self.uc.mem_write(off, struct.pack("<Q", val))
            init_array = elf.get_section_by_name(".init_array")
            ctors = []
            if init_array is not None:
                raw = self.uc.mem_read(LIB_BASE + init_array["sh_addr"], init_array["sh_size"])
                ctors = [v for (v,) in struct.iter_unpack("<Q", bytes(raw)) if v not in (0, 2**64 - 1)]
        for c in ctors:
            self.call_addr(c)

    def _import(self, name: str) -> int:
        """Address for an imported symbol: a hooked stub for functions, a data cell for known objects."""
        if name == "__stack_chk_guard":
            addr = self._data_next
            self._data_next += 0x10
            self.uc.mem_write(addr, struct.pack("<Q", 0x5A5A5A5A5A5A5A5A))
            return addr
        impl = getattr(self, f"_libc_{name}", None)
        addr = STUB_BASE + 4 * len(self._stubs)
        self.uc.mem_write(addr, RET)
        self._stubs[addr] = (name, impl)
        return addr

    # ---- calling ---------------------------------------------------------------------------------
    def call(self, name: str, *args: int) -> int:
        if name not in self.symbols:
            raise KeyError(f"no exported symbol {name}")
        return self.call_addr(self.symbols[name], *args)

    def call_addr(self, addr: int, *args: int) -> int:
        if len(args) > 8:
            raise NotImplementedError("more than 8 arguments")
        for i, a in enumerate(args):
            self.uc.reg_write(UC_ARM64_REG_X0 + i, a & 0xFFFF_FFFF_FFFF_FFFF)
        self.uc.reg_write(UC_ARM64_REG_SP, STACK_TOP - 0x100)
        self.uc.reg_write(UC_ARM64_REG_LR, RETURN_ADDR)
        try:
            self.uc.emu_start(addr, RETURN_ADDR)
        except UcError as e:
            pc = self.uc.reg_read(UC_ARM64_REG_PC)
            raise RuntimeError(f"emulation fault at {pc:#x} ({self._where(pc)}): {e}") from None
        return self.uc.reg_read(UC_ARM64_REG_X0)

    def _where(self, pc: int) -> str:
        if STUB_BASE <= pc < STUB_BASE + PAGE * 4:
            return f"import {self._stubs.get(pc & ~3, ('?',))[0]}"
        best = max(((n, a) for n, a in self.symbols.items() if a <= pc), key=lambda x: x[1], default=None)
        return f"{best[0]}+{pc - best[1]:#x}" if best else "?"

    def _on_bad_mem(self, uc, access, addr, size, value, data):
        pc = uc.reg_read(UC_ARM64_REG_PC)
        print(f"elfemu: invalid memory access {addr:#x} (size {size}) at {self._where(pc)}", file=sys.stderr)
        return False

    def _on_stub(self, uc, addr, size, data):
        name, impl = self._stubs[addr]
        if impl is None:
            raise RuntimeError(f"library called unimplemented import {name}")
        args = [uc.reg_read(UC_ARM64_REG_X0 + i) for i in range(8)]
        ret = impl(*args)
        uc.reg_write(UC_ARM64_REG_X0, (ret or 0) & 0xFFFF_FFFF_FFFF_FFFF)
        # the stub itself is a "ret", which returns to LR

    # ---- memory helpers --------------------------------------------------------------------------
    def malloc(self, n: int) -> int:
        n = _align(max(n, 1), 16)
        addr = self._heap + 16
        self.uc.mem_write(self._heap, struct.pack("<Q", n))     # size header for realloc
        self._heap = addr + n
        if self._heap > HEAP_BASE + HEAP_SIZE:
            raise MemoryError("emulated heap exhausted")
        return addr

    def write(self, addr: int, data: bytes) -> None:
        self.uc.mem_write(addr, bytes(data))

    def read(self, addr: int, n: int) -> bytes:
        return bytes(self.uc.mem_read(addr, n))

    def cstr(self, addr: int, limit: int = 4096) -> bytes:
        out = bytearray()
        while len(out) < limit:
            b = self.uc.mem_read(addr + len(out), 1)[0]
            if b == 0:
                break
            out.append(b)
        return bytes(out)

    def new_cstr(self, s: str | bytes) -> int:
        b = s.encode() if isinstance(s, str) else s
        p = self.malloc(len(b) + 1)
        self.write(p, b + b"\0")
        return p

    # ---- libc ------------------------------------------------------------------------------------
    def _libc_malloc(self, n, *_):
        return self.malloc(n)

    def _libc_free(self, *_):
        return 0

    def _libc_realloc(self, p, n, *_):
        q = self.malloc(n)
        if p:
            old = struct.unpack("<Q", self.read(p - 16, 8))[0]
            self.write(q, self.read(p, min(old, n)))
        return q

    def _libc_memcpy(self, d, s, n, *_):
        if n:
            self.write(d, self.read(s, n))
        return d

    def _libc_memset(self, d, c, n, *_):
        if n:
            self.write(d, bytes([c & 0xFF]) * n)
        return d

    def _libc_memcmp(self, a, b, n, *_):
        x, y = self.read(a, n), self.read(b, n)
        return 0 if x == y else (-1 if x < y else 1)

    def _libc_memchr(self, s, c, n, *_):
        i = self.read(s, n).find(bytes([c & 0xFF]))
        return 0 if i < 0 else s + i

    def _libc_strlen(self, s, *_):
        return len(self.cstr(s, 1 << 20))

    def _libc_strcmp(self, a, b, *_):
        x, y = self.cstr(a), self.cstr(b)
        return 0 if x == y else (-1 if x < y else 1)

    def _libc_strncmp(self, a, b, n, *_):
        x, y = self.cstr(a, n), self.cstr(b, n)
        return 0 if x == y else (-1 if x < y else 1)

    def _libc_strcpy(self, d, s, *_):
        self.write(d, self.cstr(s, 1 << 20) + b"\0")
        return d

    def _libc_strncpy(self, d, s, n, *_):
        b = self.cstr(s, n)
        self.write(d, (b + b"\0" * n)[:n])
        return d

    def _libc_isxdigit(self, c, *_):
        return 1 if chr(c & 0xFF) in "0123456789abcdefABCDEF" else 0

    # files: only /dev/urandom-style randomness is expected
    _fds: dict = {}

    def _libc_open(self, path, flags, *_):
        p = self.cstr(path).decode(errors="replace")
        if p in ("/dev/urandom", "/dev/random"):
            fd = 100 + len(self._fds)
            self._fds[fd] = p
            return fd
        self.log_lines.append(f"open({p!r}) refused")
        return 2**64 - 1   # -1

    def _libc_read(self, fd, buf, n, *_):
        if fd in self._fds:
            self.write(buf, os.urandom(n))
            return n
        return 2**64 - 1

    def _libc_pthread_mutex_lock(self, *_):
        return 0

    _libc_pthread_mutex_unlock = _libc_pthread_mutex_lock

    def _libc___cxa_atexit(self, *_):
        return 0

    _libc___cxa_finalize = _libc___cxa_atexit

    def _libc___stack_chk_fail(self, *_):
        raise RuntimeError("stack protector tripped inside the emulated library")

    def _libc_exit(self, code, *_):
        raise RuntimeError(f"emulated library called exit({code})")

    def _libc_perror(self, s, *_):
        self.log_lines.append("perror: " + self.cstr(s).decode(errors="replace"))
        return 0

    def _libc___android_log_print(self, prio, tag, fmt, *_):
        line = f"[{self.cstr(tag).decode(errors='replace')}] {self.cstr(fmt).decode(errors='replace')}"
        self.log_lines.append(line)
        if self.trace_log:
            print("libwsm:", line, file=sys.stderr)
        return 0

    def _libc_qsort(self, *_):
        raise RuntimeError("qsort called — needs a re-entrant comparator; not implemented yet")
