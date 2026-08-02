"""Command line surface.

    watch     run the watcher in the foreground (what the daemon process does)
    start     spawn the watcher detached, unless one is already running
    stop      stop the watcher (--clear also takes the numbers off the sidebar)
    restart   stop then start
    status    report whether a watcher is running for this session
    once      do a single pass and exit
    stats     print the counts for a directory, for checking config changes
"""

import argparse
import os
import signal
import subprocess
import sys
import time

from .client import Client, HerdrError, socket_path
from .config import Config
from .daemon import Watcher
from .gitstat import GitError, diff_stat, repo_root
from .reporter import Reporter
from .state import Store

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENTRYPOINT = os.path.join(PLUGIN_ROOT, "bin", "herdr-space-diffstat")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="herdr-space-diffstat",
        description="Added, modified and deleted line counts under each herdr space.",
    )
    parser.add_argument("--config", help="path to a config file (overrides the usual lookup)")
    parser.add_argument("--socket", help="path to the herdr API socket")
    parser.add_argument("--verbose", action="store_true", help="log to stderr")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("watch", help="run the watcher in the foreground")
    sub.add_parser("start", help="spawn the watcher in the background")
    sub.add_parser("restart", help="restart the background watcher")
    sub.add_parser("status", help="show watcher status")
    sub.add_parser("once", help="report every space once and exit")

    stop = sub.add_parser("stop", help="stop the background watcher")
    stop.add_argument("--clear", action="store_true", help="also clear the tokens it reported")

    stats = sub.add_parser("stats", help="print the counts for a directory")
    stats.add_argument("path", nargs="?", default=".")
    return parser


def _logger(verbose):
    if not verbose:
        return lambda *_: None

    def log(message):
        sys.stderr.write("[space-diffstat] {}\n".format(message))
        sys.stderr.flush()

    return log


def _trim_log(path, limit=512 * 1024):
    """Keep the watcher log from growing without bound across restarts."""
    try:
        if os.path.getsize(path) > limit:
            os.remove(path)
    except OSError:
        pass


def _wait_for_exit(pid, timeout=5.0, interval=0.1):
    """Block until `pid` is gone. False if it outlived the timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(interval)
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def _pieces(args):
    config = Config.load(user_path=args.config)
    path = args.socket or socket_path()
    return config, path, Store(path)


def _reporter_factory(config, store):
    def factory(connection):
        return Reporter(connection, config, store)

    return factory


def cmd_watch(args):
    config, path, store = _pieces(args)
    log = _logger(args.verbose)

    lock = store.acquire_lock()
    if lock is None:
        log("another watcher already holds the lock for this session")
        return 0

    store.write_pid()
    watcher = Watcher(
        _reporter_factory(config, store),
        socket_path=path,
        poll_interval=config.float("poll-interval", 5.0),
        log=log,
    )
    watcher.install_signal_handlers()
    try:
        return watcher.run()
    except OSError as exc:
        log("could not reach herdr: {}".format(exc))
        return 1
    finally:
        if store.read_pid() == os.getpid():
            store.clear_pid()
        lock.close()


def cmd_start(args):
    _, path, store = _pieces(args)
    log = _logger(args.verbose)

    running = store.running_pid()
    if running:
        log("watcher already running (pid {})".format(running))
        return 0

    # Global flags come before the subcommand.
    command = [sys.executable, ENTRYPOINT, "--verbose"]
    if args.config:
        command += ["--config", args.config]
    if args.socket:
        command += ["--socket", args.socket]
    command.append("watch")

    log_path = store.log_path
    _trim_log(log_path)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write("--- started {} ---\n".format(time.strftime("%Y-%m-%d %H:%M:%S")))
        handle.flush()
        # Detach: herdr startup hooks are one-shot, so nothing supervises this.
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
            env=dict(os.environ, HERDR_SOCKET_PATH=path),
        )
    # The child writes the pid file itself, once it holds the session lock — so
    # a spawn that loses the race leaves the running watcher's pid intact.
    log("watcher started (pid {}), logging to {}".format(process.pid, log_path))
    return 0


def cmd_stop(args):
    config, path, store = _pieces(args)
    log = _logger(args.verbose)

    pid = store.running_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            log("stopped watcher (pid {})".format(pid))
        except OSError as exc:
            log("could not stop pid {}: {}".format(pid, exc))
    else:
        log("no watcher running")

    if pid and not _wait_for_exit(pid):
        # A watcher still in its select() loop would re-report what we clear.
        log("watcher {} did not exit in time".format(pid))
    store.clear_pid()

    if args.clear:
        store.load()  # the watcher owned the state file while it ran
        try:
            reporter = Reporter(Client(path), config, store)
            for workspace_id in reporter.clear_all():
                log("cleared {}".format(workspace_id))
        except (OSError, HerdrError) as exc:
            log("could not clear tokens: {}".format(exc))
            return 1
    return 0


def cmd_restart(args):
    args.clear = False
    cmd_stop(args)  # waits for the old watcher to exit
    return cmd_start(args)


def cmd_status(args):
    _, path, store = _pieces(args)
    pid = store.running_pid()
    print("socket:  {}".format(path))
    print("watcher: {}".format("running (pid {})".format(pid) if pid else "not running"))
    print("spaces:  {} reported".format(len(store.workspaces())))
    print("state:   {}".format(store.path))
    print("log:     {}".format(store.log_path))
    return 0


def cmd_once(args):
    config, path, store = _pieces(args)
    log = _logger(args.verbose)
    try:
        reporter = Reporter(Client(path), config, store)
        for workspace_id, tokens in reporter.refresh():
            log("{} -> {}".format(workspace_id, tokens))
    except (OSError, HerdrError) as exc:
        sys.stderr.write("herdr-space-diffstat: {}\n".format(exc))
        return 1
    return 0


def cmd_stats(args):
    config = Config.load(user_path=args.config)
    root = repo_root(os.path.abspath(os.path.expanduser(args.path)))
    if not root:
        sys.stderr.write("herdr-space-diffstat: not a git work tree: {}\n".format(args.path))
        return 1
    try:
        stat = diff_stat(
            root,
            include_untracked=config.bool("include-untracked", True),
            timeout=config.float("git-timeout", 10.0),
            untracked_max_bytes=config.int("untracked-max-bytes", 1048576),
            pair_modified=config.bool("pair-modified", True),
        )
    except GitError as exc:
        sys.stderr.write("herdr-space-diffstat: {}\n".format(exc))
        return 1
    reporter = Reporter(None, config, None, git=object())
    tokens = reporter.format(stat)
    print("repo:     {}".format(root))
    print("counts:   added {} modified {} deleted {}".format(stat.added, stat.modified, stat.deleted))
    for name in reporter.token_names():
        value = tokens.get(name)
        print("${:<14} {}".format(name, "(hidden)" if value is None else value))
    return 0


COMMANDS = {
    "watch": cmd_watch,
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "status": cmd_status,
    "once": cmd_once,
    "stats": cmd_stats,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return COMMANDS[args.command](args)
