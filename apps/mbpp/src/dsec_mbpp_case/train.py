"""Launch native verl Qwen3.5-2B LoRA/GRPO pilots or complete MBPP epochs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from . import dataset, reward

VERL_REVISION = "8718ca30a3f002f93b7c4fd99b9b2506718681bc"
VERL_SOURCE_SHA256 = "2811dc7f02a4f79ac5428fcd5e925fa41922e9af8bfbb6182b83cf4d394b6d55"


def verify_source(root):
    # Also supports an archive checkout without Git metadata. Cover executable
    # trainer/backend source, configuration and the complete dependency lock.
    files = [root / "pyproject.toml", root / "uv.lock",
             *sorted((root / "verl").rglob("*.py")),
             *sorted((root / "verl/trainer/config").rglob("*.yaml"))]
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    if digest.hexdigest() != VERL_SOURCE_SHA256:
        raise ValueError("verl source/configuration differs from the supported revision " + VERL_REVISION)


def plan(args):
    manifest = json.loads((args.data / "manifest.json").read_text())
    if manifest["source_sha256"] != dataset.SOURCE_SHA256 or manifest["reference_solutions_included"] is not False:
        raise ValueError("Training requires the prepared original MBPP data without reference solutions")
    for split in {"train", args.evaluation_split}:
        for suffix in ("parquet", "jsonl"):
            name = split + "." + suffix
            if hashlib.sha256((args.data / name).read_bytes()).hexdigest() != manifest["files"][name]:
                raise ValueError("Prepared dataset changed: " + name)
        ids = [json.loads(line)["extra_info"]["task_id"] for line in
               (args.data / (split + ".jsonl")).read_text().splitlines()]
        low, high = dataset.SPLITS[split]
        if sorted(ids) != list(range(low, high + 1)) or len(ids) != manifest["counts"][split]:
            raise ValueError("Prepared split is incomplete or duplicated: " + split)
    train_count = manifest["counts"]["train"]
    if args.epochs and train_count % args.batch_size:
        raise ValueError("Full epochs require a batch size dividing the train count; verl drops the final partial batch")
    steps = (train_count // args.batch_size) * args.epochs if args.epochs else (args.steps or 1)
    return dict(steps=steps, epochs=args.epochs or 1, full_epochs=bool(args.epochs),
                train_tasks=train_count, evaluation_split=args.evaluation_split,
                evaluation_tasks=min(args.validation_samples, manifest["counts"][args.evaluation_split]),
                rollouts_per_task=8, seed=args.seed, response_length=args.response_length,
                generation_concurrency=args.generation_concurrency)


def overrides(args, agent_config):
    scope = plan(args)
    settings = {
        "algorithm.adv_estimator": "grpo", "algorithm.use_kl_in_reward": False,
        "data.train_files": str(args.data / "train.parquet"),
        "data.val_files": str(args.data / (args.evaluation_split + ".parquet")),
        "data.train_batch_size": args.batch_size, "data.val_batch_size": args.evaluation_batch_size,
        "data.val_max_samples": args.validation_samples,
        "data.max_prompt_length": args.prompt_length, "data.max_response_length": args.response_length,
        "data.filter_overlong_prompts": False, "data.truncation": "error", "data.seed": args.seed,
        "data.dataloader_num_workers": 0,
        "+data.apply_chat_template_kwargs.enable_thinking": False,
        "actor_rollout_ref.model.path": str(args.model),
        "actor_rollout_ref.model.lora_rank": 8, "actor_rollout_ref.model.lora_alpha": 16,
        "actor_rollout_ref.model.target_modules": ["gate_proj", "up_proj", "down_proj"],
        "actor_rollout_ref.model.enable_gradient_checkpointing": True,
        "actor_rollout_ref.model.use_remove_padding": True,
        "actor_rollout_ref.model.use_fused_kernels": True,
        "actor_rollout_ref.model.fused_kernel_options.impl_backend": "triton",
        "actor_rollout_ref.actor.strategy": "fsdp2",
        "actor_rollout_ref.actor.optim.lr": 3e-6,
        "actor_rollout_ref.actor.ppo_mini_batch_size": args.batch_size,
        "actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu": 1,
        "actor_rollout_ref.actor.use_dynamic_bsz": False,
        "actor_rollout_ref.actor.use_torch_compile": False,
        "actor_rollout_ref.actor.use_kl_loss": True,
        "actor_rollout_ref.actor.kl_loss_coef": .001,
        "actor_rollout_ref.actor.kl_loss_type": "low_var_kl",
        "actor_rollout_ref.actor.entropy_coeff": 0,
        "actor_rollout_ref.actor.fsdp_config.model_dtype": "bf16",
        "actor_rollout_ref.actor.fsdp_config.param_offload": True,
        "actor_rollout_ref.actor.fsdp_config.optimizer_offload": True,
        "actor_rollout_ref.ref.strategy": "fsdp2",
        "actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu": 1,
        "actor_rollout_ref.ref.use_torch_compile": False,
        "actor_rollout_ref.ref.fsdp_config.param_offload": True,
        "actor_rollout_ref.rollout.name": "sglang",
        "actor_rollout_ref.rollout.n": 8,
        "actor_rollout_ref.rollout.temperature": .7,
        "actor_rollout_ref.rollout.top_p": .8,
        "actor_rollout_ref.rollout.top_k": 20,
        "actor_rollout_ref.rollout.prompt_length": args.prompt_length,
        "actor_rollout_ref.rollout.response_length": args.response_length,
        "actor_rollout_ref.rollout.max_model_len": args.prompt_length + args.response_length,
        "+actor_rollout_ref.rollout.engine_kwargs.sglang.random_seed": args.seed,
        "actor_rollout_ref.rollout.max_num_seqs": args.generation_concurrency,
        "actor_rollout_ref.rollout.tensor_model_parallel_size": 1,
        "actor_rollout_ref.rollout.gpu_memory_utilization": .4,
        "actor_rollout_ref.rollout.enforce_eager": True,
        "actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu": 1,
        "actor_rollout_ref.rollout.agent.num_workers": 4,
        "actor_rollout_ref.rollout.agent.default_agent_loop": "dsec_mbpp_single_turn",
        "actor_rollout_ref.rollout.agent.agent_loop_config_path": str(agent_config),
        "actor_rollout_ref.rollout.val_kwargs.do_sample": True,
        "actor_rollout_ref.rollout.val_kwargs.n": 8,
        "actor_rollout_ref.rollout.val_kwargs.temperature": .7,
        "actor_rollout_ref.rollout.val_kwargs.top_p": .8,
        "actor_rollout_ref.rollout.val_kwargs.top_k": 20,
        "reward.num_workers": 8,
        "reward.custom_reward_function.path": reward.__file__,
        "reward.custom_reward_function.name": "compute_score",
        "+reward.custom_reward_function.reward_kwargs.worker_socket": args.worker_socket,
        "+reward.custom_reward_function.reward_kwargs.environment_id": args.environment_id,
        "+reward.custom_reward_function.reward_kwargs.evidence_dir": str(args.out / "execution-evidence"),
        "trainer.n_gpus_per_node": 1, "trainer.nnodes": 1,
        "trainer.logger": ["console"], "trainer.project_name": "dsec_mbpp_verl",
        "trainer.experiment_name": args.out.name, "trainer.resume_mode": "disable",
        "trainer.total_training_steps": scope["steps"], "trainer.total_epochs": scope["epochs"],
        "trainer.val_before_train": args.evaluate_before_train,
        "trainer.test_freq": scope["steps"], "trainer.save_freq": args.checkpoint_every or scope["steps"],
        "trainer.max_actor_ckpt_to_keep": 1,
        "trainer.default_local_dir": str(args.out / "checkpoints"),
        "trainer.rollout_data_dir": str(args.out / "rollouts"),
        "trainer.validation_data_dir": str(args.out / "validation"),
        "ray_kwargs.ray_init.runtime_env.py_executable": None,
        "+ray_kwargs.ray_init.object_store_memory": 536870912,
        "+ray_kwargs.ray_init.include_dashboard": False,
    }
    return [key + "=" + json.dumps(value) for key, value in settings.items()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("verl-root", "model", "data", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("worker-socket", "environment-id"):
        parser.add_argument("--" + name, required=True)
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument("--steps", type=int, help="bounded pilot; default one update")
    duration.add_argument("--epochs", type=int, help="complete epochs with no dropped train tasks")
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--validation-samples", type=int, default=4)
    parser.add_argument("--evaluation-split", choices=["validation", "test"], default="validation")
    parser.add_argument("--evaluation-batch-size", type=int, default=4)
    parser.add_argument("--evaluate-before-train", action="store_true")
    parser.add_argument("--prompt-length", type=int, default=1024)
    parser.add_argument("--response-length", type=int, default=4096)
    parser.add_argument("--generation-concurrency", type=int, default=16,
                        help="maximum simultaneous sequences in the generation backend")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--checkpoint-every", type=int, default=0, help="periodic checkpoints; zero saves only the final update")
    parser.add_argument("--dry-run", action="store_true", help="compose the complete native Hydra config; no GPU run")
    args = parser.parse_args()
    if (min(args.steps or 1, args.epochs or 1, args.batch_size, args.validation_samples,
            args.evaluation_batch_size, args.prompt_length, args.response_length,
            args.generation_concurrency) < 1 or
            args.steps == 0 or args.epochs == 0 or args.checkpoint_every < 0 or args.seed < 0):
        parser.error("training/evaluation sizes must be positive, checkpoint interval and seed nonnegative")
    for name in ("verl_root", "model", "data"):
        setattr(args, name, getattr(args, name).resolve(strict=True))
    args.out = args.out.resolve()
    verify_source(args.verl_root)
    for name in ("train.parquet", args.evaluation_split + ".parquet"):
        if not (args.data / name).is_file():
            parser.error("Missing prepared dataset: " + name)
    scope = plan(args)
    args.out.mkdir(parents=True, exist_ok=False)
    agent_config = args.out / "agent-loop.json"
    agent_config.write_text(json.dumps([{"name": "dsec_mbpp_single_turn",
                                       "_target_": "dsec_mbpp_case.verl_agent.MBPPSingleTurnAgentLoop",
                                       "evidence_dir": str(args.out / "generation-evidence")}]))
    command = [sys.executable, "-m", "verl.trainer.main_ppo", *overrides(args, agent_config)]
    if args.dry_run:
        command.extend(["--cfg", "job", "--resolve"])
    (args.out / "launch.json").write_text(json.dumps({"verl_revision_required": VERL_REVISION,
            "command": command, "dry_run": args.dry_run, "scope": scope}, indent=2))
    environment = dict(os.environ, TOKENIZERS_PARALLELISM="false")
    # Selecting a venv interpreter does not activate its command-line tools.
    # FlashInfer invokes Ninja through PATH, including in subprocess Ray workers.
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", os.defpath)
    # Prevent inherited proxy settings from proxying local Ray/SGLang control RPCs.
    addresses = json.loads(subprocess.check_output(["ip", "-j", "address", "show"], text=True))
    local_ips = [a["local"] for item in addresses for a in item.get("addr_info", [])]
    bypass = [environment.get("NO_PROXY", environment.get("no_proxy", "")),
              "localhost", "127.0.0.1", "::1", *local_ips]
    environment["NO_PROXY"] = environment["no_proxy"] = ",".join(x for x in bypass if x)
    with (args.out / "trainer.log").open("x") as log:
        result = subprocess.run(command, cwd=args.verl_root, env=environment, stdout=log, stderr=subprocess.STDOUT)
    print(json.dumps({"exit_code": result.returncode, "log": str(args.out / "trainer.log"), "dry_run": args.dry_run}))
    raise SystemExit(result.returncode)
