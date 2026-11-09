# Pane Center

Moved verbatim from the README, except two paragraphs that stay there.

`kilix-panes` (also installed as the compatible `kilix-switch`) replaces the
terminal's two built-in choosers, which were the
same thing twice: a numbered list of titles, one for pages and one for panes. A
title is a poor handle on a pane — several are `bash` and several more are
whatever directory they started in — so the list told you least exactly when you
had enough windows to need it.

It shows one tree of pages and their panes, with activity, coding-agent type,
process, working directory, PTY-broker health, current task, and a live view of
what the highlighted pane is showing. `/` filters across all of those fields;
`s` writes a message to the highlighted pane and submits it; `Tab` cycles the
scope between everything, this page, and everywhere else. Kilix binds `F12` to
open on everything and its tmux-style leader `q` to open on this page.

> The paragraph on how activity is decided (`working`, `idle`, `agent`)
> is in the [README](../README.md#pane-center-activity).

For live OMP panes, a clean assistant stop becomes idle only after a short
stability window. OMP 18.3.2 can automatically retry a provider error after a
backoff (five minutes by default, potentially longer for an opted-in quota-reset
wait), but writes the failed assistant row without persisting its
`auto_retry_start` event. Therefore a transcript ending in provider `error`
remains conservatively `working`; a finite idle debounce would expose the pane
during a retry backoff. Deliberate aborts still use the short stability window.

The same snapshot is a scriptable `kilix panes` interface:

```sh
kilix panes list                         # compact table
kilix panes --json                       # stable kilix.panes/v1 record
kilix panes dump 338 --lines 60          # last 60 lines, including scrollback
kilix panes dump 338 -n 20 --screen      # visible screen only
kilix panes wait 338 --for idle --timeout 300
kilix panes send 338 --enter 'continue with the next item'
printf 'status please' | kilix panes send 338 --enter

kilix panes new --name codex-office --panes 4 \
  --pane-name administrator,assistant,engineer,worker \
  --pane-dir ~/office/administrator,~/office/assistant,~/office/engineer,~/office/worker \
  --command 'codex --yolo' \
  --initial-prompt 'familiarize yourself with your role' --enter

kilix panes close 338                    # one pane
kilix panes close 338 --page             # the whole page it sits in
```

Targets may be a pane ID, a unique title/substring, a broker-session prefix, or
a coding-session ID prefix. Ambiguity is rejected with the matching pane IDs.
`send` refuses the caller's own pane unless `--allow-self` is explicit, targets
the exact per-pane broker marker, and splits UTF-8 input into the authorizer's
1024-byte chunks. `--enter` emits carriage return, which submits both a shell
line and the current Codex prompt. An accepted send remains fire-and-forget;
read the pane back with `dump` when delivery must be proved.

"This page" means the page the tool is *running* on, resolved from its own
`KITTY_WINDOW_ID`, not whichever page the terminal currently considers active —
an overlay takes the focus the moment it opens, so the two are rarely the same
question.

Renaming and closing are here because a chooser that can see everything and
change nothing sends you somewhere else to finish the job. Closing always asks
first, and both go through the terminal's remote control, which refuses them
outright unless Kilix's scoped credential has been widened to allow them.

### Creating panes

`new` builds a page and fills it, which is the one verb here that makes something
rather than reading it. Three refusals are deliberate:

- **A per-pane list must match the pane count exactly.** Four panes and three
  directories is an error, not a cue to invent the fourth — padding would build a
  different surface than the one asked for, and would succeed while doing it.
- **`--command` is split into a fixed argv here and never handed to a shell.** A
  title, a path or a command containing `;` stays one inert argument. This is the
  same rule `launch_tab` already followed for the desktop launchers.
- **A created pane is not yet an addressable one.** The PTY broker marker that
  `send-text` matches on is written by the pane's own startup, so `--initial-prompt`
  waits for it per pane (`--ready-timeout`) and reports the panes it could not
  reach instead of assuming the text landed. Unreachable panes make the command
  exit non-zero with the ids named.

The TUI offers only the narrow case — `n`, type a title, Enter creates one shell
pane in a new page. A four-pane office with per-pane directories carries more
arguments than a one-line prompt can hold honestly, so it stays a scripted act.

### Agent activity and tell/wait

The pane `activity` is an observation of provider records, not certification that
input is safe. JSON explicitly reports `activity_readiness_verified: false`.
`idle` means an observed turn ended; it does not establish an empty composer,
absence of a menu, or delivery of a previous message. Input acceptance likewise
does not prove that the application received a prompt.

Codex activity requires one unambiguous writer with a matching live command
line and start time, holding one writable rollout. A listed Node launcher and
native child count as one owner only when live process records verify the
direct parent/child relationship, both identities remain stable, neither PID
is duplicated, and the launcher holds no writable rollout. The relationship
is checked again after reading and at the end of the snapshot. The descriptor must still
name the same inode, and the file and owner must remain unchanged across the
read. A completion older than the process, conflicting owners, multiple open
rollouts, malformed or unknown newer records, incomplete turn boundaries, or
exceeded read limits leave the pane as `agent`. No directory, newest-file, or
resume-argument fallback guesses the current session. Clients that open their
rollout only briefly can therefore remain `agent` between writes.

Grok activity uses the target process's own `GROK_HOME` (or its `HOME/.grok`).
Exactly one registry row must name that process and session, with an opening
time after the process started. Bounded, coherent events must follow that
opening time. The registry, log and process are checked again after reading.
Conflicts, missing opening times, stale completions and unreadable newer events
leave the pane as `agent`. Pending permissions survive unrelated tool events;
only coherent matching resolutions clear them. Completed turns retain the
five-second settling delay.

> The paragraph on the third provider's activity source is in the
> [README](../README.md#pane-center-activity).

`kilix panes wait PANE --for idle` still works for supported Codex/Grok
observations; unknown state never satisfies it. A wait also refuses a changed
broker session. `kilix panes tell` remains an explicit bounded input operation,
not an automatic readiness check. Higher-level clients that gate tell on idle
can use the retained states, but must still honor their delivery and composer
checks. For an uncertain pane, use the host's broker-bound deferred delivery
workflow and inspect its receipt; do not infer readiness from elapsed time.

These checks reduce stale observations but cannot eliminate the interval
between the last read and an action. They do not establish physical terminal,
installed-client, or ISO acceptance.
