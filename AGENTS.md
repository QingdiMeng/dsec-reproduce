# Working in this repository

Before proposing or continuing work, read `docs/PROJECT_STATUS.md` and the
`Work management` section of `CONTRIBUTING.md`. Current state is maintained only
in `project/registry.json`; generate the status view with
`python tools/check_project.py --write`.

Select a task ID, check prior evidence and name its increment and stop condition
before implementation or experiments. Historical acceptance is not undone by
omission from the latest run. A combination-evidence gap is not an unimplemented
feature. Reuse canonical code and existing acceptance tools. Reopening completed
work requires a concrete changed boundary or reproducible defect and prior
references. Keep one execution item in progress; candidates are not automatic
instructions to continue.

Keep architecture decisions in their existing GEN/CPH records. Core manages
sandboxes, not models, prompts or RL algorithms. Public applications use the
HTTPS API path; UDS remains development/internal. Preserve unrelated work and
host services. Close the selected task with scoped evidence, remaining limits
and, when applicable, proof that owned resources were retired. Run
`python tools/check_project.py` and `python tools/check_docs.py` for register or
documentation changes; additional checks depend on the actual changed boundary.
