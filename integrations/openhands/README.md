# OpenHands on the DSec API

**English** · [简体中文](README.zh-CN.md)

## Current integration: SDK v1.58.2

The current controller uses **`openhands-sdk==1.58.2` and
`openhands-tools==1.58.2`**. Install the core wheel first, then this optional
integration in a dedicated Python 3.12+ environment:

```sh
python -m pip install -c integrations/openhands/sdk-constraints.txt './integrations/openhands[sdk]'
```

Do not install `[sdk]` and `[legacy]` in the same environment: they own different
generations of the `openhands` namespace. The SDK adapter is `dsec_openhands.sdk`;
the Runtime adapter described below remains for replaying old experiments.

The official SDK Agent/Conversation owns prompting, native tool calls, history,
Thinking events and stuck detection. Official Terminal and FileEditor executors
run inside the task sandbox, with their native action and observation models
transmitted through DSec HTTPS API → Node/Edge → native HTTP on guest loopback.
Descriptions come from those guest tools; controller factories cannot construct
a local task executor. The guest uses the upstream persistent subprocess/PTY
backend, explicit soft timeouts and serialized actions. Editor undo stays in
the guest executor.
The SDK rejects bare newline-separated commands with a native error observation;
use its documented `&&`/`;` chains or execute a script. The adapter never rewrites
model commands to bypass this rule.

`create_conversation` requires a fresh controller process and empty private HOME.
Its local workspace holds conversation metadata only; inherited command,
file-transfer and Git operations reject host access. The SWE-Gym CLI creates
this process before importing the SDK, preventing ambient personal plugins and
hooks from participating. Model credentials remain in the controller and are
never sent in guest configuration or tool requests. Transport failure fences
further tools and aborts the episode; incomplete results are UNKNOWN.
Interrupt uses a separate native session and HTTP route so neither the SDK's
per-session lock nor the guest action lock can queue it behind a blocked command.
For the pinned subprocess backend, the adapter sends its documented `C-c` key
through the PTY; upstream's process-group signal can target the shell instead
of the foreground job. A regression requires early completion with a nonzero
exit code, rather than merely accepting an interrupt response. Agent actions
and observations still use the unchanged official executor.
The caller owns sandbox retirement. Persisted conversations are evidence;
automatic resume onto another sandbox/tool-process epoch is unsupported.

The published per-call time ceiling is included in the tool description.
Requests above it receive a recoverable error observation before execution;
they do not get silently clamped or classified as UNKNOWN. Genuine transport
failure fences all tool workers and aborts at the controller's event boundary.
Workers never call `pause()` while the controller may hold its state lock.

The generic factory accepts an optional upstream `condenser`. The SWE-Gym
profile uses `sdk_condenser.EvidenceSummarizingCondenser`, a narrow extension
of the pinned SDK's summarizing condenser. Selection thresholds, atomic event
boundaries and summary placement remain upstream. Summary inputs use complete
native event JSON, including commands/editor arguments, structured Thinking,
tool output tails, call IDs and error/exit status. They never use the SDK's
500-character display previews. The unchanged public task instruction is also
included in every summary chunk and merge: upstream's retained initial window
otherwise excludes it from the summarizer's input. Inputs over the reserved token budget are
split and summarized in bounded stages; original events remain on disk and
completion logs retain every summary input. Summaries remain model-generated,
fallible memory: instructions distinguish reading a historical diff from
applying it and require evidence for changes and test results. No extra task
commands, source edits or model sampling changes are made by the condenser.

Version 3 also honors upstream's shrinking hard-reset preview budget without
discarding evidence. Preview strings are used only to count the intended token
budget; complete records/fragments are sent in correspondingly smaller chunks.
They are not replaced by truncated previews. Empty summaries fail the current
attempt immediately, allowing upstream recovery before generating later chunks
that would otherwise be discarded. Default triggers and model sampling stay
unchanged. This fixes the version-2 recovery boundary where smaller preview
limits did not reduce actual request sizes.

Harness-owned identity checks, Git inventory and candidate capture use DSec's
non-PTY command API in that same sandbox. They do not parse terminal screen
control sequences as JSON, and never change the model's upstream tool feedback.

Publish portable Python plus SDK/tools 1.58.2 dependencies using
`prepare_tools.py` below. Replace the old ACI tool layer in a new immutable
environment, retaining the Linux `pip freeze` and layer digest as evidence.
No dependency installation occurs during episodes. Controller and guest both
reject a different SDK version.

For OpenAI-compatible aliases, `sdk_llm.EndpointLLM` supports explicit
`thinking_history='current_user_turn'` (Qwen template policy) or
`'all_assistant_turns'` (DeepSeek Thinking). Only the SDK's structured reasoning
field is retained at serialization; agent history and response parsing remain
upstream. Completion logs/events are evaluation evidence, not exact-token and
logprob tensors for SAO. Browser, host plugins, image viewing, arbitrary client
tools and cross-epoch resume are not claimed.

The optional SWE-Gym application uses `dsec-swegym-openhands` for this SDK and
`dsec-swegym-openhands-legacy` for the old fork. Frozen tasks and official scoring
stay unchanged; modern-SDK results are distinct from the original agent baseline.

### Optional recovery of unparsed MiniCPM tool markup

`create_conversation(..., unparsed_tool_recovery='minicpm5', recovery_limit=3)`
opts into the pinned SDK's native Stop hook. If an assistant text reply contains
unparsed MiniCPM `<function name="...">` markup, the hook returns feedback that
no operation from that reply ran and lists the actual advertised tools. The SDK
retains the original assistant message and adds its environment feedback before
continuing the same conversation. This does not alias unknown tool names, parse
commands into actions, or execute response text. Structured tool calls retain
upstream validation; normal final answers and fenced/inline examples can stop.

The hook permits at most three recovery feedbacks by default (configurable
between one and ten), within the original iteration and episode limits. An
invalid last reply can still end when those limits are exhausted; it must not be
reported as evidence that the requested code repair was completed. The SWE-Gym
runner labels such a finished episode `unparsed_tool_call` while still obtaining
its actual official score. Hook execution failure aborts the episode rather than
becoming a model reward. `tool-recovery.json` and upstream HookExecutionEvents
record attempts and hook outcomes. This option is disabled by default and must
be identical for baseline, training and after-evaluation.

The hook subprocess is a fixed Python metadata reader in the dedicated
controller; model text never becomes its command. Task tools continue to execute
only in the DSec sandbox. This is an optional agent integration policy, not a
DSec core mechanism or a general detector for every model's tool syntax.

### Experimental exact-token training boundary

The optional SWE-Gym SAO pilot additionally runs
`python -m dsec_openhands.sglang_training` on controller loopback. It captures
SGLang's native input/output token IDs and behavior logprobs, matches them to
the SDK response ID, and records the published policy version. A mismatch fails
the request; parsed display text is never re-tokenized as a substitute.
Condenser requests are recorded separately and excluded from policy targets.
Each generation retains its own exact prompt, including any summarized history.

This is a training application's local inference boundary, not a DSec API
service or an alternative agent loop. Its file lease coordinates a single GPU
between generation and an external learner; it does not provide distributed
model serving. See the [SAO pilot scope](../../apps/swegym/README.md#sao-integration-pilot-experimental)
for paper alignment, local deviations and acceptance scope. Normal SDK
completion/event evidence alone is still insufficient for training.

## Legacy Runtime integration

This optional integration implements the OpenHands Runtime boundary. The core
does not install OpenHands, a benchmark, a model or a trainer. `DSecRuntime`
contains no task instructions, evaluator, model-response parser or agent loop.
It is usable by another harness that supplies a published coding environment.

The reference agent is SWE-Gym's OpenHands fork at
`e644a2ca45c3623b27a7e6c169e3d479f0a87fbc` (CodeActAgent 2.2).
[SWE-Gym recommends this fork](https://github.com/SWE-Gym/SWE-Gym/blob/main/docs/OpenHands.md).
[SAO](https://arxiv.org/html/2607.07508) names OpenHands but does not identify an
exact commit. This is a traceable integration, not an assertion that these are
the authors' exact source/dependency versions.

## Boundaries

| Owner | Responsibilities |
| --- | --- |
| Upstream OpenHands | System prompt, CodeAct tools/parsing, history, controller, stuck detection |
| This integration | Action/observation mapping, stable action IDs, runtime capabilities, sandbox ownership |
| DSec | IAM/admission, sandbox creation, native sessions/files/HTTP, mutation evidence, isolation and retirement |
| Harness | Dataset split, public instruction, environment selection, patch/artifact collection and grading |
| External model/trainer | Inference, model configuration, exact sampled tokens/logprobs and RL updates |

The product path is OpenHands Runtime → DSec HTTPS API → Node/Edge → sandbox.
There is no UDS or controller-local execution fallback. A benchmark harness
need not implement shell parsing or take over OpenHands' conversation history.

## Install the controller adapter

Install the core wheel first. In a separate Python 3.12 environment:

```sh
python -m pip install -c integrations/openhands/upstream-constraints.txt './integrations/openhands[legacy]'
```

The upstream old dependency lock includes a LiteLLM wheel no longer obtainable
from PyPI. The package pins its official source at the corresponding tag commit
instead. `e2b==0.17.1` is explicitly pinned because automatic range selection
rejects the old yanked release; DSec does not use that cloud provider. Constraints
come from the selected fork's poetry.lock. Keep the resolved installation
manifest with acceptance evidence. Dependencies and their own licenses remain
external; do not install this full controller package inside the task image.

## Publish the coding tools separately

The prepared sandbox needs an immutable tool directory at
`/opt/dsec-openhands`, containing a portable Linux **Python 3.12** under `python/`
and the dependencies in `guest-requirements.txt` under `site-packages/`.
Build the wheelhouse outside episodes, pin all artifacts, then publish this
directory using the existing uncompressed EROFS/DAX layer builder and add it
to the environment's shared mounts. Supply the portable interpreter's native
libraries as part of the tool artifact. Neither episode startup nor the adapter
downloads dependencies. Task commands retain the image's original interpreter;
tool Python variables are removed before starting the task shell.

Given those prepared directories, the source-release publisher writes their
paths under `opt/dsec-openhands/` in the root filesystem layer:

```sh
python integrations/openhands/prepare_tools.py \
  --python-root /srv/build/portable-python-3.12 \
  --dependencies /srv/build/openhands-site-packages \
  --out /srv/dsec/layers/openhands-tools.erofs \
  --mkfs /usr/local/bin/mkfs.erofs
```

Keep the returned path/hash/size in the environment's pinned `readonly_layers`,
and add `/opt/dsec-openhands` to `shared_mounts` when preparing its guest template.
The tar is a guest-root filesystem tree, not contents relative to the mountpoint.
Publishing a layer does not itself update a running Edge or its trusted catalog.

This companion is execution support, not an agent: a real PTY runs commands,
ipykernel maintains Python state, and the unmodified `openhands_aci.editor`
implements file editing and undo. It listens only on guest loopback, reached
through DSec's native HTTP operation. No host port or new privileged helper is
installed. Sandbox retirement owns all companion/kernel processes.
The runtime also runs the pinned upstream `INIT_COMMANDS` unchanged, including
Git identity and `git --no-pager`. Background-launch signal inheritance and
environment activation must not disable SIGINT or corrupt terminal framing.
Each new bash action is evaluated as one complete script in that persistent
shell, so multiline commands and heredocs produce one completion with the final
exit code. Per-line prompts must never shift output into a later action. Input
sent to an already-running foreground process remains raw stdin. Regression
checks cover both the script result and the following command's output identity.

The coding profile supports persistent cwd/environment, foreground stdin,
nonblocking poll, SIGINT, persistent IPython/editor state, bounded file transfer,
file line ranges and directory listing. Browser, VSCode and recursive archive
transfer are not advertised. FnCall reset semantics cannot replace a persistent
coding workspace. This first adapter creates Container sandboxes.

## Use from a harness

Construct the upstream EventStream/config and pass a runtime to the upstream
controller. A representative runtime configuration is:

```python
runtime = DSecRuntime(
    config=openhands_config,
    event_stream=event_stream,
    sid=conversation_id,
    headless_mode=True,
    plugins=agent.sandbox_plugins,
    dsec_config={
        "api_config": "/run/dsec/client.json",
        "spec": {"environment_id": "published-coding-environment"},
        "resource_demand": {"cpu": 1, "memory_mb": 1280, "disk_mb": 2048, "network_mbps": 0},
        "cwd": "/workspace",
        "shell_init": "",  # explicit environment-specific initialization
        "command_timeout_ms": 45000,
        "transport_timeout_ms": 120000,
    },
)
await runtime.connect()
```

Both budgets must fit the operator-published guest/catalog ceiling. The HTTP
budget includes kernel startup and interrupt settlement; it must exceed the tool
budget by at least 10 seconds and be at least 35 seconds. Native HTTP support is
required. Unknown calls are not replayed. OpenHands event IDs map to stable DSec
mutation IDs; the guest companion checks payload identity and a process epoch.
If the companion restarts, lost interactive state is rejected rather than
silently recreated. Explicitly close the runtime and check cleanup evidence.
Closing first fences new adapter calls, joins submitted SDK work, then requests
retirement; cancelling an upstream awaiter does not revoke that work. Only a
stop explicitly rejected before admission may retry with its original ID.
Unknown outcomes remain sticky, including on a repeated/atexit close.

The optional [SWE-Gym application](../../apps/swegym/README.md) owns its separate
runner and fresh official verification. Its frozen-model completion logs are
evaluation evidence, not accepted SAO token/logprob tensors. Exact training
records remain an external model/trainer integration requirement.

## Verify another environment

The opt-in Linux conformance test is independent of SWE-Gym. Supply the runtime
configuration above as JSON, plus a new evidence directory, and run it from the
repository with the controller environment installed:

```sh
DSEC_OPENHANDS_ACCEPTANCE_CONFIG=/run/dsec/coding-runtime.json \
DSEC_OPENHANDS_ACCEPTANCE_OUT=/tmp/dsec-openhands-check-01 \
python -m unittest discover -s integrations/openhands/tests -p test_linux_runtime.py -v
```

It creates and retires one sandbox. It checks persistent shell/Python state,
stdin/poll/interrupt, timeout settlement, the real upstream editor and undo,
native file ranges, upstream Git initialization and cancellation/retirement
ordering. Keep the operation IDs and cleanup evidence in its report.
Run the same check when changing the agent version, backend or environment;
then validate the benchmark's separate baseline/oracle and actual model score.
Passing these coding capabilities does not imply browser, snapshot, FnCall,
another agent version, or RL token/logprob compatibility.

## Extending the integration

Keep the two adaptation points independent:

| Change | Place to adapt | Checks to retain |
| --- | --- | --- |
| Another agent | Its optional runtime/action/observation adapter | Execution semantics, request identity, cancellation and ownership |
| Another benchmark harness | Its optional application | Public task setup, candidate artifacts, independent official grading |
| Another execution backend | Published DSec environment and capability profile | The same conformance tests, real isolation and retirement evidence |
| An RL trainer | External trainer/application connector | Exact sampled tokens, behavior logprobs, masks and policy versions |

The first OpenHands adapter creates and owns its sandbox. Attaching to a
harness-owned sandbox is not implemented; such an extension must explicitly
separate detaching a session from stopping its owner's sandbox. Likewise a
stateless FnCall cannot be substituted for a persistent coding session. Add
capabilities when an integration requires them and verify their semantics;
do not infer compatibility from shared method names or backend labels.
