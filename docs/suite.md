# The utility suite

These paragraphs follow the command table in the
[README](../README.md#commands) and were moved here verbatim.

The shared Programs registry includes **PDF Conversion** as a direct tab/pane
application through `kilix app run kilix-pdf-conversion`. The same registry
feeds both the TUI desktop and `kilix-launcher`, which is also the Programs
object used by Kilix Land.

The five center commands are focused applications over that same state and
registry, not parallel menus. They stop at their own root when the user walks
back, accept catalog action deep links, and retain the same in-place floor and
Kilix-page launch behavior as the full desktop. The accessory tools share the
shell, keymap, document opener, and OSC-52 clipboard support with the rest of
the suite.

CPU, Memory, and Temperatures consume one versioned `kilix-telemetry` snapshot
when the suite is launched by Kilix. That record supplies global and per-core
CPU use, frequency, RAM, swap, PSI, VM counters, processes, temperatures, and
fan speeds to every surface without each pane walking `/proc` and `/sys`
again. The same utilities retain their direct read-only collectors when run
standalone or when the shared sampler is unavailable, so TUI, IceWM, Land, and
remote-shell launches have the same data model without a hard service
dependency.

### Machine-readable system health

`kilix-system` keeps its interactive facts view and its line-oriented
`--print` output. For automation, `--json` emits one combined health snapshot:

```sh
kilix-system --json
kilix-system --json --top 5
```

`--top N` accepts 1 through 50 and limits the process records; it is meaningful
only with `--json`. The report has `schema_version: 1` and contains:

- `cpu`: aggregate and per-core utilization measured over a short sample,
  logical-core count, and the 1/5/15-minute load averages;
- `memory`: RAM availability and use plus swap totals and percentages, in
  bytes;
- `disks`: device, mount point, filesystem type, byte totals, and use for each
  readable real filesystem;
- `network`: aggregate byte, packet, and error counters from `/proc/net/dev`;
- `top_processes`: processes ranked by cumulative CPU time, including PID,
  name, resident bytes, memory percentage, CPU time, and kernel state; and
- numeric and UTC ISO-8601 timestamps describing when the snapshot was taken.

Network values are counters since the kernel initialized each interface, not
transfer rates. Process `cpu_time` is likewise cumulative rather than an
instantaneous CPU percentage. The command reads Linux `/proc` directly and
uses only the Python standard library; unreadable or unavailable sources
degrade to empty or zero values instead of adding a monitoring-service or
`psutil` dependency.
