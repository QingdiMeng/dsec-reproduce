"""SAO objective helpers for the optional external SWE-Gym training recipe.

Paper equations 1–5: detached direct IS weights, strict open interval and
token-level GAE across sampled actions. Observations and summaries remain
conditioning context; they are never policy targets.
"""
import torch


def skip_observation_gae(values, rewards, action_mask, *, gamma=1., lam=1.):
    if values.ndim != 1 or rewards.shape != values.shape or action_mask.shape != values.shape:
        raise ValueError('Aligned one-dimensional values, rewards and action mask required')
    if not 0 <= lam <= 1 or not 0 <= gamma <= 1:
        raise ValueError('Invalid GAE discount')
    positions = torch.nonzero(action_mask.bool(), as_tuple=False).flatten()
    if positions.numel() == 0:
        raise ValueError('GAE requires action tokens')
    advantages = torch.zeros_like(values)
    returns = torch.zeros_like(values)
    with torch.no_grad():
        next_value = values.new_zeros(())
        gae = values.new_zeros(())
        for position in reversed(positions):
            delta = rewards[position] + gamma * next_value - values[position]
            gae = delta + gamma * lam * gae
            advantages[position] = gae
            returns[position] = gae + values[position]
            next_value = values[position]
    return advantages, returns


def direct_is_loss(logprobs, behavior_logprobs, advantages, *, denominator,
                   lower=.2, upper=4.):
    """Coding-agent DIS band: (1-.8, 1+3); no PPO old-policy surrogate."""
    if not 0 < lower < upper or denominator <= 0:
        raise ValueError('Invalid DIS interval or normalization')
    if logprobs.shape != behavior_logprobs.shape or logprobs.shape != advantages.shape:
        raise ValueError('DIS tensors must align')
    delta = logprobs.detach().float() - behavior_logprobs.detach().float()
    # Compare in log space so excluded extreme ratios cannot overflow.
    import math
    kept = (delta > math.log(lower)) & (delta < math.log(upper))
    weights = torch.zeros_like(delta)
    weights[kept] = delta[kept].exp()
    loss = -(weights * advantages.detach() * logprobs).sum() / denominator
    return loss, dict(kept=int(kept.sum()), tokens=kept.numel())


def policy_lambda(action_tokens, alpha=1.5):
    if action_tokens < 1 or alpha < 1:
        raise ValueError('Invalid length-adaptive GAE scale')
    return 1. - 1. / (alpha * action_tokens)
