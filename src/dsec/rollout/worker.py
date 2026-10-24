"""Compatibility facade for historical rollout worker imports and launchers.

Use EpisodeService for episode coordination. Worker names describe the process
role and legacy constructor, not another owner of state or a training agent loop.
"""
from dsec.rollout.episode import Episode, Episode as Rollout
from dsec.rollout.episodes import EpisodeService, container_environment_id
from dsec.rollout.store import EpisodeStore, EpisodeStore as RolloutStore
from dsec.compat.rollout_worker import RolloutWorker
from dsec.rollout.server import error_response, serve, main


if __name__ == "__main__":
    main()
