"""On-disk state: what we last reported per space, and the watcher's lock.

Keyed by socket path, so several herdr sessions can run the plugin at once
without fighting over one file.

The location is deliberately *not* HERDR_PLUGIN_STATE_DIR: herdr only sets that
when it launches the command itself, so a watcher started by the startup hook
and a `stop` you run from a shell would look in different places, never see
each other's pid file, and leave two watchers running.
"""

import errno
import fcntl
import hashlib
import json
import os

STATE_DIR_ENV = "HERDR_SPACE_DIFFSTAT_STATE_DIR"


def state_dir():
    root = os.environ.get(STATE_DIR_ENV)
    if not root:
        xdg = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        root = os.path.join(xdg, "herdr-space-diffstat")
    root = os.path.expanduser(root)
    os.makedirs(root, exist_ok=True)
    return root


def _session_key(socket_path):
    return hashlib.sha1(socket_path.encode("utf-8")).hexdigest()[:12]


class Store:
    """Per-session record of the tokens we last reported."""

    def __init__(self, socket_path):
        key = _session_key(socket_path)
        root = state_dir()
        self.path = os.path.join(root, "spaces-{}.json".format(key))
        self.pid_path = os.path.join(root, "watcher-{}.pid".format(key))
        self.lock_path = os.path.join(root, "watcher-{}.lock".format(key))
        self.log_path = os.path.join(root, "watcher-{}.log".format(key))
        self.spaces = {}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.spaces = data.get("spaces", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.spaces = {}

    def save(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"spaces": self.spaces}, handle, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, workspace_id):
        return self.spaces.get(workspace_id)

    def remember(self, workspace_id, tokens, at):
        self.spaces[workspace_id] = {"tokens": tokens, "at": at}

    def forget(self, workspace_id):
        self.spaces.pop(workspace_id, None)

    def workspaces(self):
        return list(self.spaces)

    def clear(self):
        self.spaces = {}

    def prune(self, live_workspace_ids):
        for workspace_id in list(self.spaces):
            if workspace_id not in live_workspace_ids:
                del self.spaces[workspace_id]

    # -- watcher pid ------------------------------------------------------

    def write_pid(self, pid=None):
        with open(self.pid_path, "w", encoding="utf-8") as handle:
            handle.write(str(pid or os.getpid()))

    def read_pid(self):
        try:
            with open(self.pid_path, encoding="utf-8") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            return None

    def clear_pid(self):
        try:
            os.remove(self.pid_path)
        except OSError:
            pass

    def running_pid(self):
        """The watcher pid if a process with it is alive, else None."""
        pid = self.read_pid()
        if not pid:
            return None
        try:
            os.kill(pid, 0)
        except OSError as exc:
            if exc.errno == errno.EPERM:
                return pid
            return None
        return pid

    def acquire_lock(self):
        """Take the session's watcher lock, or return None if it is held."""
        handle = open(self.lock_path, "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        handle.write(str(os.getpid()))
        handle.flush()
        return handle
