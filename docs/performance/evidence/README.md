# Evidence inventory

The Spanish [analysis](../2026-11-15-analysis.md) interprets these measurements.
The [laboratory guide](../../PERFORMANCE.md) documents reproduction and limits.

- `2026-11-15-measurements.json`: valid study samples, parameters, cgroup limits and
  pressure, environment, asset hashes, source manifests and artifact identities.
- `2026-11-15-paired-summary.json`: medians/ranges and paired percent reductions.
  Bootstrap intervals resample the observed pairs; they do not predict a
  population p95, especially with only three pairs.
- `2026-11-15-traces.json`: inclusive call spans separated from timing samples.

Source `base-*`/`paired-*-base` cases normally use the archived `a0c162b` product
with only the diagnostic dispatch/module added. Source `after-*` cases use the
UI/engine changes. `paired-cpu-*` cases use executable tmpfs. The early sequential
matrix is retained despite host-load drift; the analysis bases improvement
claims on counterbalanced pairs and unchanged-code controls.

Frozen comparisons have explicit controls:

- `artifacts.original`: archived product plus diagnostic support.
- `artifacts.phase1`: UI/engine improvements with file URLs, without the stable
  QML resource namespace; the control for the resource-cache experiment.
- `artifacts.qrc_measured`: resource-cache change, before the final initial-read
  guard. `paired-qrc-*` identifies those experiments.
- `artifacts.final`: delivered AppImage, including that guard. The seven
  `paired-final-warm-*` pairs and `final-head-warm` test this exact artifact.

Every frozen command has a path and, in the later cases, `command_sha256`.
Source hashes describe the supplied source tree; the executable hash identifies
which frozen artifact actually ran. Paired reductions are medians of `100*(1-after/before)`; intervals use 5,000 resamples of observed pairs with seed 410. The `commit` and `dirty` fields identify the
**driver checkout**, not a claim that an archived variant is that Git HEAD.
The source manifests and control definitions identify the child code.

`qml-*` are exploratory minimal-window controls, not equivalent application
implementations. Their Main.qml contains only:

```qml
import QtQuick
import QtQuick.Controls
ApplicationWindow {
 required property QtObject controller
 property bool dailyMode: false
 property int page: 0
 visible: true; width: 980; height: 780
 Text { anchors.centerIn: parent; text: "Linux Theming" }
}
```

Temporary paths, private cache filenames and synthetic journals are lab data.
No desktop configuration contents, real process lists, credentials or private
application data are recorded in these files. Binary pstats, generated images,
full stdout/stderr and the preserved local artifacts are in ignored
`build/performance/`. Failed fixture pilots have no valid timing sample and are
excluded from the consolidated timing data.
