"""Unit tests. Run with: python3 -m unittest discover -s test"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from hsd import gitstat  # noqa: E402
from hsd.config import Config, parse_yaml  # noqa: E402
from hsd.gitstat import DiffStat, GitError, count_lines, parse_patch  # noqa: E402
from hsd.reporter import Reporter, _pick_path  # noqa: E402
from hsd.state import Store  # noqa: E402

DEFAULTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "defaults.yml"
)

HAS_GIT = shutil.which("git") is not None


def config(**overrides):
    """A Config backed by the real defaults, with config-section overrides."""
    base = Config.load(default_path=DEFAULTS, user_path=False)
    base.user = {"config": {k.replace("_", "-"): str(v) for k, v in overrides.items()}}
    return base


def patch(*lines):
    return list(lines)


class ParsePatchTest(unittest.TestCase):
    def test_empty_diff(self):
        self.assertEqual(parse_patch([]), DiffStat(0, 0, 0))

    def test_pairs_within_a_hunk(self):
        # One line rewritten is one modification, not an add and a delete.
        stat = parse_patch(patch("@@ -2 +2 @@", "-old", "+new"))
        self.assertEqual(stat, DiffStat(0, 1, 0))

    def test_surplus_additions_are_additions(self):
        stat = parse_patch(patch("@@ -5 +5,3 @@", "-e", "+x", "+y", "+z"))
        self.assertEqual(stat, DiffStat(2, 1, 0))

    def test_surplus_deletions_are_deletions(self):
        stat = parse_patch(patch("@@ -5,3 +5 @@", "-a", "-b", "-c", "+x"))
        self.assertEqual(stat, DiffStat(0, 1, 2))

    def test_hunks_are_paired_independently(self):
        # Two separate one-line rewrites, not one add plus one delete each.
        stat = parse_patch(patch("@@ -1 +1 @@", "-a", "+A", "@@ -9 +9 @@", "-b", "+B"))
        self.assertEqual(stat, DiffStat(0, 2, 0))

    def test_pairing_does_not_cross_files(self):
        stat = parse_patch(
            patch(
                "diff --git a/one b/one",
                "--- a/one",
                "+++ b/one",
                "@@ -1 +0,0 @@",
                "-gone",
                "diff --git a/two b/two",
                "--- a/two",
                "+++ b/two",
                "@@ -0,0 +1 @@",
                "+fresh",
            )
        )
        self.assertEqual(stat, DiffStat(1, 0, 1))

    def test_pairing_off_gives_gits_own_counts(self):
        stat = parse_patch(patch("@@ -5,3 +5,5 @@", "-a", "-b", "-c", "+v", "+w", "+x", "+y", "+z"), pair=False)
        self.assertEqual(stat, DiffStat(5, 0, 3))

    def test_ignores_file_headers(self):
        # The --- / +++ header lines must not be counted as a delete and an add.
        stat = parse_patch(
            patch(
                "diff --git a/f b/f",
                "index 9405325..99e81bf 100644",
                "--- a/f",
                "+++ b/f",
                "@@ -1 +1 @@",
                "-a",
                "+b",
            )
        )
        self.assertEqual(stat, DiffStat(0, 1, 0))

    def test_ignores_no_newline_marker(self):
        stat = parse_patch(patch("@@ -1 +1 @@", "-a", "\\ No newline at end of file", "+b"))
        self.assertEqual(stat, DiffStat(0, 1, 0))

    def test_binary_files_contribute_nothing(self):
        stat = parse_patch(
            patch(
                "diff --git a/b.bin b/b.bin",
                "new file mode 100644",
                "Binary files /dev/null and b/b.bin differ",
            )
        )
        self.assertEqual(stat, DiffStat(0, 0, 0))

    def test_rename_without_content_change(self):
        stat = parse_patch(
            patch(
                "diff --git a/old b/new",
                "similarity index 100%",
                "rename from old",
                "rename to new",
            )
        )
        self.assertEqual(stat, DiffStat(0, 0, 0))


class CountLinesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_counts_trailing_newline(self):
        self.assertEqual(count_lines(self.write("a", b"one\ntwo\n")), 2)

    def test_counts_final_line_without_newline(self):
        self.assertEqual(count_lines(self.write("b", b"one\ntwo")), 2)

    def test_empty_file_is_zero(self):
        self.assertEqual(count_lines(self.write("c", b"")), 0)

    def test_binary_is_skipped(self):
        self.assertIsNone(count_lines(self.write("d", b"text\x00more\n")))

    def test_oversized_is_skipped(self):
        self.assertIsNone(count_lines(self.write("e", b"x\n" * 100), max_bytes=10))

    def test_missing_file_is_skipped(self):
        self.assertIsNone(count_lines(os.path.join(self.dir, "nope")))


@unittest.skipUnless(HAS_GIT, "git is not installed")
class DiffStatAgainstRealGitTest(unittest.TestCase):
    """The parser is only worth anything if it agrees with real git output."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.git("init", "-q", ".")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")

    def git(self, *args):
        subprocess.run(
            ("git",) + args,
            cwd=self.dir,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "wip")

    def stat(self, **kwargs):
        kwargs.setdefault("include_untracked", False)
        return gitstat.diff_stat(self.dir, **kwargs)

    def test_clean_checkout_is_all_zero(self):
        self.write("f.txt", "a\nb\n")
        self.commit()
        self.assertEqual(self.stat(), DiffStat(0, 0, 0))

    def test_repo_without_commits_counts_staged_files(self):
        self.write("f.txt", "a\nb\nc\n")
        self.git("add", "-A")
        self.assertEqual(self.stat(), DiffStat(3, 0, 0))

    def test_mixed_edit(self):
        self.write("f.txt", "a\nb\nc\nd\ne\n")
        self.commit()
        # b -> B2 is a modification; e -> X, Y is a modification plus an add.
        self.write("f.txt", "a\nB2\nc\nd\nX\nY\n")
        self.assertEqual(self.stat(), DiffStat(1, 2, 0))

    def test_counts_staged_and_unstaged_together(self):
        self.write("f.txt", "a\nb\n")
        self.commit()
        self.write("f.txt", "a\nb\nstaged\n")
        self.git("add", "f.txt")
        self.write("g.txt", "unstaged\n")
        self.git("add", "g.txt")
        self.assertEqual(self.stat(), DiffStat(2, 0, 0))

    def test_unpaired_totals_match_git_shortstat(self):
        self.write("f.txt", "a\nb\nc\nd\ne\n")
        self.commit()
        self.write("f.txt", "a\nB2\nc\nd\nX\nY\n")
        stat = self.stat(pair_modified=False)
        self.assertEqual(stat, DiffStat(3, 0, 2))  # git: 3 insertions, 2 deletions

    def test_deleted_file(self):
        self.write("f.txt", "a\nb\nc\n")
        self.commit()
        os.remove(os.path.join(self.dir, "f.txt"))
        self.assertEqual(self.stat(), DiffStat(0, 0, 3))

    def test_binary_file_is_ignored(self):
        self.write("f.txt", "a\n")
        self.commit()
        with open(os.path.join(self.dir, "b.bin"), "wb") as handle:
            handle.write(b"\x00\x01\x02\x03")
        self.git("add", "b.bin")
        self.assertEqual(self.stat(), DiffStat(0, 0, 0))

    def test_untracked_counts_as_added_when_enabled(self):
        self.write("f.txt", "a\n")
        self.commit()
        self.write("new.txt", "one\ntwo\nthree\n")
        self.assertEqual(self.stat(include_untracked=False), DiffStat(0, 0, 0))
        self.assertEqual(self.stat(include_untracked=True), DiffStat(3, 0, 0))

    def test_ignored_files_do_not_count(self):
        self.write(".gitignore", "*.log\n")
        self.commit()
        self.write("noise.log", "spam\nspam\n")
        self.assertEqual(self.stat(include_untracked=True), DiffStat(0, 0, 0))

    def test_committing_resets_the_counts(self):
        self.write("f.txt", "a\n")
        self.commit()
        self.write("f.txt", "a\nb\n")
        self.assertEqual(self.stat(), DiffStat(1, 0, 0))
        self.commit()
        self.assertEqual(self.stat(), DiffStat(0, 0, 0))

    def test_repo_root_finds_the_checkout_from_a_subdirectory(self):
        os.makedirs(os.path.join(self.dir, "deep", "deeper"))
        found = gitstat.repo_root(os.path.join(self.dir, "deep", "deeper"))
        self.assertEqual(os.path.realpath(found), os.path.realpath(self.dir))

    def test_repo_root_of_a_plain_directory_is_none(self):
        plain = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, plain, True)
        self.assertIsNone(gitstat.repo_root(plain))


class FakeClient:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.reports = []
        self.fail_for = set()

    def snapshot(self):
        return self._snapshot

    def report_workspace_metadata(self, workspace_id, source, tokens, seq=None, ttl_ms=None):
        if workspace_id in self.fail_for:
            from hsd.client import HerdrError

            raise HerdrError("workspace_not_found: gone")
        self.reports.append((workspace_id, dict(tokens), ttl_ms))
        return {}


class FakeGit:
    def __init__(self, roots, stats, fail=()):
        self.roots = roots
        self.stats = stats
        self.fail = set(fail)
        self.calls = []

    def repo_root(self, path, timeout=10.0):
        return self.roots.get(path)

    def diff_stat(self, root, **kwargs):
        self.calls.append(root)
        if root in self.fail:
            raise GitError("git exploded")
        return self.stats[root]


def snapshot(workspaces, panes):
    return {"workspaces": workspaces, "panes": panes}


class PickPathTest(unittest.TestCase):
    def test_prefers_the_focused_pane_of_the_active_tab(self):
        panes = [
            {"tab_id": "w:t1", "cwd": "/other", "focused": False},
            {"tab_id": "w:t2", "cwd": "/wrong", "focused": False},
            {"tab_id": "w:t2", "cwd": "/right", "focused": True},
        ]
        self.assertEqual(_pick_path(panes, "w:t2"), "/right")

    def test_falls_back_to_any_pane_in_the_active_tab(self):
        panes = [{"tab_id": "w:t2", "cwd": "/here", "focused": False}]
        self.assertEqual(_pick_path(panes, "w:t2"), "/here")

    def test_falls_back_to_any_pane_at_all(self):
        panes = [{"tab_id": "w:t9", "cwd": "/elsewhere", "focused": False}]
        self.assertEqual(_pick_path(panes, "w:t2"), "/elsewhere")

    def test_falls_back_to_foreground_cwd(self):
        panes = [{"tab_id": "w:t1", "foreground_cwd": "/fg"}]
        self.assertEqual(_pick_path(panes, "w:t1"), "/fg")

    def test_no_panes_means_no_path(self):
        self.assertIsNone(_pick_path([], "w:t1"))


class ReporterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.environ["HERDR_SPACE_DIFFSTAT_STATE_DIR"] = self.dir
        self.addCleanup(os.environ.pop, "HERDR_SPACE_DIFFSTAT_STATE_DIR", None)
        self.store = Store("/tmp/fake.sock")

    def reporter(self, client, git, **overrides):
        return Reporter(client, config(**overrides), self.store, git=git)

    def one_space(self, cwd="/repo"):
        return snapshot(
            [{"workspace_id": "w1", "active_tab_id": "w1:t1"}],
            [{"workspace_id": "w1", "tab_id": "w1:t1", "cwd": cwd, "focused": True}],
        )

    def test_formats_tokens_with_icons(self):
        reporter = self.reporter(None, None)
        tokens = reporter.format(DiffStat(38, 12, 4))
        self.assertEqual(
            tokens,
            {"diff_added": "+38", "diff_modified": "~12", "diff_deleted": "-4"},
        )

    def test_hide_zero_clears_the_token(self):
        reporter = self.reporter(None, None)
        self.assertIsNone(reporter.format(DiffStat(5, 0, 0))["diff_modified"])

    def test_hide_zero_off_keeps_the_zero(self):
        reporter = self.reporter(None, None, hide_zero="false")
        self.assertEqual(reporter.format(DiffStat(5, 0, 0))["diff_modified"], "~0")

    def test_icons_and_gap_are_configurable(self):
        reporter = self.reporter(None, None, added_icon="", icon_gap=" ")
        self.assertEqual(reporter.format(DiffStat(3, 0, 0))["diff_added"], " 3")

    def test_token_prefix_is_configurable(self):
        reporter = self.reporter(None, None, token_prefix="lines")
        self.assertEqual(
            reporter.token_names(), ("lines_added", "lines_modified", "lines_deleted")
        )

    def test_reports_a_git_space(self):
        client = FakeClient(self.one_space())
        git = FakeGit({"/repo": "/repo"}, {"/repo": DiffStat(38, 12, 4)})
        reported = self.reporter(client, git).refresh()

        self.assertEqual([workspace for workspace, _ in reported], ["w1"])
        workspace_id, tokens, ttl = client.reports[0]
        self.assertEqual(workspace_id, "w1")
        self.assertEqual(tokens["diff_added"], "+38")
        self.assertEqual(tokens["diff_modified"], "~12")
        self.assertEqual(tokens["diff_deleted"], "-4")
        self.assertGreater(ttl, 0)

    def test_stays_silent_for_a_space_that_is_not_a_repo(self):
        # Nothing was ever put on screen, so there is nothing to take off it.
        client = FakeClient(self.one_space("/plain"))
        git = FakeGit({"/plain": None}, {})
        self.assertEqual(self.reporter(client, git).refresh(), [])
        self.assertEqual(client.reports, [])

    def test_clears_when_a_space_stops_being_a_repo(self):
        client = FakeClient(self.one_space())
        git = FakeGit({"/repo": "/repo"}, {"/repo": DiffStat(1, 0, 0)})
        self.reporter(client, git).refresh()

        gone = FakeClient(self.one_space("/plain"))
        self.reporter(gone, FakeGit({"/plain": None}, {})).refresh()
        self.assertEqual(
            gone.reports[0][1],
            {"diff_added": None, "diff_modified": None, "diff_deleted": None},
        )

    def test_unchanged_counts_are_not_reported_again(self):
        client = FakeClient(self.one_space())
        git = FakeGit({"/repo": "/repo"}, {"/repo": DiffStat(1, 2, 3)})
        self.reporter(client, git).refresh()
        self.reporter(client, git).refresh()
        self.assertEqual(len(client.reports), 1)

    def test_changed_counts_are_reported(self):
        client = FakeClient(self.one_space())
        git = FakeGit({"/repo": "/repo"}, {"/repo": DiffStat(1, 2, 3)})
        self.reporter(client, git).refresh()
        git.stats["/repo"] = DiffStat(1, 2, 4)
        self.reporter(client, git).refresh()
        self.assertEqual(len(client.reports), 2)
        self.assertEqual(client.reports[1][1]["diff_deleted"], "-4")

    def test_stale_reports_are_refreshed_before_the_ttl_expires(self):
        client = FakeClient(self.one_space())
        git = FakeGit({"/repo": "/repo"}, {"/repo": DiffStat(1, 2, 3)})
        self.reporter(client, git).refresh()
        # Pretend the last report was long enough ago that herdr would drop it.
        self.store.spaces["w1"]["at"] = 0
        self.reporter(client, git).refresh()
        self.assertEqual(len(client.reports), 2)

    def test_pair_modified_reaches_git(self):
        class Recording(FakeGit):
            def diff_stat(self, root, **kwargs):
                self.kwargs = kwargs
                return FakeGit.diff_stat(self, root, **kwargs)

        client = FakeClient(self.one_space())
        git = Recording({"/repo": "/repo"}, {"/repo": DiffStat(1, 0, 0)})
        self.reporter(client, git, pair_modified="false").refresh()
        self.assertIs(git.kwargs["pair_modified"], False)

    def test_one_git_call_for_two_spaces_on_one_checkout(self):
        client = FakeClient(
            snapshot(
                [
                    {"workspace_id": "w1", "active_tab_id": "w1:t1"},
                    {"workspace_id": "w2", "active_tab_id": "w2:t1"},
                ],
                [
                    {"workspace_id": "w1", "tab_id": "w1:t1", "cwd": "/repo"},
                    {"workspace_id": "w2", "tab_id": "w2:t1", "cwd": "/repo/sub"},
                ],
            )
        )
        git = FakeGit({"/repo": "/repo", "/repo/sub": "/repo"}, {"/repo": DiffStat(9, 0, 0)})
        self.reporter(client, git).refresh()
        self.assertEqual(git.calls, ["/repo"])
        self.assertEqual(len(client.reports), 2)

    def test_a_failing_git_leaves_the_last_numbers_alone(self):
        client = FakeClient(self.one_space())
        git = FakeGit({"/repo": "/repo"}, {"/repo": DiffStat(1, 0, 0)})
        self.reporter(client, git).refresh()

        broken = FakeGit({"/repo": "/repo"}, {}, fail=["/repo"])
        self.assertEqual(self.reporter(client, broken).refresh(), [])
        self.assertEqual(len(client.reports), 1)

    def test_a_closed_space_is_forgotten_rather_than_retried(self):
        client = FakeClient(self.one_space())
        client.fail_for.add("w1")
        git = FakeGit({"/repo": "/repo"}, {"/repo": DiffStat(1, 0, 0)})
        self.assertEqual(self.reporter(client, git).refresh(), [])
        self.assertEqual(self.store.get("w1"), None)

    def test_closed_spaces_are_pruned_from_state(self):
        client = FakeClient(self.one_space())
        git = FakeGit({"/repo": "/repo"}, {"/repo": DiffStat(1, 0, 0)})
        self.reporter(client, git).refresh()
        self.assertEqual(self.store.workspaces(), ["w1"])

        empty = FakeClient(snapshot([], []))
        self.reporter(empty, git).refresh()
        self.assertEqual(self.store.workspaces(), [])

    def test_clear_all_takes_every_token_off(self):
        client = FakeClient(self.one_space())
        git = FakeGit({"/repo": "/repo"}, {"/repo": DiffStat(1, 0, 0)})
        reporter = self.reporter(client, git)
        reporter.refresh()
        self.assertEqual(reporter.clear_all(), ["w1"])
        self.assertEqual(
            client.reports[-1][1],
            {"diff_added": None, "diff_modified": None, "diff_deleted": None},
        )
        self.assertEqual(self.store.workspaces(), [])

    def test_ttl_is_derived_from_the_poll_interval(self):
        reporter = self.reporter(None, None, poll_interval=30)
        self.assertEqual(reporter.ttl_ms, 120000)

    def test_ttl_has_a_floor_for_fast_polls(self):
        reporter = self.reporter(None, None, poll_interval=1)
        self.assertEqual(reporter.ttl_ms, 15000)

    def test_explicit_ttl_wins(self):
        reporter = self.reporter(None, None, ttl_ms=60000, poll_interval=1)
        self.assertEqual(reporter.ttl_ms, 60000)


class EventsTest(unittest.TestCase):
    def test_every_subscription_works_without_arguments(self):
        # One entry needing a pane_id fails the whole events.subscribe call and
        # the watcher exits before its first pass, so this is worth guarding.
        from hsd.daemon import EVENTS, SUBSCRIBABLE_WITHOUT_ARGUMENTS

        self.assertEqual(set(EVENTS) - SUBSCRIBABLE_WITHOUT_ARGUMENTS, set())

    def test_does_not_subscribe_to_its_own_writes(self):
        from hsd.daemon import EVENTS

        self.assertNotIn("workspace.metadata_updated", EVENTS)


class ConfigTest(unittest.TestCase):
    def test_sections_and_quotes(self):
        parsed = parse_yaml('config:\n  added-icon: "+"\n  hide-zero: true\n')
        self.assertEqual(parsed["config"]["added-icon"], "+")
        self.assertEqual(parsed["config"]["hide-zero"], "true")

    def test_strips_trailing_comment_but_keeps_hash_icon(self):
        # A comment only starts at " #", so an icon that is itself "#" survives.
        parsed = parse_yaml("config:\n  added-icon: #\n  modified-icon: ~ # tilde\n")
        self.assertEqual(parsed["config"]["added-icon"], "#")
        self.assertEqual(parsed["config"]["modified-icon"], "~")

    def test_empty_string_survives_as_a_value(self):
        # icon-gap: "" has to mean "no gap", not "fall back to the default".
        base = Config(user={"config": {"icon-gap": ""}}, defaults={"config": {"icon-gap": " "}})
        self.assertEqual(base.option("icon-gap", "?"), "")

    def test_defaults_file_parses(self):
        loaded = Config.load(default_path=DEFAULTS, user_path=False)
        self.assertEqual(loaded.option("added-icon"), "+")
        self.assertTrue(loaded.bool("hide-zero"))
        self.assertEqual(loaded.float("poll-interval"), 5.0)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.environ["HERDR_SPACE_DIFFSTAT_STATE_DIR"] = self.dir
        self.addCleanup(os.environ.pop, "HERDR_SPACE_DIFFSTAT_STATE_DIR", None)

    def test_round_trips_reported_tokens(self):
        store = Store("/tmp/a.sock")
        store.remember("w1", {"diff_added": "+1"}, 123.0)
        store.save()
        self.assertEqual(Store("/tmp/a.sock").get("w1"), {"tokens": {"diff_added": "+1"}, "at": 123.0})

    def test_sessions_do_not_share_state(self):
        first = Store("/tmp/a.sock")
        first.remember("w1", {"diff_added": "+1"}, 1.0)
        first.save()
        self.assertIsNone(Store("/tmp/b.sock").get("w1"))

    def test_no_watcher_pid_when_none_written(self):
        self.assertIsNone(Store("/tmp/c.sock").running_pid())

    def test_running_pid_sees_this_process(self):
        store = Store("/tmp/d.sock")
        store.write_pid()
        self.assertEqual(store.running_pid(), os.getpid())
        store.clear_pid()
        self.assertIsNone(store.running_pid())


if __name__ == "__main__":
    unittest.main()
