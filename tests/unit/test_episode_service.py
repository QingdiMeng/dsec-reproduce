"""Episode ownership survives component extraction and controlled contention."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from dsec.contracts.profiles import FrameworkProfile
from dsec.rollout.episode import Episode
from dsec.rollout.episodes import EpisodeService


class ObservedLock:
    """Observe contenders before lock admission without timing-based assertions."""
    def __init__(self):
        self.lock = asyncio.Lock()
        self.arrived = {name: asyncio.Event() for name in ('retry', 'stop')}

    async def __aenter__(self):
        name = asyncio.current_task().get_name()
        if name in self.arrived:
            self.arrived[name].set()
        await self.lock.acquire()

    async def __aexit__(self, *args):
        self.lock.release()


class EpisodeOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_action_and_queued_stop_share_one_commit_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            episode_id = 'a' * 32
            started, release = asyncio.Event(), asyncio.Event()
            calls = []
            case = self

            def durable():
                return json.loads((root / (episode_id + '.json')).read_text())

            class Sandbox:
                id = 'one-sandbox'

                async def run_shell(self, command, **kwargs):
                    record = durable()
                    case.assertEqual(record['state'], 'EXECUTING')
                    case.assertEqual(record['pending']['request_id'], kwargs['request_id'])
                    case.assertEqual(record['history'], [])
                    calls.append(command)
                    started.set()
                    await release.wait()
                    return {'exit_code': 0, 'output': 'once\n'}

                async def stop(self, **kwargs):
                    record = durable()
                    case.assertEqual(record['state'], 'STOPPING')
                    case.assertEqual(len(record['history']), 1)
                    case.assertEqual(record['pending']['request_id'], kwargs['request_id'])

            service = EpisodeService(SimpleNamespace(_transport=object()), state_dir=root)
            self.addCleanup(service.store.lock.close)
            episode = Episode(episode_id, 'generic-task', Sandbox(), FrameworkProfile(),
                              300, store=service.store)
            episode.lock = lock = ObservedLock()
            service.store.reserve(episode.record())
            service.rollouts[episode_id] = episode
            action = {'operation': 'step', 'args': {'rollout_id': episode_id,
                      'step_id': 0, 'action_id': 'once', 'command': 'effect'}}
            first = asyncio.create_task(service.dispatch(action))
            retry = stop = None
            try:
                await asyncio.wait_for(started.wait(), 5)
                retry = asyncio.create_task(service.dispatch(action), name='retry')
                await asyncio.wait_for(lock.arrived['retry'].wait(), 5)
                stop = asyncio.create_task(service.dispatch({'operation': 'stop',
                    'args': {'rollout_id': episode_id}}), name='stop')
                await asyncio.wait_for(lock.arrived['stop'].wait(), 5)
                self.assertFalse(retry.done())
                self.assertFalse(stop.done())
                release.set()
                first_result, replay, stopped = await asyncio.wait_for(
                    asyncio.gather(first, retry, stop), 5)
                self.assertFalse(first_result['replayed_result'])
                self.assertTrue(replay['replayed_result'])
                self.assertEqual(stopped['state'], 'STOPPED')
                self.assertEqual(calls, ['effect'])
                saved = durable()
                self.assertEqual(saved['state'], 'STOPPED')
                self.assertEqual(saved['next_step'], 1)
                self.assertEqual(len(saved['history']), 1)
                with self.assertRaisesRegex(RuntimeError, 'STOPPED'):
                    await service.dispatch({'operation': 'step', 'args': dict(
                        action['args'], step_id=1, action_id='late')})
                self.assertEqual(calls, ['effect'])
            finally:
                release.set()
                pending = [t for t in (first, retry, stop)
                           if t is not None and not t.done()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            service.store.lock.close()
            recovered = EpisodeService(SimpleNamespace(_transport=object()), state_dir=root)
            self.addCleanup(recovered.store.lock.close)
            await recovered.initialize()
            self.assertEqual(recovered.rollouts[episode_id].record(), saved)
