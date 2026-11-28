"""Samsung WSM pairing authentication, computed by running Samsung's own libwsm2.so (arm64) in an emulator.

The Samsung Accessory Protocol authenticates both ends with WSM, a white-box-crypto challenge/response keyed
on the two Bluetooth addresses. Rather than re-implement it, we execute Samsung's library (`g360.elfemu`).
The library is NOT shipped with this project. It is found inside Samsung's "SAccessoryService" APK under
lib/arm64-v8a/; find_lib() looks for it in G360_LIBWSM, then under the project's re/ and apk/ folders (where
an extracted APK lands), then at ~/.config/g360/libwsm2.so.

Roles (from com/sec/android/WSM and SASecurityManager): a Server and a Client, each initialised with
(serverID, clientID) = Bluetooth addresses. Fresh authentication:
  1. Client.commitment()            -> commitment            (client -> server)
  2. Server.client_challenge(c)     -> client challenge      (server -> client)
  3. Client.server_challenge(ch)    -> server challenge      (client -> server)
  4. Server.client_response(sch)    -> client response       (server -> client)
  5. Client.check_response(resp)    -> both sides now share the ESAP session key
C signatures recovered from libwsm2_jni.so's JNI wrappers (see NOTES.md).
"""
from __future__ import annotations

import glob
import os
import pathlib
import pwd

ENV_VAR = "G360_LIBWSM"
BUF = 512          # every WSM message fits
# The WSM_* calls return a status (> 0 = OK), not a length; message sizes are fixed per protocol version
# (constants from com/sec/android/WSM/{Client,Server}.java).
SIZES = {
    1: {"commitment": 67, "client_challenge": 70, "server_challenge": 102, "client_response": 35, "esap_key": 32},
    2: {"commitment": 67, "client_challenge": 136, "server_challenge": 200, "client_response": 67, "esap_key": 64},
}


class WSMError(RuntimeError):
    pass


ROOT = pathlib.Path(__file__).resolve().parents[1]


def find_lib() -> str | None:
    """Path of Samsung's arm64 libwsm2.so: $G360_LIBWSM, the project's re/ or apk/ folders, or
    ~/.config/g360/libwsm2.so (the invoking user's, also under sudo)."""
    env = os.environ.get(ENV_VAR)
    if env:
        return env if os.path.exists(env) else None
    for pattern in ("re/**/arm64-v8a/libwsm2.so", "apk/**/arm64-v8a/libwsm2.so"):
        hits = sorted(glob.glob(str(ROOT / pattern), recursive=True))
        if hits:
            return hits[0]
    homes = [pathlib.Path.home()]
    if os.environ.get("SUDO_USER"):
        try:
            homes.insert(0, pathlib.Path(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir))
        except KeyError:
            pass
    for home in homes:
        p = home / ".config" / "g360" / "libwsm2.so"
        if p.exists():
            return str(p)
    return None


NOT_FOUND = (f"Samsung's arm64 libwsm2.so was not found: put it under the project's re/ or apk/ folder "
             f"(as extracted from the SAccessoryService APK), at ~/.config/g360/libwsm2.so, or set {ENV_VAR}")


def _lib_path(lib_path: str | None) -> str:
    p = lib_path or find_lib()
    if not p or not os.path.exists(p):
        raise WSMError(NOT_FOUND)
    return p


class _Party:
    _init_fn = ""

    def __init__(self, server_id: str, client_id: str, key: bytes | None = None, lib_path: str | None = None,
                 trace: bool = False, version: int | None = None):
        from .elfemu import ElfEmu      # needs unicorn/pyelftools (.venv); finding the library does not
        self.emu = ElfEmu(_lib_path(lib_path), trace_log=trace)
        if version is not None:   # the SM-C200 (fw 0.83) speaks WSM protocol version 1
            self.emu.call("WSM_set_protocol_version", version)
        k = 0
        if key is not None:
            k = self.emu.malloc(len(key))
            self.emu.write(k, key)
        self.handle = self.emu.call(self._init_fn, self.emu.new_cstr(server_id), self.emu.new_cstr(client_id), k)
        if self.handle == 0 or self.handle >= 2**63:
            raise WSMError(f"{self._init_fn} failed ({self.handle:#x}); log: {self.emu.log_lines[-3:]}")
        self.version = self.emu.call("WSM_get_current_protocol_version")
        self.sizes = SIZES[self.version]

    def _buf(self, data: bytes = b"") -> int:
        p = self.emu.malloc(BUF)
        self.emu.write(p, data.ljust(BUF, b"\0"))
        return p

    def _check(self, fn: str, ret: int) -> int:
        r = ret if ret < 2**31 else ret - 2**32   # functions return int
        r = r if r < 2**63 else r - 2**64
        if r <= 0:
            raise WSMError(f"{fn} returned {r}; log: {self.emu.log_lines[-3:]}")
        return r

    def esap_key(self) -> bytes:
        p = self.emu.call("WSM_getESAPKey", self.handle)
        if p == 0:
            raise WSMError("WSM_getESAPKey returned NULL (authentication not finished?)")
        return self.emu.read(p, self.sizes["esap_key"])


class Server(_Party):
    """The role Samsung's phone app plays ("Send Server Challenge")."""
    _init_fn = "WSM_Server_init"

    def client_challenge(self, commitment: bytes) -> bytes:
        # SASecurityManager copies the received commitment into a zeroed 136-byte (v2) buffer, which the
        # call then overwrites with the client challenge.
        b = self._buf(commitment[: self.sizes["client_challenge"]])
        self._check("WSM_Server_generateClientChallenge", self.emu.call("WSM_Server_generateClientChallenge", self.handle, b))
        return self.emu.read(b, self.sizes["client_challenge"])

    def client_response(self, server_challenge: bytes) -> bytes:
        i, o = self._buf(server_challenge), self._buf()
        self._check("WSM_Server_checkAndGenerateClientResponse",
                    self.emu.call("WSM_Server_checkAndGenerateClientResponse", self.handle, i, o))
        return self.emu.read(o, self.sizes["client_response"])


class Client(_Party):
    """The other role (presumably the camera). Used here to self-test the Server side."""
    _init_fn = "WSM_Client_init"

    def commitment(self) -> bytes:
        o = self._buf()
        self._check("WSM_Client_generateCommitment", self.emu.call("WSM_Client_generateCommitment", self.handle, o))
        return self.emu.read(o, self.sizes["commitment"])

    def server_challenge(self, client_challenge: bytes) -> bytes:
        i, o = self._buf(client_challenge), self._buf()
        self._check("WSM_Client_checkAndGenerateServerChallenge",
                    self.emu.call("WSM_Client_checkAndGenerateServerChallenge", self.handle, i, o))
        return self.emu.read(o, self.sizes["server_challenge"])

    def check_response(self, client_response: bytes) -> int:
        return self._check("WSM_Client_checkClientResponse",
                           self.emu.call("WSM_Client_checkClientResponse", self.handle, self._buf(client_response)))


def self_test(lib_path: str | None = None, server_addr: str = "11:22:33:44:55:66",
              client_addr: str = "AA:BB:CC:DD:EE:FF", verbose: bool = True) -> bytes:
    """Run a full fresh authentication between an emulated Server and Client; return the shared key."""
    s = Server(server_addr, client_addr, lib_path=lib_path)
    c = Client(server_addr, client_addr, lib_path=lib_path)
    steps = []
    commit = c.commitment();                 steps.append(("client commitment", commit))
    challenge = s.client_challenge(commit);  steps.append(("client challenge", challenge))
    schal = c.server_challenge(challenge);   steps.append(("server challenge", schal))
    resp = s.client_response(schal);         steps.append(("client response", resp))
    c.check_response(resp)
    ks, kc = s.esap_key(), c.esap_key()
    if verbose:
        for name, b in steps:
            print(f"  {name:18} {len(b):4} bytes  {b[:12].hex(' ')}…")
        print(f"  server key {ks[:8].hex()}…  client key {kc[:8].hex()}…  match={ks == kc}")
    if ks != kc:
        raise WSMError("server and client derived different keys")
    return ks
