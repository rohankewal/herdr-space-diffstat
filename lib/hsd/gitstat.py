"""Counting added, modified and deleted lines in a checkout.

Everything here measures `git diff HEAD` — staged plus unstaged work, which
resets to nothing when you commit — optionally plus untracked files.

git itself only reports additions and deletions; there is no such thing as a
"modified line" in a diff. This module derives one: within a single hunk, an
added line and a deleted line are assumed to be the same line rewritten, so a
hunk of 3 deletions and 5 additions counts as 3 modified and 2 added. That is a
heuristic, and it is why these numbers deliberately do not add up to the ones
`git diff --stat` prints.
"""

import collections
import os
import subprocess
import time

DiffStat = collections.namedtuple("DiffStat", "added modified deleted")

EMPTY = DiffStat(0, 0, 0)

# A repo with no commits has no HEAD to diff against; git's canonical empty
# tree stands in for it.
EMPTY_TREE_SHA1 = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Flags on every invocation. --no-optional-locks keeps a background poller from
# taking the index lock out from under the shell you are actually working in.
GIT = ("git", "--no-optional-locks")

# How much of a file to sniff before deciding it is binary.
_SNIFF_BYTES = 8192
_CHUNK = 65536


class GitError(Exception):
    """git could not answer — not a repo, not installed, or it timed out."""


def _run(args, cwd=None, timeout=10.0):
    try:
        completed = subprocess.run(
            list(GIT) + list(args),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(str(exc))
    if completed.returncode != 0:
        raise GitError("git {} exited {}".format(args[0], completed.returncode))
    return completed.stdout.decode("utf-8", "replace")


def repo_root(path, timeout=10.0):
    """The checkout root containing `path`, or None if it is not in a work tree.

    For a linked worktree this is the worktree's own root, which is what we
    want: two herdr spaces on two worktrees of one repo get their own numbers.
    """
    if not path or not os.path.isdir(path):
        return None
    try:
        out = _run(("rev-parse", "--show-toplevel"), cwd=path, timeout=timeout)
    except GitError:
        return None
    root = out.strip()
    return root or None


def _diff_base(root, timeout):
    """What to diff against: HEAD, or the empty tree in a repo with no commits."""
    try:
        _run(("rev-parse", "--verify", "--quiet", "HEAD"), cwd=root, timeout=timeout)
        return "HEAD"
    except GitError:
        pass
    try:
        return _run(("hash-object", "-t", "tree", os.devnull), cwd=root, timeout=timeout).strip()
    except GitError:
        return EMPTY_TREE_SHA1


def parse_patch(lines, pair=True):
    """Fold a unified-diff-with-zero-context stream into a DiffStat.

    Takes an iterable of str lines. Binary files contribute nothing: git emits
    a "Binary files ... differ" notice for them and no +/- lines at all.

    With `pair` off, no line is ever called modified and the two remaining
    counts are exactly git's own insertions and deletions.
    """
    added = modified = deleted = 0
    hunk_adds = hunk_dels = 0
    in_hunk = False

    def close_hunk():
        nonlocal added, modified, deleted, hunk_adds, hunk_dels
        paired = min(hunk_adds, hunk_dels) if pair else 0
        modified += paired
        added += hunk_adds - paired
        deleted += hunk_dels - paired
        hunk_adds = hunk_dels = 0

    for line in lines:
        if line.startswith("@@"):
            if in_hunk:
                close_hunk()
            in_hunk = True
            continue
        if not in_hunk:
            # File headers, mode changes, rename notices, "index abc..def".
            continue
        if line.startswith("+"):
            hunk_adds += 1
        elif line.startswith("-"):
            hunk_dels += 1
        elif line.startswith("\\"):
            # "\ No newline at end of file" annotates the line above it.
            continue
        else:
            # Only reachable at the next file's header; -U0 emits no context.
            close_hunk()
            in_hunk = False
    if in_hunk:
        close_hunk()
    return DiffStat(added, modified, deleted)


def _tracked_stat(root, timeout, pair=True):
    """Stream `git diff HEAD -U0` and fold it, so a huge diff stays bounded."""
    base = _diff_base(root, timeout)
    args = list(GIT) + [
        "diff",
        base,
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=dirty",
    ]
    try:
        process = subprocess.Popen(
            args,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise GitError(str(exc))

    deadline = time.time() + timeout

    def guarded():
        for index, raw in enumerate(process.stdout):
            # Checking the clock every line would cost more than the parse.
            if index % 4096 == 0 and time.time() > deadline:
                raise GitError("git diff timed out in {}".format(root))
            yield raw.decode("utf-8", "replace")

    try:
        return parse_patch(guarded(), pair=pair)
    finally:
        try:
            process.stdout.close()
        except OSError:
            pass
        if process.poll() is None:
            process.kill()
        process.wait()


def count_lines(path, max_bytes=1048576):
    """Lines in a file, or None if it is binary, unreadable or too large.

    A final line with no trailing newline still counts, matching how git counts
    additions for a new file.
    """
    try:
        if os.path.getsize(path) > max_bytes:
            return None
        with open(path, "rb") as handle:
            first = handle.read(_SNIFF_BYTES)
            if b"\x00" in first:
                return None
            lines = first.count(b"\n")
            last = first[-1:] if first else b"\n"
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                lines += chunk.count(b"\n")
                last = chunk[-1:]
            if last not in (b"\n", b""):
                lines += 1
            return lines
    except OSError:
        return None


def _untracked_added(root, timeout, max_bytes):
    """Lines in files git does not track yet, counted as additions."""
    try:
        out = _run(("ls-files", "--others", "--exclude-standard", "-z"), cwd=root, timeout=timeout)
    except GitError:
        return 0
    total = 0
    for name in out.split("\0"):
        if not name:
            continue
        counted = count_lines(os.path.join(root, name), max_bytes=max_bytes)
        if counted:
            total += counted
    return total


def diff_stat(
    root,
    include_untracked=True,
    timeout=10.0,
    untracked_max_bytes=1048576,
    pair_modified=True,
):
    """Added, modified and deleted lines for a checkout."""
    stat = _tracked_stat(root, timeout, pair=pair_modified)
    if include_untracked:
        extra = _untracked_added(root, timeout, untracked_max_bytes)
        if extra:
            stat = DiffStat(stat.added + extra, stat.modified, stat.deleted)
    return stat
