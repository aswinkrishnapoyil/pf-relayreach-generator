# PowerFactory based Adaptive Distance Protection Dataset Generator

[![Offline regression tests](https://github.com/aswinkrishnapoyil/pf-relayreach-generator/actions/workflows/tests.yml/badge.svg)](https://github.com/aswinkrishnapoyil/pf-relayreach-generator/actions/workflows/tests.yml)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)

Generates structured datasets for machine-learning prediction of adaptive
distance-protection reach settings in PowerFactory grids with distributed
generation (DG).

The pipeline connects to a live **DIgSILENT PowerFactory** installation,
evaluates switch state topologies and randomized line/DG cases,
calculates relay zone reaches and short circuit based infeed corrections, and
exports one graph array Parquet row per accepted scenario. Flat rows, audit
workbooks and randomization logs are retained with the batch files for
traceability.

The repository contains the generator, the modified IEEE 14-bus 110 kV
development grid as a PowerFactory export (`grid/Modified_IEEE14_110kV_FAU.pfd`)
and its 216-state switch-state library (`switch_state/switch_states.csv`). With
the default configuration it regenerates the 14-bus development dataset of the
MSc thesis. It does **not** contain an ML model or PowerFactory binaries.

---

## Repository structure

```text
pf-relayreach-generator/
├── main_script.py                         # pipeline entry point
├── powerfactory.example.json              # local connection template
├── tools/
│   ├── export_switch_inventory.py         # lists the switches of a grid (for new libraries)
│   ├── check_switch_state_islands.py      # lists switch states that create islands (offline)
│   └── time_generation_steps.py           # times the generation steps per case
├── requirements.txt
├── LICENSE
├── grid/
│   └── Modified_IEEE14_110kV_FAU.pfd      # 14-bus development grid (PowerFactory export)
├── switch_state/
│   ├── switch_states.csv                  # 216 switch states of the 14-bus grid
│   └── switch_states.example.csv          # neutral input-format example
├── pf_adaptive_distance_dataset/
│   ├── core/
│   │   ├── config.py                      # generation and runtime settings
│   │   ├── dataset_schema.py              # flat and graph column groups
│   │   └── models.py                      # data containers and exceptions
│   ├── pf_api/
│   │   ├── pf_session.py                  # PowerFactory session lifecycle
│   │   ├── pf_utils.py                    # checked object/attribute access
│   │   ├── slave_cases.py                 # temporary study case lifecycle
│   │   ├── state_capture.py               # baseline state capture
│   │   └── grid_state.py                  # checked state restoration
│   ├── domain/
│   │   ├── topology.py                    # line, terminal and cubicle helpers
│   │   ├── network_topology.py            # corridors, branches and parallels
│   │   ├── dg_utils.py                    # DG discovery and capacity handling
│   │   ├── islanding.py                   # supply check against external grids
│   │   ├── zone_reach.py                  # Zone 1/2/3 reach calculations
│   │   └── infeed.py                      # DG short circuit infeed correction
│   ├── pipeline/
│   │   ├── batch_orchestrator.py          # workers, restart, resume and merge
│   │   ├── dataset_generator.py           # base/randomized scenario loop
│   │   ├── switch_states.py               # switch loading and application
│   │   ├── switch_state_islands.py        # offline islanding check of a library
│   │   ├── randomization.py               # line and DG randomization
│   │   └── case_features.py               # directed-relay feature rows
│   ├── graph/
│   │   ├── graph_arrays.py                # scenario-to-graph conversion
│   │   └── graph_array_utils.py           # graph identifiers and indexing
│   └── exports/
│       ├── export.py                      # streaming batch exports
│       └── validation.py                  # row validation and statistics
└── tests/                                 # offline regression suite
```

---

## Requirements

- Windows.
- A separately installed and licensed DIgSILENT PowerFactory.
- The exact Python version and architecture supported by that PowerFactory
  installation. The supplied path is an example for PowerFactory 2023 SP3 and
  Python 3.9.
- `pandas`, `pyarrow` and `openpyxl` from `requirements.txt`.
- A PowerFactory project containing a target grid, master study case, master
  operation scenario and compatible switch-state library. For the 14-bus
  development grid, import `grid/Modified_IEEE14_110kV_FAU.pfd` (PowerFactory:
  *File > Import > PFD*) into your PowerFactory user.

Install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

PowerFactory and its Python API are not installed by this command.

---

## Setup

### 1. Configure the local PowerFactory connection

The defaults in `config.py` match the 14-bus development grid imported from
`grid/Modified_IEEE14_110kV_FAU.pfd` into the current PowerFactory user.
The default project is `Modified_IEEE14_110kV_FAU` and the default grid is `Grid_110kV.ElmNet`.
If your PowerFactory installation, project name or grid differ, override them
locally. Copy the public template:

```powershell
Copy-Item .\powerfactory.example.json .\powerfactory.local.json
```

Edit the ignored `powerfactory.local.json`:

```json
{
  "PF_PYTHON_PATH": "C:\\Program Files\\DIgSILENT\\PowerFactory 2023 SP3\\Python\\3.9",
  "PROJECT_NAME": "\\your_user\\YourProject",
  "GRID_NAME": "YourGrid.ElmNet",
  "MASTER_STUDY_CASE_NAME": "SC_Master",
  "MASTER_OPERATION_SCENARIO_NAME": "OS_Master"
}
```

Only these five connection fields may be overridden in this JSON file.
Generation, randomization, batching and protection policies remain in
`pf_adaptive_distance_dataset/core/config.py`.

### 2. Provide a switch-state library

For the 14-bus development grid, `switch_state/switch_states.csv` is included:
the 216 switch states of the thesis dataset. Its switch columns use
user-independent IDs (see below), so the library works in the project imported
from `grid/Modified_IEEE14_110kV_FAU.pfd` by any PowerFactory user. The switch
states are identical to the library of the thesis run (SHA-256
`3ade501b308efa7e…`), which used user-bound IDs of the same switches.

For another grid, replace it with that grid's library. The neutral schema
example shows the format, and `tools/export_switch_inventory.py` lists the switch
identifiers of the configured grid:

```powershell
Copy-Item .\switch_state\switch_states.example.csv `
  .\switch_state\switch_states.csv
```

Replace the example column and row with identifiers and states from the target
grid. Other files in `switch_state/` are ignored by Git.

```text
ConfigID;switch_<identifier_1>;switch_<identifier_2>
state_001;1;0
state_002;0;1
```

Every `switch_...` value must be numeric `0` (open) or `1` (closed). Identifiers
may be native switch/cubicle CIM RDF IDs or the deterministic UUID format
supported by the generator. Every column must resolve unambiguously to exactly
one cubicle switch in the configured grid.

The deterministic UUID is built from the project name, the grid name and the
full PowerFactory names of the switch and its cubicle. The generator accepts the
**user-independent** form, in which the leading user folder (`\<user>.IntUser`) is removed from the full names, and the
user-bound form of older libraries, which is valid only for the user who
created the library. Use the user-independent form for new libraries; the
`user_independent_switch_id` column of the inventory lists it:

```powershell
python -B .\tools\export_switch_inventory.py --output .\Results\switch_inventory.csv
```

Then check the library for switch states that cut busbars off from every
external grid. This needs no PowerFactory:

```powershell
python -B .\tools\check_switch_state_islands.py --inventory .\Results\switch_inventory.csv --output .\Results\islanded_switch_states.csv
```

The script prints each islanded switch state with its `ss_<n>` ID, row number,
`ConfigID` and island busbars, and exits with code 1 if it finds any (0
otherwise). `--library <csv>` checks another library than
`switch_state/switch_states.csv`. `--write-clean-library <new csv>` writes a
copy without those rows; removing rows shifts the `ss_<n>` numbers of later
switch states. Input files are never changed and existing output files are
never overwritten. The generator omits islanded switch states in any case;
the script only lets you see and clean them beforehand.

The offline check requires the `connected_object_outserv` and
`connected_object_terminal_full_names` columns written by
`tools/export_switch_inventory.py`. Re-export any inventory missing them.

Limits of the offline check, which the generator's own check does not have:
a line without any switch does not appear in the inventory and can make a
supplied part look like an island; an external grid outside the configured
grid (behind a transformer) is not seen, and the script then stops with
"no external grid"; couplers (`ElmCoup`) are treated as closed.

Before dataset generation, verify the library against a non-production copy of
the project and confirm switch application, electrical validity and
short-circuit convergence. The offline tests cannot certify a live grid.

### 3. Run the offline tests

```powershell
python -B -m unittest discover -s tests -v
```

The GitHub Actions workflow runs the same suite on Windows with Python 3.9 and
3.14. It does not install or execute PowerFactory.

---

## Configuration

All study and runtime settings are in
`pf_adaptive_distance_dataset/core/config.py`.

### Dataset volume

```python
ENABLE_SWITCH_STATE_SCENARIOS = True
MAX_SWITCH_STATE_CONFIG_COUNT = None   # None = all CSV rows
INCLUDE_ORIGINAL_BASE_CASE = True
RANDOMIZED_SCENARIO_COUNT = 25
```

Before policy-based omissions, a library containing `N` switch states produces

```text
N × (1 base case + 25 randomized cases)
```

scenario attempts with the production defaults.

### Protection settings

```python
REACH_GF = 0.85
TERMINAL_ZONE2_REACH_FACTOR = 1.20
ZONE3_REACH_FACTOR = 1.20
ZONE2_TARGET_TO_BASE_RATIO_LIMIT = 2.0
PERSISTENT_ZONE2_FAILURE_MIN_RANDOMIZATIONS = 25
```

### Randomization settings

```python
ENABLE_LINE_RANDOMIZATION = True
LINE_LENGTH_SCALE_MIN = 0.8
LINE_LENGTH_SCALE_MAX = 1.2
ENABLE_LINE_RX_RATIO_RANDOMIZATION = True
LINE_RX_SCALE_MIN = 0.8
LINE_RX_SCALE_MAX = 1.2
LINE_RX_RATIO_MAX = 0.60

ENABLE_DG_CAPACITY_RANDOMIZATION = True
DG_CAPACITY_SCALE_MIN = 0.8
DG_CAPACITY_SCALE_MAX = 1.2

RANDOM_SEED_BASE = 20260929
DG_CAPACITY_RANDOM_SEED_OFFSET = 100000
```

Set `RANDOM_SEED_BASE = None` only when an automatically generated seed is
desired. The selected seed and realized parameter values are recorded, but
exact reproduction also requires the same grid, settings and PowerFactory
object iteration order.

### Batch/runtime settings

```python
DEBUG = False
BATCH_SIZE = 1
RESTART_PAUSE_SECONDS = 15.0
WORKER_STARTUP_TIMEOUT_SECONDS = 60.0
WORKER_STARTUP_RETRIES = 2
KEEP_BATCH_FILES = True
RESUME_RUN_ID = None
```

Each batch uses a fresh worker and PowerFactory engine session. Increasing the
batch size reduces startup overhead but keeps more work in one session. It does
not fix an electrically invalid case or a PowerFactory startup problem.

---

## First live run

Back up the PowerFactory project and close unrelated engine sessions. For a
small smoke test, temporarily use:

```python
MAX_SWITCH_STATE_CONFIG_COUNT = 1
RANDOMIZED_SCENARIO_COUNT = 2
KEEP_BATCH_FILES = True
DEBUG = False
```

Run the generator:

```powershell
python -B .\main_script.py
```

Inspect the terminal log, graph Parquet, flat CSV, Excel audit, randomization
logs and skip report when present. Confirm restoration of switch/service
states, line lengths/types and DG capacities. Only then restore the intended
production counts.

---

## Pipeline workflow

For each batch, the generator:

1. Loads and validates the requested switch-state rows.
2. Opens a fresh PowerFactory engine session and activates the configured
   project, grid, study case and operation scenario.
3. Creates a temporary slave study case/operation scenario pair for each switch
   state.
4. Captures the baseline grid state and applies the complete switch request with
   checked write back.
5. Checks the applied topology for islands, then evaluates the optional base
   case and each randomized realization for supplied switch states.
6. Discovers protected corridors, downstream branches, parallel circuits and
   eligible DG sources.
7. Calculates Zone-1/2/3 reaches and executes the required one-DG-at-a-time
   short circuit calculations for infeed correction.
8. Builds validated flat relay rows and one graph-array row per accepted
   scenario.
9. Restores all changed PowerFactory state and deletes the temporary slave
   objects.
10. Validates each batch, merges accepted graph rows and writes a final run
    manifest.

PowerFactory is hidden by default. `DEBUG = True` or `--debug` shows the GUI and
enables verbose logging.

---

## Implemented protection policies

These are dataset target generation rules. Agreement with the exported labels
does not certify relay coordination, time grading, directional operation or
transient performance.

### Downstream branch selection

Valid downstream branches exclude the relay-return direction and lines of
parallel protected corridors. Zone 2 uses the valid branch with the smallest
reactance. Zone 3 uses the valid branch with the largest reactance. Ties are
broken by length, then resistance. The `longest_downstream_branch_*` fields
describe the branch selected for Zone 3 by reactance.

### Terminal corridors

When no valid downstream branch exists:

- base Zone-1 is `0.85` times the protected corridor impedance.
- base Zone-2 is `1.20` times the protected corridor impedance.
- Zone 3 is inapplicable.

Applicable embedded DG corrections are added afterward. A DG at the remote
terminal is excluded from correction when no valid downstream fault location
exists, but remains represented in the topology and DG features.

Terminal relay queries contain null Zone-3 targets and
`zone3_applicable = 0`. ML consumers must therefore use the target mask
`[1, 1, 1, 1, 0, 0]`; numerical zero is not a Zone-3 label.

### Extreme corrected Zone-2

For every directed relay with nonzero base Zone-2 impedance:

```text
q2 = |Z2,target| / |Z2,base|
```

If any relay in a base or randomized scenario has `q2 >= 2.0`, the complete
scenario is omitted. Its skip record stores the relay, corridor, observed ratio
and configured threshold. Other realizations of the same switch state remain
eligible.

When at least 25 randomized realizations are evaluated and every one fails this
rule, the switch state contributes no scenario. Only randomized realizations
count toward this rule. If the base case is enabled, it receives exactly one
`persistent_switch_state_extreme_zone2` skip record, replacing any earlier
skip record for that case. The threshold is a dataset-admissibility rule. The generator does not cap Zone 2, remove only the failing
relay, or force Zone-2/Zone-3 ordering.

### Islanded switch states

The setting rules assume that every part of the grid is supplied. After a
switch state is applied, and before its base scenario is processed, the
generator checks that every terminal at the end of an available line inside
the configured grid is connected to at least one source:

- connections are in-service lines with both cubicle switches closed (the
  lines the corridor tracing uses), closed in-service couplers (`ElmCoup`) and
  in-service transformers (`ElmTr2`, `ElmTr3`), also outside the configured
  grid, so an external grid behind a transformer still counts;
- sources are in-service external grids (`ElmXnet`, `bus_typ` 113) whose
  cubicle switch is closed. DG units are not sources.

Line and DG randomization do not change the topology, so one check covers all
scenarios of a switch state. If any terminal is unsupplied, the switch state
contributes no scenario. Every expected scenario gets its own skip record, so
the coverage check of the batch still holds:

```json
{
  "scenario_uid": "ss_0180_base_0000",
  "switch_state_row_index": 180,
  "switch_state_config_id": "<ConfigID>",
  "reason": "islanded_switch_state",
  "detail": "Switch state leaves busbars without a connection to any external grid: [Terminal_HV_7, Terminal_HV_8]",
  "island_terminals": [["Terminal_HV_7", "Terminal_HV_8"]]
}
```

In the bundled 14-bus library, `ss_0180` isolates busbars 7 and 8, and
`ss_0192` isolates busbars 10 and 11.

A busbar that is cut off from every line is not reported, because no relay is
traced there.

### Line and DG randomization

- All eligible lines receive independent length scaling in `[0.8, 1.2]`.
- `TypLne` objects may additionally receive positive-sequence R/X variation
  through a temporary cloned line type. X per kilometre stays fixed while R per
  kilometre is scaled, subject to the configured maximum R/X ratio of `0.60`.
- `TypTow` electrical characteristics are geometry-derived and are not modified
  by the R/X routine; these objects receive length-only variation.
- Eligible DG capacities are independently scaled in `[0.8, 1.2]`.

Original type assignments and parameters are restored and verified. Shared
PowerFactory line types are never edited in place.

### Failure and integrity handling

- Switch states that leave busbars without an external grid are omitted
  completely (see [Islanded switch states](#islanded-switch-states)).
- Required short-circuit execution/current failures omit the complete affected
  scenario, record the reason and allow later realizations to continue.
- Partial correction results and stale-current fallbacks never become labels.
- Switching, restoration and graph-integrity failures stop the worker and
  retain temporary files for diagnosis and resume.
- A completed run must account for every expected realization through either an
  exported graph row or an approved skip record.

---

## Graph and supervision contract

One Parquet row represents one accepted scenario. It contains:

- ordered bus identifiers, nominal voltages, DG capacities and categorical bus
  types;
- in-service physical sections with length, resistance, reactance and
  admittance values;
- switched real and imaginary Y-bus matrices;
- directed relay queries kept separate from physical edges;
- six reach targets in the order
  `[Z1_R, Z1_X, Z2_R, Z2_X, Z3_R, Z3_X]`;
- validity and applicability metadata used to construct supervision masks.

Parallel physical circuits remain distinct. Multi-section corridor impedance
is reconstructed as the sum of its sections. A rejected target stays aligned
with its directed query using `directed_edge_target_valid = 0` and must not be
used for training. Audit values, validity flags and calculation-stage metadata
must not be used as predictive inputs.

`bus_typ` is categorical:

| Code | Meaning                 |
| ---: |-------------------------|
| 101 | Load/tie bus            |
| 103 | Synchronous DG bus      |
| 107 | PV/inverter DG bus      |
| 109 | Mixed DG bus            |
| 113 | External-grid/slack bus |
| 127 | Transformer HV bus      |
| 131 | Transformer LV bus      |
| 137 | Junction node           |

Use typed encoders, one hot values or an explicit mapping to embedding indices.
The sparse codes are labels, not continuous measurements.

---

## Outputs

Successful batched runs write the merged dataset and durable provenance record
to `Results/`:

```text
Results/
├── distance_protection_graph_array_<run_id>.parquet
├── run_manifest_<run_id>.json
├── skipped_scenarios_<run_id>.json        # only when scenarios were omitted
└── _batch_tmp/<run_id>/                   # retained when KEEP_BATCH_FILES=True
    ├── run_manifest.json
    └── batch_<start>_<end>/
        ├── distance_protection_graph_array_<timestamp>.parquet
        ├── distance_protection_flat_rows_<timestamp>.csv
        ├── distance_protection_audit_<timestamp>.xlsx
        ├── line_randomization_log_<timestamp>.csv
        ├── dg_randomization_log_<timestamp>.csv
        ├── metadata_<timestamp>.json
        ├── statistics_<timestamp>.json
        └── skipped_scenarios.json         # only when required

logs/
├── dataset_generator_terminal_<timestamp>.txt
└── pipeline.log
```

Only the graph Parquets are merged across batches. Supporting flat CSV, Excel,
randomization and statistics files remain batch-level audit material.

The final manifest records the random seed, switch library SHA-256 hash, policy
signature, batch accounting and final outputs. It remains available when batch
files are deleted.

Regenerate datasets produced with length-based Zone 3 branch selection to
update the reach targets and branch features. For datasets that already use
reactance-based branch selection but predate the islanding check, remove all
scenarios from islanded switch states before reuse.

---

## Running and resuming

```powershell
python -B .\main_script.py
python -B .\main_script.py --help
python -B .\main_script.py --batch-size 2 --restart-pause-seconds 15
python -B .\main_script.py --worker-startup-timeout-seconds 180
python -B .\main_script.py --worker-startup-retries 2
python -B .\main_script.py --keep-batch-files
python -B .\main_script.py --no-keep-batch-files
python -B .\main_script.py --debug
python -B .\main_script.py --resume-run 20261004_120000
```

CLI options override the corresponding config defaults for one invocation.
Startup timeouts stop and retry only a worker that has not completed the
PowerFactory connection handshake. Once connected, the supervisor does not
force-stop protection calculations or restoration.

Failed runs retain `Results/_batch_tmp/<run_id>/`. Resume validates completed
batches and rejects changes to the switch CSV hash, seed or generation-policy
signature. A run ID is a timestamp identifier, not a filesystem path.

---

## Timing the generation steps

`tools/time_generation_steps.py` runs a small generation (default: switch states 1–3,
the base case and four randomized cases each) in one PowerFactory session and
records how long each step of every case takes. The generator code is not
modified; the timers are attached in memory and the output goes to
`Results/step_timing_<time>/`.

```powershell
python -B .\tools\time_generation_steps.py
```

`--generator` defaults to this repository. Run the command from the
repository root so its relative paths resolve correctly.

---

## Licence

The source is distributed under the GNU General Public License version 3.0;
see [LICENSE](LICENSE). DIgSILENT PowerFactory and its API binaries are not
distributed with this repository.
