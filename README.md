# PF RelayReach Generator

PowerFactory-based dataset generator for adaptive distance-protection reach
prediction. Version `v3.3.2` exports directed relay targets and physical-grid
graph arrays for MLP/GNN development; it does not train ML models.

For each switch state, the generator evaluates the base case and randomized
line/DG realizations, calculates protection targets and infeed corrections,
validates results, and exports a Parquet dataset with provenance and audit files.

## Requirements and setup

- Windows and a separately installed, licensed DIgSILENT PowerFactory.
- Python 3.9 or newer, **using the exact Python version and architecture required
  by your PowerFactory installation**. The supplied installation path is an
  example for PowerFactory 2023 SP3/Python 3.9.
- pandas, pyarrow and openpyxl from `requirements.txt`.
- A PowerFactory project with a target grid, master study case, master operation
  scenario and matching switch-state library. No grid model or API binary is
  distributed here.

~~~powershell
python -m pip install -r requirements.txt
Copy-Item .\powerfactory.example.json .\powerfactory.local.json
~~~

Do not overwrite an existing local configuration. Edit `powerfactory.local.json`
for `PF_PYTHON_PATH`, `PROJECT_NAME`, `GRID_NAME`,
`MASTER_STUDY_CASE_NAME` and `MASTER_OPERATION_SCENARIO_NAME`. Only these
connection fields may override defaults; unknown keys and empty/non-string
values are rejected. This file is ignored by Git.

All generation-policy, randomization, batching, debug, restart and retention
settings remain in `pf_adaptive_distance_dataset/core/config.py`. Local
connection settings override its corresponding defaults.

No real switch-state library or PowerFactory object identifier is published.
Create the active input from the neutral template, then replace its example
header and row with identifiers and states from your own grid:

~~~powershell
Copy-Item .\switch_state\switch_states.example.csv `
  .\switch_state\switch_states.csv
~~~

The active `switch_states.csv` is ignored by Git because it is grid-specific.
Copied/renamed projects can have different identifiers, so regenerate or
remap the library before every new-grid run. Confirm that every CSV column
identifies exactly one switch in the configured project, apply each state in a
non-production copy of the grid, and verify switch application and load-flow
convergence before starting dataset generation.

## Tests and first live run

Back up the PowerFactory project first and close unrelated engine sessions.
The generator reserves `SC_Slave_ss_<index>` and `OS_Slave_ss_<index>` for
temporary objects (prefixes are configurable). Cleanup selects only those
names and study-case/scenario classes, and leaves the user's recycle bin alone.

~~~powershell
python -B -m unittest discover -s tests -v
~~~

Tests use mocked PowerFactory objects and real temporary exports; they do not
certify a live grid. Grid-specific switch mapping, load-flow convergence and
protection applicability must therefore be checked in PowerFactory before the
production run. Every requested switch column must map unambiguously.

The included GitHub Actions workflow runs the offline suite on Windows with
Python 3.9 and 3.14. It does not install or run PowerFactory.

For a smoke test, temporarily set these in `config.py`:

~~~python
MAX_SWITCH_STATE_CONFIG_COUNT = 1
RANDOMIZED_SCENARIO_COUNT = 2
KEEP_BATCH_FILES = True
DEBUG = False
~~~

~~~powershell
python -B .\main_script.py
~~~

Inspect the terminal log, Parquet, skip report when present, flat CSV, Excel
audit and line/DG randomization logs. Confirm restoration of switch/service
states, line lengths/types and DG capacities. Then restore production counts:
`None` for all library states and `25` randomizations in the current study.
The base case adds one realization when `INCLUDE_ORIGINAL_BASE_CASE = True`.

Dependency ranges are compatibility bounds, not an exact environment lock.
Record the production environment, for example:
`python -m pip freeze > Results/python_environment.txt`.

## Running and resuming

~~~powershell
python -B .\main_script.py
python -B .\main_script.py --help
python -B .\main_script.py --batch-size 2 --restart-pause-seconds 15
python -B .\main_script.py --keep-batch-files
python -B .\main_script.py --no-keep-batch-files
python -B .\main_script.py --debug
python -B .\main_script.py --resume-run 20261004_120000
~~~

CLI options override config defaults for one invocation. PowerFactory is
hidden by default. Each batch starts a fresh worker; the default is one switch
state. Startup timeouts stop only that worker's process tree and retry startup.
After the connection handshake, the watchdog never force-stops calculations
or restoration. Changing batch size/timeouts does not repair an electrical case.

Failed runs retain `Results/_batch_tmp/<run_id>/`. Resume validates completed
batches and rejects changes to the CSV hash, seed or generation signature,
including terminal reach, ratio-limit and persistence settings. Run IDs are
timestamp identifiers, not paths. `Config.RESUME_RUN_ID` also supports resume;
reset it to `None` afterward.

Start a new run with this first repository revision. Older manifests do not
record every setting now checked. Existing datasets are not retroactively
modified.

## Implemented study policies

These are target-generation rules, not proof of relay coordination, time
grading, directional operation or transient performance.

### Terminal corridors

With no valid downstream branch, **base** Zone 1 is 85% and base Zone 2 is 120%
of the protected corridor impedance. Zone 3 is inapplicable. Applicable
embedded-DG corrections are then added. A DG at the remote terminal is excluded
from correction when no valid downstream fault location exists, but remains
in topology/DG features.

Terminal queries have `zone3_applicable = 0` and null Zone-3 R/X targets.
ML consumers must use supervision mask `[1,1,1,1,0,0]`; numerical zero is not
a substitute label for an inapplicable zone.

### Extreme corrected Zone 2

For each directed relay with nonzero base impedance:

~~~text
q2 = |Z2,target| / |Z2,base|
~~~

If any relay has `q2 >= 2.0`, the **complete realization** is omitted,
including a base case. Its skip record identifies the relay, corridor, ratio
and threshold. Other realizations remain eligible. If at least 25 randomized
realizations are evaluated and all fail this ratio rule, the switch state
contributes no scenario, including its base case.

This is a supervisor-approved dataset-admissibility criterion, not a universal
relay-setting limit. No individual relay is dropped to retain a failing case;
Zone 2 is not capped, and Zone 2/3 ordering is not forced. Review skip records
before training.

### Randomization

Length and DG-capacity scale ranges default to `[0.8, 1.2]`.
`TypLne` lines also vary positive-sequence R/X through a temporary cloned
type: X per km stays fixed and R per km is scaled. The upper scale is
`min(LINE_RX_SCALE_MAX, LINE_RX_RATIO_MAX / reference_ratio)`.
Sampling is uniform between the configured lower scale and this upper scale;
if the cap is below the lower scale, the cap itself is used. The default R/X
cap is 0.60. Length scaling independently scales the resulting total R and X.

`TypTow` electrical characteristics are geometry-derived, so its type is
not altered by the R/X routine; it receives length-only variation. Original
assignments and parameters are restored and read back. The original type
library is not edited in place. Seeds and actual parameter realizations are
recorded; reproduction also requires consistent model, object order and settings.

### Failures and integrity

- Required short-circuit execution/current failures omit the complete affected
  realization and record its reason; later realizations continue.
- No partial correction or stale-current fallback becomes a valid label.
- Switching, restoration and graph-integrity failures stop the worker and
  retain files for diagnosis/resume.
- Unexpected failures cannot silently yield a complete dataset: every expected
  realization must be covered by an exported graph or an approved skip record.

## Switch and graph contracts

Switch columns are `switch_<identifier>` with numeric `0` or `1` values.
Native switch/cubicle CIM IDs and the manual-library deterministic UUID format
are supported. Unknown/ambiguous mappings, conflicting aliases and ignored
writes fail checks. Floating write/restoration checks allow
`rel_tol=1e-7`, `abs_tol=1e-9` rounding. Open-switch and out-of-service DGs
are excluded from active topology, bus DG features and infeed. DG identity uses
full PowerFactory paths.

One graph row represents a scenario. Physical sections and directed relay
queries are separate arrays; parallel circuits stay distinct, and corridor
impedance is the sum of its sections. Flat rows and graph labels share
validation. Rejected labels remain aligned queries with
`directed_edge_target_valid = 0` and must not train the models. Supervision
flags and calculation-stage audit values must not leak into ML inputs.

`bus_typ` is categorical:

| Code | Meaning |
| ---: | --- |
| 101 | Plain/load/tie bus |
| 103 | Synchronous DG bus |
| 107 | PV/inverter DG bus |
| 109 | Mixed DG bus |
| 113 | External-grid/slack bus |
| 127 | Transformer HV bus |
| 131 | Transformer LV bus |
| 137 | Junction node |

Use typed encoders, one-hot values or a mapping to embedding indices.
The sparse codes are not continuous measurements.

## Outputs

~~~text
Results/
  distance_protection_graph_array_<run_id>.parquet
  run_manifest_<run_id>.json
  skipped_scenarios_<run_id>.json  # only when scenarios are omitted
logs/
  dataset_generator_terminal_<timestamp>.txt
  pipeline.log
~~~

The final manifest remains available even with `KEEP_BATCH_FILES = False`.
It records the seed, CSV hash, policy signature, batch accounting and final
outputs. With retention enabled, `Results/_batch_tmp/<run_id>/` also holds
batch Parquets, flat CSVs, Excel audits, line/DG logs, metadata/statistics
and skip records. Supporting CSV/Excel files are not merged at the top level.

The full graph Parquet is the only dataset variant. There is no separate
“ML-ready” export. Consumers explicitly select approved inputs and apply masks.

## Repository contents

~~~text
main_script.py
pf_adaptive_distance_dataset/  # config, API, domain, graph, pipeline, exports
tests/
switch_state/switch_states.example.csv # neutral schema example
powerfactory.example.json
requirements.txt
LICENSE
~~~

Git excludes results, logs, caches, local connection settings, old patch
archives, alternative config copies, grid-specific utilities and historical
switch libraries. These files can remain on the developer's disk; the active
switch library is not published.

## First commit and push

Review the staged files before committing. Do not force-add ignored output or
local configuration files. After choosing the remote repository URL:

~~~powershell
git status --short
git diff --cached --check
git commit -m "Initial generator release (v3.3.2)"
git remote add origin <repository-url>
git push -u origin main
~~~

The local preparation does not create a remote repository, commit or push.
GitHub Actions will verify the offline suite after a push; live PowerFactory
validation remains a separate step.

## Licence

The source is distributed under GNU GPL version 3.0; see [LICENSE](LICENSE).
PowerFactory and its API binaries are not distributed with this repository.
