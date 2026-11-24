"""Discarded-gradient capacity probe on recorded native contexts, not RL data.

Historical generations without behavior logprobs must never be passed off as
SAO samples. This probe uses their original IDs with explicit diagnostic losses
to exercise the same native actor/critic engines and optimizer state allocation.
"""
import argparse
import json
import math
from pathlib import Path
import resource
import time
import urllib.request

import torch

from dsec_openhands.sglang_training import gpu_lease, write_json
from .sao_verl import (action_slice, install_single_rank_fused_boundary,
                       make_engine, segment_batch, values_for)


def optimizer_probe(engine, segments, *, kind):
    total = sum(len(s['response_ids']) for s in segments)
    with engine.train_mode():
        engine.optimizer_zero_grad()
        for index, segment in enumerate(segments):
            def objective(model_output, data, dp_group):
                if kind == 'actor':
                    # Diagnostic NLL; no invented historical behavior logprob.
                    loss = -action_slice(model_output, 'log_probs', segment).float().sum() / total
                else:
                    values = action_slice(model_output, 'values', segment).float()
                    loss = .5 * (values - 1.).square().sum() / total
                return loss, {}

            engine.forward_backward_batch(segment_batch(segment, fused=(kind == 'actor')),
                                           objective, forward_only=False)
            print(json.dumps(dict(stage=kind, segment=index + 1, segments=len(segments),
                prompt_tokens=len(segment['prompt_ids']), response_tokens=len(segment['response_ids']))), flush=True)
        norm = engine.optimizer_step()
        if not math.isfinite(norm):
            raise RuntimeError('Nonfinite diagnostic gradient')
        engine.lr_scheduler_step()
    torch.cuda.empty_cache()
    return dict(grad_norm=norm, action_tokens=total)


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--contexts', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--inference-root', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    data = json.loads(args.contexts.read_text())
    assert data['schema'] == 'dsec.sao.memory-probe.v1' and data['not_training_data'] is True
    segments = data['segments']
    assert segments and all(s['prompt_ids'] and s['response_ids'] for s in segments)
    report = dict(status='running', diagnostic_only=True, gradients_discarded=True,
        source=str(args.contexts), contexts=[dict(response_id=s['response_id'],
            prompt_tokens=len(s['prompt_ids']), response_tokens=len(s['response_ids'])) for s in segments])
    started = time.monotonic()
    torch.cuda.set_device(0)
    torch.distributed.init_process_group('nccl', init_method='file://' + str((args.out / 'rendezvous').resolve()),
                                        rank=0, world_size=1)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def control(method):
        request = urllib.request.Request('http://127.0.0.1:31026/' + method,
            data=json.dumps(dict(tags=['weights', 'kv_cache'])).encode(),
            headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=120) as response:
            response.read()

    released = False
    try:
        install_single_rank_fused_boundary()
        actor = make_engine(args.model, 'language_model', learning_rate=1e-6, steps=2)
        critic = make_engine(args.model, 'value_model', learning_rate=5e-6, steps=4)
        write_json(args.out / 'ready.json', dict(status='ready'))
        deadline = time.monotonic() + 600
        while not (args.out / 'start').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Inference handoff was not started')
            time.sleep(.2)
        with gpu_lease(args.inference_root, 'memory-probe'):
            control('release_memory_occupation'); released = True
            torch.cuda.reset_peak_memory_stats()
            initial_stats = torch.cuda.memory_stats()
            rounds = []
            for _ in range(2):
                values_for(critic, segments)
                critic_steps = [optimizer_probe(critic, segments, kind='critic') for _ in range(2)]
                values_for(critic, segments)
                actor_step = optimizer_probe(actor, segments, kind='actor')
                rounds.append(dict(critic_updates=critic_steps, actor_update=actor_step))
            torch.cuda.synchronize()
            report.update(rounds=rounds, peak_cuda_allocated_mib=torch.cuda.max_memory_allocated()/2**20,
                peak_cuda_reserved_mib=torch.cuda.max_memory_reserved()/2**20,
                allocator_retries=torch.cuda.memory_stats()['num_alloc_retries']-initial_stats['num_alloc_retries'])
            control('resume_memory_occupation'); released = False
        report['status'] = 'passed'
    except BaseException as exc:
        import traceback
        report.update(status='failed', error_type=type(exc).__name__, error=str(exc), traceback=traceback.format_exc())
        raise
    finally:
        if released:
            try: control('resume_memory_occupation')
            except Exception as exc: report['inference_resume_error'] = repr(exc)
        report['elapsed_s'] = time.monotonic()-started
        write_json(args.out/'report.json', report)
        torch.distributed.destroy_process_group()
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
