# Contributing

This project independently reproduces DSec mechanisms. Start with the
[roadmap](ROADMAP.md), [quickstart](docs/guides/QUICKSTART.md) and
[comparison protocol](docs/architecture/DOCKER_DSEC_BENCHMARK_PROTOCOL.md). The current support
boundary is Linux, a trusted single-host runtime user and explicitly
provisioned external VM/storage components.

Open an issue describing the problem, supported configuration, expected
behavior, actual result and a minimal reproduction. For planned work, include
the roadmap work ID (for example, `RM-103`) and its acceptance gate. Remove
credentials and private task data from any attached logs.

Keep changes focused on one behavior. A pull request should explain the user
impact, implementation choice, validation and remaining limits. Preserve the
project's MIT license and the Apache-2.0 notices for Miles-derived code.

Update an existing implementation before adding another runner or helper.
Keep one canonical source; compatibility entry points should delegate to it.
Use configuration for deployment differences instead of copying launch scripts.
Guides and contracts describe current behavior, while acceptance reports record
bounded evidence. Do not append debugging diaries to operational documentation.
Trainer compatibility patches remain opt-in and separate from the core package.

## Local package checks

Tests are grouped by execution requirements:

| Directory | Purpose | Automatic discovery |
| --- | --- | --- |
| `tests/unit/` | Core scheduling, storage, lifecycle and recovery regressions | Yes; Linux-specific checks skip elsewhere |
| `tests/contracts/` | Frozen v0.1 interfaces, legacy imports and module dependency boundaries | Yes |
| `tests/adapters/` | Task, Miles message/reward and verifier guards | Yes; the legacy 3FS publisher check needs the experiment workspace |
| `tests/packaging/` | Source export and opt-in bundle boundaries | Yes; the legacy bundle check needs the experiment workspace |
| `tests/benchmarks/` | Comparison harness accounting and host-isolation guards | Explicit suite in the experiment workspace |
| `tests/training/` | Opt-in trainer compatibility and GRPO probe checks | Explicit suite in the experiment workspace |
| `tests/integration/` | Manual Linux/KVM, snapshot faults and task acceptance | No; files are named `check_*.py` |

Run manual checks as modules, for example
`python -m tests.integration.tb21.check_tb2_trainer_rejoin --help`, after reviewing
their host paths and prerequisites. Some checks require an isolated service,
privileges or provisioned artifacts; they are not part of regular CI.
The published source includes the portable unit/adapter suites and source-export
regression. The extended experiment suites remain in the development workspace.

Use Python 3.11 or newer in a virtual environment:

```sh
python -m pip install 'setuptools>=77' wheel build
python -m pip install --no-deps -e .
python -m build --wheel --no-isolation --outdir dist/core .
python tools/check_wheel.py dist/core/*.whl
python -m unittest discover -s tests/unit -t . -v
python -m unittest discover -s tests/adapters -t . -v
python -m unittest tests.packaging.test_release_source -v
python -m unittest discover -s tests/contracts -t . -v
python tools/check_docs.py
python -m build --wheel --no-isolation --outdir dist/tb21 apps/tb21
python -m pip install dist/core/*.whl dist/tb21/*.whl
python -m unittest discover -s apps/tb21/tests -v
```

The source archive allowlist in `tools/package_release_source.py` defines the
exported runtime, regression and application files. Update it when adding a
file required by a release; do not include generated artifacts or experiment
state. GitHub CI builds both wheels and runs selected regressions without
provisioning KVM, Docker, GPU models or TB2.1 tasks. It is not a real sandbox
or clean-host acceptance test.

Changes to lifecycle, storage, privileges or recovery also need the relevant
real Linux acceptance in an isolated instance. Record the exact revision,
configuration, raw evidence and cleanup result. Stop owned sandboxes through
the SDK before retiring services. A VM PSS comparison does not establish a
full backend resource advantage, and model failure is not an infrastructure
failure.

See release notes for the tested artifact identities. Future roadmap items
remain planned until their acceptance evidence is attached; do not replace
historical or frozen evidence with results from a different revision.
