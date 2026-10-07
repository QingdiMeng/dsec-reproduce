"""Preserve verl's native single-turn token path with explicit sampling settings.

This optional module is imported only by a separately installed pinned verl.
It does not add verl as a core or application installation dependency.
"""
import json
from contextvars import ContextVar
import os
from pathlib import Path
import time
from uuid import uuid4

from verl.experimental.agent_loop.single_turn_agent_loop import SingleTurnAgentLoop


class _AuditedServer:
    def __init__(self, server):
        self.server = server
        self.last_request = ContextVar("mbpp_generation_request", default=None)

    def __getattr__(self, name):
        return getattr(self.server, name)

    async def generate(self, **kwargs):
        self.last_request.set({"prompt_ids": kwargs["prompt_ids"],
                               "sampling_params": dict(kwargs["sampling_params"])})
        output = await self.server.generate(**kwargs)
        output.extra_fields["dsec_sampling_params"] = dict(kwargs["sampling_params"])
        output.extra_fields["dsec_finish_reason"] = output.stop_reason
        return output


class MBPPSingleTurnAgentLoop(SingleTurnAgentLoop):
    def __init__(self, *args, evidence_dir=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_manager = _AuditedServer(self.server_manager)
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None

    async def run(self, sampling_params, priority=0, **kwargs):
        params = dict(sampling_params, presence_penalty=1.5, min_p=0.0, repetition_penalty=1.0)
        identity = uuid4().hex
        started = time.monotonic()
        result = None
        record = {"schema": "dsec.mbpp.generation.v1", "generation_id": identity,
                  "sampling_params": params, "priority": int(priority)}
        try:
            result = await super().run(params, priority=priority, **kwargs)
            result.extra_fields["dsec_response_tokens"] = len(result.response_ids)
            result.extra_fields["dsec_generation_id"] = identity
            record.update(prompt_ids=result.prompt_ids, response_ids=result.response_ids,
                          response_mask=result.response_mask,
                          response_logprobs=result.response_logprobs,
                          finish_reason=result.extra_fields["dsec_finish_reason"])
            return result
        except Exception as exc:
            record.update(error_type=type(exc).__name__, error=str(exc),
                          request=self.server_manager.last_request.get())
            raise
        finally:
            record["elapsed_seconds"] = time.monotonic()-started
            if self.evidence_dir:
                self.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.evidence_dir / (identity + ".json")
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(record, stream, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
