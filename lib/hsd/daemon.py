"""The long-running watcher.

herdr startup hooks are one-shot, so this process is spawned detached and
supervises itself: it exits when the herdr server goes away, and refuses to
start a second copy for the same session.

Nothing tells us when a file on disk changes, so the poll is the primary clock
here rather than a safety net. Events only bring the pass forward — most
usefully pane.updated, which carries an agent changing state and so fires
around the moment an agent stops working and its numbers become worth reading.
"""

import select
import signal
import socket
import time

from .client import Client, EventStream, HerdrError

EVENTS = (
    "workspace.created",
    "workspace.closed",
    "workspace.focused",
    "workspace.updated",
    "workspace.moved",
    "worktree.created",
    "worktree.opened",
    "worktree.removed",
    "tab.created",
    "tab.closed",
    "tab.focused",
    "pane.created",
    "pane.closed",
    "pane.exited",
    "pane.updated",
    "pane.focused",
)
# Deliberately not workspace.metadata_updated: this plugin writes those, and
# subscribing would have every pass trigger the next one.
#
# Not pane.agent_status_changed either, tempting as it is: that subscription
# requires a pane_id, so it cannot be taken out session-wide, and a bad entry
# fails the whole events.subscribe call. pane.updated covers the same ground.
SUBSCRIBABLE_WITHOUT_ARGUMENTS = frozenset(
    (
        "workspace.created",
        "workspace.updated",
        "workspace.metadata_updated",
        "workspace.renamed",
        "workspace.moved",
        "workspace.closed",
        "workspace.focused",
        "worktree.created",
        "worktree.opened",
        "worktree.removed",
        "tab.created",
        "tab.closed",
        "tab.focused",
        "tab.renamed",
        "tab.moved",
        "pane.created",
        "pane.closed",
        "pane.updated",
        "pane.focused",
        "pane.moved",
        "pane.exited",
        "pane.agent_detected",
        "layout.updated",
    )
)

DEBOUNCE_SECONDS = 0.25


def connect_events(path=None, attempts=10, delay=0.5, timeout=1.0):
    """Open the event stream, retrying so a race with server startup survives."""
    last = None
    for attempt in range(attempts):
        try:
            return EventStream(path, timeout=timeout)
        except OSError as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
    raise last


class Watcher:
    def __init__(self, reporter_factory, socket_path=None, poll_interval=5.0, log=None):
        self.reporter_factory = reporter_factory
        self.socket_path = socket_path
        self.poll_interval = max(0.0, poll_interval)
        self.log = log or (lambda *_: None)
        self.running = True

    def stop(self, *_):
        self.running = False

    def install_signal_handlers(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):
                pass

    def run(self):
        events = connect_events(self.socket_path)
        reporter = self.reporter_factory(Client(self.socket_path))
        try:
            events.subscribe(EVENTS)
        except HerdrError as exc:
            self.log("subscribe failed: {}".format(exc))
            events.close()
            return 1

        self.log("watching {}".format(events.path))
        self._refresh(reporter)
        last_pass = time.time()
        due = None

        while self.running:
            now = time.time()
            timeouts = [1.0]
            if due is not None:
                timeouts.append(max(0.0, due - now))
            if self.poll_interval:
                timeouts.append(max(0.0, last_pass + self.poll_interval - now))
            readable, _, _ = select.select([events.sock], [], [], min(timeouts))

            if readable:
                if not self._drain(events):
                    self.log("herdr closed the event stream; exiting")
                    break
                due = time.time() + DEBOUNCE_SECONDS

            now = time.time()
            if due is not None and now >= due:
                due = None
                self._refresh(reporter)
                last_pass = now
            elif self.poll_interval and now - last_pass >= self.poll_interval:
                self._refresh(reporter)
                last_pass = now

        events.close()
        return 0

    def _drain(self, events):
        """Read the pending events. False means the server hung up."""
        while True:
            try:
                message = events.read_message()
            except socket.timeout:
                return True
            except (OSError, ValueError):
                return False
            if message is None:
                return False
            if not events.has_buffered():
                return True

    def _refresh(self, reporter):
        try:
            for workspace_id, tokens in reporter.refresh():
                self.log("{} -> {}".format(workspace_id, _describe(tokens)))
        except HerdrError as exc:
            self.log("refresh failed: {}".format(exc))
        except OSError as exc:
            self.log("connection lost: {}".format(exc))
            self.running = False
        except Exception as exc:  # never let one bad pass kill the watcher
            self.log("unexpected error: {!r}".format(exc))


def _describe(tokens):
    shown = [value for value in tokens.values() if value]
    return " ".join(shown) if shown else "(cleared)"
