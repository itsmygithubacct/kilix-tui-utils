# Kilix Rollout Resume

`kilix-rollout-resume` discovers and resumes Claude Code, Codex, and Kimi Code
conversations from one picker and one command. It includes the complete
recovery surface formerly provided by the standalone Claude and Codex tools.

## Discovery

```sh
kilix-rollout-resume list --since 6h --state candidates
kilix-rollout-resume list --state interrupted --query project-name
kilix-rollout-resume show SESSION_ID
```

The unified state names are `idle`, `cut-off`, `live`, `orphaned`, and
`invalid`. The compatibility names `resumable` and `interrupted` are accepted
as aliases for `idle` and `cut-off`.

Use `--projects-dir` for a non-default Claude projects directory,
`--no-orphans` to hide Claude side-car directories whose transcript is gone,
`--sessions-dir` for a non-default Codex sessions directory, and `--archived`
to include the sibling Codex `archived_sessions` tree. `--all-time` disables
the modification-time window.

`--json` emits the provider-neutral record directly. `--envelope` emits the
stable success/error shape used by the retired tools:

```json
{"ok": true, "data": []}
```

Expected failures include a stable `code` (`EUSAGE`, `ENOENT`, `ECONFLICT`,
`EBACKEND`, or `EPACING`). `--no-header` suppresses the plain-text list header.
The `claude` and `codex` command namespaces preserve the retired tools'
machine-facing convention, so `kilix-rollout-resume claude list --json`
automatically uses the stable envelope and legacy state/field aliases. Their
former default windows (Claude 7d, Codex 1h) and detached-tmux `resume`
behavior are retained inside those namespaces.

## Resume and restore

```sh
kilix-rollout-resume resume ID
kilix-rollout-resume resume ID --detached --name recovery --attach
kilix-rollout-resume restore --state candidates --limit 5 --yes
kilix-rollout-resume restore ID1 ID2 --interval 60 --yes
```

Every resume supports `--cwd`, `--force-live`, `--dry-run`, and a
provider-specific executable override (`--claude`, `--codex`, or `--kimi`).
Claude also supports `--fork`, `--permission-mode`, `--model`, and `--prompt`
for single or batch restores. `--yolo` enables the provider's own unsafe flag;
`--no-yolo` overrides a shared unsafe default.

Batch launches share a private, cross-process pacing record and cannot be
configured below 30 seconds. A real batch requires confirmation or `--yes`.

Native tmux is the default backend. `--tb /path/to/tb` selects tmux-cli's JSON
backend and its pane logging; `--no-log` disables logging for that launch.
`configure --tb ...` makes that choice persistent, while `--native-tmux`
bypasses a configured adapter for one invocation.

## Picker

The picker searches all providers together. `/` filters, Space marks one
recoverable session, `*` marks every visible candidate, and `R` restores the
marked set through the shared pacer. `x` starts the selected session detached;
`A` starts it and offers to attach. `v`, `a`, and `t` cycle state, provider,
and the 1h, 6h, 24h, 7d, 30d, and all-time ranges. `y`/`!` toggles unsafe mode
after confirmation. While a paced launch is counting down, `q`/Escape cancels
before the pending agent starts.

The TUI accepts the same source roots, archive/orphan controls, executable
overrides, launch interval, and `tb` backend selection as the CLI.

## Agents

```sh
kilix-rollout-resume status
kilix-rollout-resume install kimi --yes
kilix-rollout-resume update claude
kilix-rollout-resume sync-menu
```

`status` reports every agent; `--json` emits the machine shape. `install` runs
the vendor's own documented command, printed with its source URL, and never
without confirmation or `--yes`. `update` delegates to the agent's own updater
(`claude update`, `codex update`, `kimi upgrade`) rather than re-running an
install script. In the picker, Tab switches between the session and agent
panes, and Enter on an agent installs or updates it.

`sync-menu` refreshes the Kilix-95 Start-menu entries: the picker entry is
always present, and a per-agent "Update <agent>" entry exists only while that
agent is installed. `install.sh` and installs done from inside the tool sync
them too.

## Configuration and launcher

```sh
kilix-rollout-resume configure --tb /path/to/tb --interval 60
kilix-rollout-resume doctor --archived
kilix-rollout-resume prune
kilix-rollout-resume install-launcher
kilix-rollout-resume uninstall-launcher
```

Configuration and pacing state live under
`~/.local/kilix_rollout_resume` by default with private permissions.
Provider-specific legacy executable, `tb`, and interval settings are read as
migration fallbacks. Focused launcher removal preserves configuration.
