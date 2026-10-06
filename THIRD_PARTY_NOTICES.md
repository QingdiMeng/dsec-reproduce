# Third-party code and external runtime artifacts

This is an independent reproduction, not the original DSec source release.
The project's own code and documentation use the MIT License in `LICENSE`,
selected by the project owner on 2026-10-06. The Miles-derived module below
retains its Apache-2.0 license and attribution. The wheel's combined license
expression is `MIT AND Apache-2.0` because it includes both sets of files;
this does not relicense the project's own code under Apache-2.0.

## Miles agent loop

`dsec_adapters/openenv_agent_function.py` derives from
`examples/experimental/openenv/openenv_agent_function.py` in
[radixark/miles](https://github.com/radixark/miles/tree/98942681e85ec38a312091771e04c1736ec583c3/examples/experimental/openenv).

- Reviewed upstream checkout: `98942681e85ec38a312091771e04c1736ec583c3`.
- Reviewed source SHA-256: `7822b9db51672e2e5fd4f3943ccbc451b5794bc7469f09fb5e657094a7921976`.
- Upstream license: Apache License 2.0, Copyright 2025 Zhipu AI.
- Complete upstream license text: `licenses/miles-APACHE-2.0.txt`.
- Local changes include optional standalone policy URL handling, an explicit
  policy-call hook, evaluator selection and verdict diagnostics. The DSec
  episode integration uses its own scheduled worker, not an OpenEnv service.
- The legacy experiment module is an import alias to the packaged derivative.

This records the reviewed reference version; it does not claim the derivative
is byte-identical to upstream. Subsequent changes must preserve these notices.

## External dependencies and artifacts

Firecracker and the guest Linux kernel, EROFS tooling, Rust OverlayBD/ublk,
3FS, Docker, Miles and model weights are separate dependencies. Their binaries,
repositories and weights are not included in the development wheel. Follow
their own pinned source licenses when building a complete deployment image;
the control-plane source notice does not grant redistribution rights to them.

Terminal-Bench-2.1 tasks are also separate data. The fixed acceptance trajectory
records the task revision `7131e4375048a0e408a8fb404b5f499d726b695b`; the wheel does
not redistribute the task suite, container images or verifier data. Diagnostic
cache manifests contain hashes and compatibility metadata, not wheel contents.

The source publication must exclude `.runtime/`, generated results, downloaded
repositories, credentials and VM/model artifacts. `tools/check_wheel.py` checks
the wheel's explicit source and data allowlist; it does not replace a final
source-tree publication review or full dependency license audit.
