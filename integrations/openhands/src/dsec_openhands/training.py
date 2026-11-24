"""Exact sampled generations at an external inference boundary.

SDK display text is deliberately insufficient for this contract. Each action
retains its own native prompt: tool serialization and condensation need not
preserve a token-prefix relationship between consecutive calls.
"""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class SampledGeneration:
    response_id: str
    policy_version: int
    prompt_ids: list[int]
    response_ids: list[int]
    behavior_logprobs: list[float]
    purpose: str

    def __post_init__(self):
        if not self.response_id or type(self.policy_version) is not int or self.policy_version < 0:
            raise ValueError('Generation identity and nonnegative policy version required')
        if self.purpose not in ('agent', 'summary'):
            raise ValueError('Unknown generation purpose')
        if not self.prompt_ids or not self.response_ids:
            raise ValueError('Native prompt and response IDs required')
        if any(type(x) is not int or x < 0 for x in [*self.prompt_ids, *self.response_ids]):
            raise ValueError('Invalid native token ID')
        if len(self.response_ids) != len(self.behavior_logprobs):
            raise ValueError('One behavior logprob per sampled token required')
        if any(not math.isfinite(x) or x > 1e-4 for x in self.behavior_logprobs):
            raise ValueError('Invalid behavior logprob')

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_sglang(cls, event, response, *, policy_version, purpose):
        """Accept original IDs/logprobs, never re-encode parser-rendered text."""
        out = event['out']
        meta = out['meta_info']
        if response['id'] != meta['id']:
            raise ValueError('Response/native generation identity mismatch')
        ids = out['output_ids']
        if ids != response['choices'][0]['response_token_ids']:
            raise ValueError('Response/native sampled IDs mismatch')
        entries = meta['output_token_logprobs']
        if [x[1] for x in entries] != ids:
            raise ValueError('Native behavior logprob IDs mismatch')
        if meta['completion_tokens'] != len(ids):
            raise ValueError('Incomplete sampled generation')
        return cls(response['id'], policy_version, event['obj']['input_ids'],
                   ids, [x[0] for x in entries], purpose)


def episode_actions(records, completion_ids):
    """Select the SDK's actual calls and exclude auxiliary summary generations."""
    if len(set(completion_ids)) != len(completion_ids):
        raise ValueError('Duplicate SDK completion identity')
    index = {record.response_id: record for record in records}
    if len(index) != len(records):
        raise ValueError('Duplicate sampled generation identity')
    selected = [index[identity] for identity in completion_ids]
    actions = [record for record in selected if record.purpose == 'agent']
    if not actions:
        raise ValueError('No exact agent generation available for training')
    return actions
