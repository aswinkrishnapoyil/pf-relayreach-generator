# PF RelayReach Generator

[![Offline regression tests](https://github.com/aswinkrishnapoyil/pf-relayreach-generator/actions/workflows/tests.yml/badge.svg)](https://github.com/aswinkrishnapoyil/pf-relayreach-generator/actions/workflows/tests.yml)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](LICENSE)

Generates structured datasets for machine-learning prediction of adaptive
distance-protection reach settings in PowerFactory grids with distributed
generation (DG).

The pipeline connects to a live **DIgSILENT PowerFactory** installation,
evaluates switch-state topologies and randomized line/DG realizations,
calculates relay-zone reaches and short-circuit-based infeed corrections, and
exports one graph-array Parquet row per accepted scenario. Flat rows, audit
workbooks and randomization logs are retained with the batch files for
traceability.

This repository contains the generator only. It does **not** contain an ML
model, a PowerFactory grid, PowerFactory binaries or a grid-specific
switch-state library.

---

## Repository structure

```text
pf-relayreach-generator/
├── main_script.py                         # pipeline entry point
├── powerfactory.example.json              # local connection template
├── requirements.txt
├── LICENSE
├── switch_state/
│   └── switch_states.example.csv          # neutral input-format example
├── pf_adaptive_distance_dataset/
│   ├── core/
│   │   ├── config.py                      # generation and runtime settings
│   │   ├── dataset_schema.py              # flat and graph column groups
│   │   └── models.py                      # data containers and exceptions
│   ├── pf_api/
│   │   ├── pf_session.py                  # PowerFactory session lifecycle
│   │   ├── pf_utils.py                    # checked object/attribute access
│   │   ├── slave_cases.py                 # temporary study-case lifecycle
│   │   ├── state_capture.py               # baseline state capture
│   │   └── grid_state.py                  # checked state restoration
│   ├── domain/
│   │   ├── topology.py                    # line, terminal and cubicle helpers
│   │   ├── network_topology.py            # corridors, branches and parallels
│   │   ├── dg_utils.py                    # DG discovery and capacity handling
│   │   ├── zone_reach.py                  # Zone 1/2/3 reach calculations
│   │   └── infeed.py                      # DG short-circuit infeed correction
│   ├── pipeline/
│   │   ├── batch_orchestrator.py          # workers, restart, resume and merge
│   │   ├── dataset_generator.py           # base/randomized scenario loop
│   │   ├── switch_states.py               # switch loading and application
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
  operation scenario and compatible switch-state library.

Install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

PowerFactory and its Python API are not installed by this command.

---

## Setup

### 1. Configure the local PowerFactory connection

Copy the public template:

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

No real switch identifiers are published. Copy the neutral schema example:

```powershell
Copy-Item .\switch_state\switch_states.example.csv `
  .\switch_state\switch_states.csv
```

Replace the example column and row with identifiers and states from the target
grid. The active file is ignored by Git because the identifiers are
grid-specific.

```text
ConfigID;switch_<identifier_1>;switch_<identifier_2>
state_001;1;0
state_002;0;1
```

Every `switch_...` value must be numeric `0` (open) or `1` (closed). Identifiers
may be native switch/cubicle CIM RDF IDs or the deterministic UUID format
supported by the generator. Every column must resolve unambiguously to exactly
one cubicle switch in the configured grid.

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

RANDOM_SEED_BASE = 20260915
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
batch size reduces startup overhead but keeps more work in one session; it does
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
3. Creates a temporary slave study-case/operation-scenario pair for each switch
   state.
4. Captures the baseline grid state and applies the complete switch request with
   checked write-back.
5. Evaluates the optional base case and each randomized realization.
6. Discovers protected corridors, downstream branches, parallel circuits and
   eligible DG sources.
7. Calculates Zone 1/2/3 reaches and executes the required one-DG-at-a-time
   short-circuit calculations for infeed correction.
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

These are dataset target-generation rules. Agreement with the exported labels
does not certify relay coordination, time grading, directional operation or
transient performance.

### Terminal corridors

When no valid downstream branch exists:

- base Zone 1 is `0.85` times the protected-corridor impedance;
- base Zone 2 is `1.20` times the protected-corridor impedance;
- Zone 3 is inapplicable.

Applicable embedded-DG corrections are added afterward. A DG at the remote
terminal is excluded from correction when no valid downstream fault location
exists, but remains represented in the topology and DG features.

Terminal relay queries contain null Zone-3 targets and
`zone3_applicable = 0`. ML consumers must therefore use the target mask
`[1, 1, 1, 1, 0, 0]`; numerical zero is not a Zone-3 label.

### Extreme corrected Zone 2

For every directed relay with nonzero base Zone-2 impedance:

```text
q2 = |Z2,target| / |Z2,base|
```

If any relay in a base or randomized scenario has `q2 >= 2.0`, the complete
scenario is omitted. Its skip record stores the relay, corridor, observed ratio
and configured threshold. Other realizations of the same switch state remain
eligible.

When at least 25 randomized realizations are evaluated and every one fails this
rule, the switch state contributes no scenario, including its base case. This
is a supervisor-approved dataset-admissibility rule, not a universal
relay-setting limit. The generator does not cap Zone 2, remove only the failing
relay, or force Zone-2/Zone-3 ordering.

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

Use typed encoders, one-hot values or an explicit mapping to embedding indices.
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
randomization and statistics files remain batch-level audit material. There is
no separate “ML-ready” dataset variant.

The final manifest records the random seed, switch-library SHA-256 hash, policy
signature, batch accounting and final outputs. It remains available when batch
files are deleted.

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

## Licence

The source is distributed under the GNU General Public License version 3.0;
see [LICENSE](LICENSE). DIgSILENT PowerFactory and its API binaries are not
distributed with this repository.
