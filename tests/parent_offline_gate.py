"""Scoped Python offline policy; load explicitly with -p tests.parent_offline_gate.

Ports the private parent_offline_gate/ctbc_nonorigin_gate denials. This is not
an OS sandbox or a claim about native/approved child-process isolation.
"""
from importlib import import_module
import os
from pathlib import Path
import re
import shutil
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCOPES = frozenset({
    "test_optional_diagnostic_logging.py",
    "test_optional_diagnostic_logging_remaining.py",
    "test_optional_diagnostic_logging_siblings.py",
    "test_parent_offline_gate.py",
})
RUNTIME = tuple({Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()})
SOURCE_SUFFIXES = {".py", ".pyc", ".js", ".cjs", ".mjs"}
active = False
_temp = None
_NODE = shutil.which("node")
_GIT = shutil.which("git")


def deny(*args, **kwargs):
    raise AssertionError("offline gate: forbidden operation")


def in_scope(item):
    return item.nodeid.split("::", 1)[0] in {f"tests/{name}" for name in SCOPES}


def _inside(path, root):
    return root is not None and path.is_relative_to(root)


def _path(value):
    path = Path(os.fsdecode(value))
    if active and _private(path):
        deny()
    return path.resolve()


def _private(path):
    return (path.name == ".env" or ".env." in path.name
            or any(part in path.parts for part in ("secrets", ".aws"))
            or ("data" in path.parts and "site-packages" not in path.parts))


def audit(event, args):
    if not active:
        return
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = _path(args[0])
        if _private(path):
            deny()
        mode, flags = args[1:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if _inside(path, _temp):
            return
        if writing:
            deny()
        if _inside(path, ROOT) and path.suffix in SOURCE_SUFFIXES and ".git" not in path.parts:
            return
        if any(_inside(path, root) for root in RUNTIME):
            return
        deny()
    if event.startswith(("socket.connect", "socket.getaddrinfo", "socket.gethostby", "socket.sendto",
                         "socket.getnameinfo", "socket.sendmsg", "urllib.", "http.client.")):
        deny()
    if event == "subprocess.Popen":
        executable, argv, cwd, env = args
        if not isinstance(argv, (list, tuple)) or not argv:
            deny()
        exe = shutil.which(os.fsdecode(executable))
        # Read-only git object reads only, never options/config injection.
        if exe == _GIT and _GIT and len(argv) == 3 and argv[1] == "show" and re.fullmatch(
                r"(?:HEAD|[0-9a-f]{40}):backend/[A-Za-z0-9_/]+\.py", argv[2]):
            if _path(cwd or Path.cwd()) == ROOT:
                return
        # Node only evaluates synthetic JS on stdin; no script/loader/options.
        if exe == _NODE and _NODE and list(argv[1:]) == ["-"]:
            return
        deny()
    if event in {"os.system", "os.posix_spawn", "os.exec", "os.fork", "pty.spawn"}:
        deny()
    if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate", "os.utime"}:
        if not isinstance(args[0], (str, bytes, os.PathLike)) or not _inside(_path(args[0]), _temp):
            deny()
        if _private(_path(args[0])):
            deny()
    if event in {"os.rename", "os.link", "os.symlink"}:
        deny()


sys.addaudithook(audit)


def _patch_boundaries(patch):
    for sdk in ("playwright", "patchright"):
        cls = import_module(f"{sdk}._impl._browser_type").BrowserType
        for method in ("launch", "launch_persistent_context", "connect", "connect_over_cdp"):
            patch.setattr(cls, method, deny)
    from scrapling.fetchers import StealthyFetcher, StealthySession
    patch.setattr(StealthyFetcher, "fetch", deny)
    patch.setattr(StealthySession, "__init__", deny)
    patch.setattr(socket.socket, "connect", deny)
    patch.setattr(socket.socket, "connect_ex", deny)
    patch.setattr(socket, "getaddrinfo", deny)
    patch.setattr(socket.socket, "sendto", deny)
    from backend.core import creds, base
    for value in vars(creds).values():
        if isinstance(value, type) and issubclass(value, creds.BankCreds):
            patch.setattr(value, "__init__", deny)
            patch.setattr(value, "load", classmethod(deny))
    for name in ("_load_dotenv", "_load_one_env", "_from_env"):
        patch.setattr(creds, name, deny)
    patch.setattr(base.BankCrawler, "__init__", deny)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    global active, _temp
    if not in_scope(item):
        return (yield)
    # Obtain pytest's fresh synthetic root before locking writes; never trust an
    # application data root or permit all of the system temp directory.
    _temp = item.config._tmp_path_factory.getbasetemp().resolve()
    patch = pytest.MonkeyPatch()
    patch.setattr(sys, "dont_write_bytecode", True)
    active = True
    try:
        _patch_boundaries(patch)
        return (yield)
    finally:
        patch.undo()
        active = False
        _temp = None
