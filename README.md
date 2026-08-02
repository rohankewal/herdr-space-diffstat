# herdr space diffstat

Line counts under every [herdr](https://herdr.dev) space. Each space that sits
in a git checkout gets a row of its own showing how many lines have been added,
modified and deleted — three counts, three colours, right under the space name.

```
 ●  open_source
    main · ↑2
    +38  ~12  -4
```

Spaces that are not git checkouts show nothing at all: herdr hides a sidebar
row when none of its tokens have a value, so there is no placeholder and no
blank line to look at.

## What the numbers mean

The counts are **`git diff HEAD`** — everything staged and unstaged — plus the
lines in untracked files. They answer "how much uncommitted work is sitting in
this space", so they drop back to nothing every time you commit.

git has no concept of a modified line; a diff only has additions and deletions.
This plugin derives one: within a single hunk, an added line and a deleted line
are taken to be the same line rewritten, so a hunk with 3 deletions and 5
additions counts as **3 modified and 2 added**.

That is a heuristic, and it has one consequence worth knowing up front: these
numbers deliberately do not match `git diff --stat`. Where git says
`5 insertions(+), 3 deletions(-)`, this says `+2 ~3`. Both describe the same
diff — git counts line events, this counts lines that changed. If you would
rather have git's own arithmetic, set `pair-modified: false` and leave
`$diff_modified` out of your rows: the two remaining counts are then exactly
git's insertions and deletions.

## Requirements

- herdr 0.7.0 or newer
- git
- Python 3.8+ (macOS and most Linux distributions already have it)

## Install

```sh
herdr plugin install rohankewal/herdr-space-diffstat
```

Or from a local checkout:

```sh
git clone https://github.com/rohankewal/herdr-space-diffstat.git
herdr plugin link /path/to/herdr-space-diffstat
```

Then tell herdr where to put the numbers, in `~/.config/herdr/config.toml`:

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["branch", "git_status"],
  [
    { token = "$diff_added",    fg = "#a6e3a1" },
    { token = "$diff_modified", fg = "#f9e2af" },
    { token = "$diff_deleted",  fg = "#f38ba8" },
  ],
]
```

Apply both:

```sh
herdr server reload-config
herdr plugin action invoke herdr-space-diffstat.restart
```

Nothing appears until that `rows` block names the tokens — the plugin only
reports values, herdr decides where they go. To remove it:

```sh
herdr plugin action invoke herdr-space-diffstat.stop   # clears the numbers
herdr plugin unlink herdr-space-diffstat
```

## The tokens

| Token            | Value                                        |
| ---------------- | -------------------------------------------- |
| `$diff_added`    | Lines added, including untracked files       |
| `$diff_modified` | Lines rewritten in place (see above)         |
| `$diff_deleted`  | Lines removed                                |

Each is reported only when it is nonzero, so a clean checkout leaves the whole
row empty and herdr drops it. Rename them with `token-prefix` if `$diff_*`
collides with something else you report.

Put them wherever you like — beside the branch rather than under it, or in the
agent rows. They are ordinary herdr metadata tokens; this plugin has no opinion
about placement beyond what your config says.

## Configuration

Create `~/.config/herdr/herdr-space-diffstat.yml` with only the keys you want
to change; the rest fall back to [`config/defaults.yml`](config/defaults.yml).

```yml
config:
  added-icon: ""
  modified-icon: ""
  deleted-icon: ""
  icon-gap: " "
  poll-interval: 5
```

| Key                    | Default      | Meaning                                                          |
| ---------------------- | ------------ | ---------------------------------------------------------------- |
| `added-icon`           | `+`          | Goes in front of the added count                                  |
| `modified-icon`        | `~`          | Goes in front of the modified count                               |
| `deleted-icon`         | `-`          | Goes in front of the deleted count                                |
| `icon-gap`             | *(blank)*    | Between icon and number — `" "` gives `+ 38`                      |
| `hide-zero`            | `true`       | Leave a count out entirely when it is zero                        |
| `pair-modified`        | `true`       | Pair adds against deletes per hunk; off gives git's own numbers    |
| `include-untracked`    | `true`       | Count lines in untracked files as additions                       |
| `untracked-max-bytes`  | `1048576`    | Skip untracked files larger than this rather than reading them    |
| `poll-interval`        | `5`          | Seconds between passes; `0` means events only                     |
| `ttl-ms`               | `0`          | How long herdr keeps the numbers; `0` derives it from the poll    |
| `git-timeout`          | `10`         | Give up on a git invocation after this long                       |
| `token-prefix`         | `diff`       | Token names: `$diff_added` and friends                            |

The icons default to ASCII because it renders in any font. If you use a Nerd
Font, ``, `` and `` (nf-oct-diff_added / _modified / _removed) are the
obvious swap — check the glyphs exist in your patched font first, since the
octicon codepoints moved in Nerd Fonts v3.

Config is read when the watcher starts, so apply changes with:

```sh
herdr plugin action invoke herdr-space-diffstat.restart
```

A custom config path works too:

```sh
export HERDR_SPACE_DIFFSTAT_CONFIG=~/dotfiles/herdr-diffstat.yml
```

## Commands

```sh
bin/herdr-space-diffstat stats [path]   # what does this checkout come to?
bin/herdr-space-diffstat once           # report every space one time
bin/herdr-space-diffstat start          # start the watcher
bin/herdr-space-diffstat stop --clear   # stop it and take the numbers off
bin/herdr-space-diffstat restart
bin/herdr-space-diffstat status
```

`stats` is the one to reach for when a number looks wrong — it prints the raw
counts and the token values for any directory, without touching herdr.

All of them honour `HERDR_SESSION` / `HERDR_SOCKET_PATH`, so they act on the
same session the `herdr` CLI would.

## How it works

herdr has a first-class place to put this: `workspace.report_metadata` sets
display-only tokens on a space, and `[ui.sidebar.spaces] rows` decides where
they render. So the plugin never touches your labels or your layout — it
reports numbers and herdr draws them.

- The `[[startup]]` hook spawns a detached watcher. herdr startup hooks are
  one-shot and unsupervised, so the watcher supervises itself: it exits when
  the herdr server closes the event stream.
- Nothing in herdr reports a file changing, so the poll is the primary clock
  rather than a safety net. Events only bring a pass forward — most usefully
  `pane.updated`, which carries an agent changing state and so lands around the
  moment an agent stops working and its numbers become worth reading.
- A space has no directory of its own, so the plugin takes one from a pane: the
  focused pane of the active tab, or any pane in the space.
- Checkouts are deduplicated per pass, so five spaces on one repo cost one
  `git diff`, and two worktrees of the same repo get their own counts.
- Every report carries a ttl. If the watcher is killed, herdr drops the numbers
  rather than leaving stale ones on screen — which is also why an idle repo is
  re-reported before the ttl runs out.

Three things about herdr's API that shaped this, recorded in case they save
someone else an afternoon:

- **The socket answers one request per connection, then hangs up.** Event
  subscriptions are the exception. Reuse a connection for a second request and
  you get `BrokenPipeError`.
- **Don't subscribe to `workspace.metadata_updated`.** This plugin *writes*
  those events; subscribing means every pass triggers the next one.
- **`pane.agent_status_changed` cannot be subscribed session-wide.** It needs a
  `pane_id`, and one bad entry fails the whole `events.subscribe` call — so the
  watcher connects, is refused, and exits before its first pass.
- **Don't key plugin state off `HERDR_PLUGIN_STATE_DIR`.** herdr only sets it
  when it launches the command itself, so a watcher started by the startup hook
  and a `stop` you run from a shell land in different directories, never see
  each other's pid file, and you end up with two watchers.

## Cost

One `git diff HEAD -U0` per distinct checkout per poll, streamed and folded
line by line so a huge diff costs time rather than memory, plus one
`git ls-files --others` when untracked counting is on. Every invocation passes
`--no-optional-locks`, so a background poll never takes the index lock out from
under the shell you are working in.

If you keep many large repos open, raise `poll-interval` — the event hooks mean
the numbers still refresh when an agent finishes, which is when you actually
look at them.

## Development

```sh
make test   # 64 stdlib-only tests
make lint
```

The tests cover the hunk pairing, the reporting rules, and a set of cases run
against real `git` output in a throwaway checkout — those skip automatically if
git is not installed.

## Licence

[MIT](LICENSE).
