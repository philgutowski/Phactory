"""No network test guard.

The plan describes this file as lifted from native-relay's tests/_nonet.py.
That repository is not in this checkout, so this is a fresh file written to
the behaviour U5's test scenarios name: a test that reaches the network
through urllib, through a raw socket to a non loopback address, or by
launching gh, curl, wget, or ssh fails with NetworkReached.

pyproject.toml loads this module as a pytest plugin with "-p _nonet", which
pytest imports before any conftest or test module. That ordering is the
point: a module that did "from subprocess import Popen" before the guard was
installed would keep the unguarded class.

Loopback and Unix domain sockets stay open, because the integration suite
talks to a local Postgres and git talks to file paths.
"""

import ipaddress
import os
import socket
import subprocess
import urllib.request

BLOCKED_PROGRAMS = frozenset({"gh", "curl", "wget", "ssh"})
NETWORK_URL_PREFIXES = ("http://", "https://", "ssh://", "git://", "git@")


class NetworkReached(BaseException):
    """A test reached for the network.

    It derives from BaseException so that code under test which catches
    Exception or OSError, as most HTTP client code does, cannot swallow it.
    """


_original_open = urllib.request.OpenerDirector.open
_original_urlopen = urllib.request.urlopen
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_create_connection = socket.create_connection
_original_popen = subprocess.Popen


def _is_local(address) -> bool:
    if not isinstance(address, tuple):
        return True  # a Unix domain socket path
    host = address[0]
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _blocked_urlopen(*args, **kwargs):
    raise NetworkReached(f"urllib.request.urlopen{args!r}")


def _blocked_open(self, fullurl, *args, **kwargs):
    url = fullurl if isinstance(fullurl, str) else fullurl.get_full_url()
    raise NetworkReached(f"urllib OpenerDirector.open({url!r})")


def _guarded_connect(self, address):
    if not _is_local(address):
        raise NetworkReached(f"socket.connect({address!r})")
    return _original_connect(self, address)


def _guarded_connect_ex(self, address):
    if not _is_local(address):
        raise NetworkReached(f"socket.connect_ex({address!r})")
    return _original_connect_ex(self, address)


def _guarded_create_connection(address, *args, **kwargs):
    if not _is_local(address):
        raise NetworkReached(f"socket.create_connection({address!r})")
    return _original_create_connection(address, *args, **kwargs)


def _argv(args) -> list[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    return [os.fsdecode(a) for a in args]


class _GuardedPopen(_original_popen):
    """Popen that refuses network programs and network URLs before forking."""

    def __init__(self, args, *rest, **kwargs):
        argv = _argv(args)
        if kwargs.get("shell"):
            words = argv[0].split() if argv else []
        else:
            words = argv
        if words and os.path.basename(words[0]) in BLOCKED_PROGRAMS:
            raise NetworkReached(f"subprocess {words!r}")
        for word in words:
            if word.startswith(NETWORK_URL_PREFIXES):
                raise NetworkReached(f"subprocess {words!r} names {word!r}")
        super().__init__(args, *rest, **kwargs)


def install() -> None:
    """Patch urllib, socket, and subprocess for the rest of the process."""
    urllib.request.urlopen = _blocked_urlopen
    urllib.request.OpenerDirector.open = _blocked_open
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex
    socket.create_connection = _guarded_create_connection
    subprocess.Popen = _GuardedPopen


install()
