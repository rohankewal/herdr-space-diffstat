"""Minimal client for herdr's newline-delimited JSON socket API.

herdr answers exactly one request per connection and then hangs up, so `Client`
opens a short-lived connection per call. Event subscriptions are the exception:
that connection stays open and streams, which is what `EventStream` is for.

Same wire handling as herdr-nerd-font-tab-name, which is where it came from.
"""

import json
import os
import socket

REQUEST_ID = "hsd-1"


class HerdrError(Exception):
    """An error response from the herdr server."""


def socket_path():
    """Resolve the socket the same way the herdr CLI does."""
    explicit = os.environ.get("HERDR_SOCKET_PATH")
    if explicit:
        return explicit
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    root = os.path.join(config_home, "herdr")
    session = os.environ.get("HERDR_SESSION")
    if session:
        return os.path.join(root, "sessions", session, "herdr.sock")
    return os.path.join(root, "herdr.sock")


class _Wire:
    """A connected socket that reads newline-delimited JSON messages."""

    def __init__(self, path, timeout=None):
        self.path = path
        self._buffer = b""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send(self, method, params=None, request_id=REQUEST_ID):
        payload = {"id": request_id, "method": method, "params": params or {}}
        self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        return request_id

    def has_buffered(self):
        """True when a complete message is already in the read buffer."""
        return b"\n" in self._buffer

    def read_message(self):
        """Read one message. None means the server closed the connection."""
        while b"\n" not in self._buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        if not line.strip():
            return self.read_message()
        return json.loads(line.decode("utf-8"))


def _unwrap(message, method):
    if message is None:
        raise HerdrError("herdr closed the connection during {}".format(method))
    if "error" in message:
        error = message["error"]
        raise HerdrError("{}: {}".format(error.get("code", "error"), error.get("message", "")))
    return message.get("result", {})


class Client:
    """Request/response access to a herdr session."""

    def __init__(self, path=None, timeout=10.0):
        self.path = path or socket_path()
        self.timeout = timeout

    def request(self, method, params=None):
        with _Wire(self.path, timeout=self.timeout) as wire:
            wire.send(method, params)
            while True:
                message = wire.read_message()
                if message is None or message.get("id") == REQUEST_ID:
                    return _unwrap(message, method)

    def snapshot(self):
        return self.request("session.snapshot").get("snapshot", {})

    def report_workspace_metadata(self, workspace_id, source, tokens, seq=None, ttl_ms=None):
        """Set display-only tokens on a workspace. A None value clears a token.

        Tokens are advisory: herdr renders them only where the user's
        [ui.sidebar.spaces] rows ask for them, and drops them when the ttl
        expires — so a watcher that dies takes its numbers with it rather than
        leaving stale ones on screen.
        """
        params = {"workspace_id": workspace_id, "source": source, "tokens": tokens}
        if seq is not None:
            params["seq"] = seq
        if ttl_ms:
            params["ttl_ms"] = int(ttl_ms)
        return self.request("workspace.report_metadata", params)


class EventStream:
    """A subscription connection that stays open and pushes events."""

    def __init__(self, path=None, timeout=1.0):
        self.path = path or socket_path()
        self.wire = _Wire(self.path, timeout=timeout)

    @property
    def sock(self):
        return self.wire.sock

    def subscribe(self, event_types):
        self.wire.send("events.subscribe", {"subscriptions": [{"type": name} for name in event_types]})
        while True:
            message = self.wire.read_message()
            if message is None or message.get("id") == REQUEST_ID:
                return _unwrap(message, "events.subscribe")

    def has_buffered(self):
        return self.wire.has_buffered()

    def read_message(self):
        return self.wire.read_message()

    def close(self):
        self.wire.close()
