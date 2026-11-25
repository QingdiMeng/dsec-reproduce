<a id="最新安装候选的短-grpo-与分叉验收"></a>

# Short GRPO run and fork acceptance on the installed candidate

**English** · [简体中文](DSEC_V01_GRPO_ACCEPTANCE.md)

The end-to-end regression for source-release-r5 passed on 2026-10-06. It used the frozen core wheel `11b2df99f8e305c332da1dd8d2b7adce6a3a25a1d2866adea6875c296e7eef5b`. All 55 installed files matched. The independent daemon, worker and client in the training container loaded from that installation. No old source checkout served as the runtime. Production services and permissions were preserved.

<a id="训练与轨迹"></a>

## Training and trajectories

Qwen3.5-4B ran in Thinking mode with a 32768-token response limit and a 65536-token context limit, using the previous precise-coding preset `temperature=0.6, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=0.0, repetition_penalty=1.0`. Miles/Megatron LoRA and SGLang took turns on a single 16 GiB GPU. Trainer memory patches remained separate experiment dependencies outside the DSec core package.

GRPO used two groups of two independent rollouts at concurrency 2. The TB2.1 task was `openssl-selfsigned-cert` at official revision `7131e4375048a0e408a8fb404b5f499d726b695b`. The experiment allowed at most 16 tool calls per task, 120 seconds per command and 1200 seconds per episode.

| Group | Official rewards | Gradient norm |
| --- | --- | ---: |
| First group | [1, 0] | 0.265217 |
| Second group | [0, 0] | 0 |

Training exited with code 0 after **466.712 seconds**. Between the two checkpoints, 96 adapter tensors changed and all had finite values. The first group supplied a learning signal. The second had uniform rewards. These data do not establish improved learning. The four responses contained 3307, 5420, 3078 and 4171 tokens. Logprob and mask lengths matched the trajectories. No responses were truncated, no samples were dropped and there were no token-in/token-out (TITO) mismatches.

Each episode actually paused after its first tool execution. The original VMM exited and a subsequent tool call resumed in a new PID. Sandbox identity, generation and step order remained continuous. Each trajectory had independent writable state. Inference resumed after training and the second group completed. Sampled GPU usage peaked at **14268 MiB**. The training container's memory peaked at **24.716 GiB**. This figure only covers the training container.

All three official zero scores were traced to a failed `test_python_verification_script` assertion. The verifier was present. Two trajectories ended with `invalid_format`, including one that had already completed the task and earned an official score of 1. The other two reached max_turns. The report retains these termination reasons because a positive task score does not establish full compliance with the reply protocol.

<a id="跨-episode-准备态复用"></a>

## Prepared-state reuse across episodes

The independent `tools/verify_installed_fork.py` acceptance entry point passed using the same installation. A sealed baseline contained a running HTTP service and its memory state. Two fresh episodes started at the same counter value with separate histories and private writes. The first branch earned an official score of 1. The baseline could still fork after both services restarted. Restarting the services for a running branch preserved its VMM and committed action result. The source baseline was stopped while the second branch remained alive. That branch then paused and resumed. Shared objects were reclaimed when the last holder released them.

This verifies prepared-state isolation and lifecycle behavior. It does not establish 60 ms fork latency or performance at scale. VMM resource samples from the fork probe include intervals when the sandbox was paused and no process was running. They are unsuitable for backend cost comparisons.

<a id="回收与证据"></a>

## Cleanup and evidence

All 50 sandbox records ended in STOPPED. Lease and pending counts were zero. No network namespaces, private service sockets, devices or active process references remained. The temporary unit files were restored. Both original user services were active. Host Docker, its existing containers, the bridge and Grafana were unchanged. GPU usage returned to 35 MiB and 0% utilization. Approximately 79.8 GiB of disk space remained.

Raw model replies, TITO, official CTRF/verifier/reward records, worker records, recovery events, two adapter checkpoints, training logs and the final audit were saved separately. Root-private records were exported through a read-only mount, preserving permissions on the source logs. Evidence resides in `.runtime/release-v01-final/grpo-evidence/` and is excluded from the public source and wheel.

This run closed the short-RL and reuse acceptance gap for this candidate. It did not test all 89 tasks, long-term training gains, real verl/Uni-Agent training or a clean deployment on a second host.
