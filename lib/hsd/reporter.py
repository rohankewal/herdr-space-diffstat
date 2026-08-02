"""One pass: look at every space, count its lines, report the tokens.

herdr renders workspace metadata tokens only where the user's
[ui.sidebar.spaces] rows ask for them, and a row whose tokens all have no value
disappears entirely — so a space that is not a git checkout needs no special
handling here beyond declining to set anything.
"""

import time

from .client import HerdrError
from .gitstat import GitError, diff_stat, repo_root

SOURCE = "herdr-space-diffstat"

FIELDS = ("added", "modified", "deleted")


class Reporter:
    def __init__(self, client, config, store, git=None):
        self.client = client
        self.config = config
        self.store = store
        # Injectable so the tests can run without a git checkout.
        self.git = git or _Git()
        self.prefix = config.option("token-prefix", "diff") or "diff"
        self.icons = {
            "added": config.option("added-icon", "+"),
            "modified": config.option("modified-icon", "~"),
            "deleted": config.option("deleted-icon", "-"),
        }
        self.gap = config.option("icon-gap", "")
        self.hide_zero = config.bool("hide-zero", True)
        self.include_untracked = config.bool("include-untracked", True)
        self.pair_modified = config.bool("pair-modified", True)
        self.timeout = config.float("git-timeout", 10.0)
        self.untracked_max_bytes = config.int("untracked-max-bytes", 1048576)
        self.ttl_ms = self._ttl_ms()
        # Re-report before the ttl runs out, even when nothing changed, or the
        # numbers would blink out on an idle repo.
        self.refresh_after = max(1.0, (self.ttl_ms / 1000.0) / 2.0)
        self._roots = {}

    def _ttl_ms(self):
        configured = self.config.int("ttl-ms", 0)
        if configured <= 0:
            poll = self.config.float("poll-interval", 5.0)
            configured = int(max(15.0, poll * 4) * 1000)
        return max(1000, min(configured, 86400000))

    def token_names(self):
        return tuple("{}_{}".format(self.prefix, field) for field in FIELDS)

    def format(self, stat):
        """The token values for a stat. None means "clear this token"."""
        tokens = {}
        for field, name in zip(FIELDS, self.token_names()):
            count = getattr(stat, field)
            if count == 0 and self.hide_zero:
                tokens[name] = None
            else:
                tokens[name] = "{}{}{}".format(self.icons[field], self.gap, count)
        return tokens

    def cleared(self):
        return {name: None for name in self.token_names()}

    def workspace_paths(self, snapshot):
        """Map each workspace to the directory that speaks for it.

        Workspaces carry no cwd of their own, so it comes from a pane: the
        focused pane of the active tab where there is one, any pane in the
        space otherwise.
        """
        panes_by_workspace = {}
        for pane in snapshot.get("panes", []):
            workspace_id = pane.get("workspace_id")
            if workspace_id:
                panes_by_workspace.setdefault(workspace_id, []).append(pane)

        paths = {}
        for workspace in snapshot.get("workspaces", []):
            workspace_id = workspace.get("workspace_id")
            if not workspace_id:
                continue
            panes = panes_by_workspace.get(workspace_id, [])
            paths[workspace_id] = _pick_path(panes, workspace.get("active_tab_id"))
        return paths

    def stats_for(self, paths):
        """Stat every distinct checkout once, however many spaces share it."""
        roots = {}
        for workspace_id, path in paths.items():
            roots[workspace_id] = self._root_for(path)

        by_root = {}
        for root in set(root for root in roots.values() if root):
            try:
                by_root[root] = self.git.diff_stat(
                    root,
                    include_untracked=self.include_untracked,
                    timeout=self.timeout,
                    untracked_max_bytes=self.untracked_max_bytes,
                    pair_modified=self.pair_modified,
                )
            except GitError:
                # A checkout mid-rebase, or git being slow. Leave the last
                # numbers up; the ttl clears them if this keeps failing.
                by_root[root] = None
        stats = {
            workspace_id: (by_root.get(root) if root else None)
            for workspace_id, root in roots.items()
        }
        return stats, roots

    def _root_for(self, path):
        if not path:
            return None
        if path not in self._roots:
            if len(self._roots) > 256:
                self._roots.clear()
            self._roots[path] = self.git.repo_root(path, timeout=self.timeout)
        return self._roots[path]

    def refresh(self):
        """Report every space that needs it. Yields (workspace_id, tokens)."""
        snapshot = self.client.snapshot()
        paths = self.workspace_paths(snapshot)
        stats, roots = self.stats_for(paths)

        now = time.time()
        reported = []
        for workspace_id in sorted(paths):
            stat = stats.get(workspace_id)
            if stat is None and roots.get(workspace_id):
                continue  # git failed for this checkout; keep what is on screen
            tokens = self.cleared() if roots.get(workspace_id) is None else self.format(stat)
            if not self._should_report(workspace_id, tokens, now):
                continue
            try:
                self.client.report_workspace_metadata(
                    workspace_id,
                    SOURCE,
                    tokens,
                    seq=int(now * 1000),
                    ttl_ms=self.ttl_ms,
                )
            except HerdrError:
                # The space closed between the snapshot and now.
                self.store.forget(workspace_id)
                continue
            self.store.remember(workspace_id, tokens, now)
            reported.append((workspace_id, tokens))

        self.store.prune(set(paths))
        self.store.save()
        return reported

    def _should_report(self, workspace_id, tokens, now):
        previous = self.store.get(workspace_id)
        if previous is None:
            # Nothing on screen and nothing to say: stay quiet rather than
            # writing empty tokens to every non-git space on every pass.
            return any(value is not None for value in tokens.values())
        if previous.get("tokens") != tokens:
            return True
        return now - previous.get("at", 0) >= self.refresh_after

    def clear_all(self):
        """Take every token this plugin set back off the sidebar."""
        cleared = []
        for workspace_id in sorted(self.store.workspaces()):
            try:
                self.client.report_workspace_metadata(
                    workspace_id, SOURCE, self.cleared(), seq=int(time.time() * 1000)
                )
            except HerdrError:
                pass
            cleared.append(workspace_id)
        self.store.clear()
        self.store.save()
        return cleared


def _pick_path(panes, active_tab_id):
    """The pane whose directory represents the space."""
    if not panes:
        return None
    in_active_tab = [pane for pane in panes if pane.get("tab_id") == active_tab_id]
    for candidates in (in_active_tab, panes):
        for pane in candidates:
            if pane.get("focused") and _path_of(pane):
                return _path_of(pane)
        for pane in candidates:
            if _path_of(pane):
                return _path_of(pane)
    return None


def _path_of(pane):
    return pane.get("cwd") or pane.get("foreground_cwd")


class _Git:
    """The real git, behind the seam the tests replace."""

    def repo_root(self, path, timeout=10.0):
        return repo_root(path, timeout=timeout)

    def diff_stat(
        self,
        root,
        include_untracked=True,
        timeout=10.0,
        untracked_max_bytes=1048576,
        pair_modified=True,
    ):
        return diff_stat(
            root,
            include_untracked=include_untracked,
            timeout=timeout,
            untracked_max_bytes=untracked_max_bytes,
            pair_modified=pair_modified,
        )
