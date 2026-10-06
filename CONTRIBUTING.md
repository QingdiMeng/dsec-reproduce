# Contributing

This project independently reproduces DSec mechanisms. Start with the
[roadmap](ROADMAP.md), [quickstart](QUICKSTART.md) and
[comparison protocol](DOCKER_DSEC_BENCHMARK_PROTOCOL.md). The current support
boundary is Linux, a trusted single-host runtime user and explicitly
provisioned external VM/storage components.

Open an issue describing the problem, supported configuration, expected
behavior, actual result and a minimal reproduction. For planned work, include
the roadmap work ID (for example, `RM-103`) and its acceptance gate. Remove
credentials and private task data from any attached logs.

Keep changes focused on one behavior. A pull request should explain the user
impact, implementation choice, validation and remaining limits. Preserve the
project's MIT license and the Apache-2.0 notices for Miles-derived code.

## Local package checks

Use Python 3.11 or newer in a virtual environment:

```sh
python -m pip install 'setuptools>=77' wheel build
python -m build --wheel --no-isolation --outdir dist/core .
python tools/check_wheel.py dist/core/*.whl
python -m unittest tools.test_release_source -v
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
