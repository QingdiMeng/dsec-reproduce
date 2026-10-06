"""MicroVM create admission must match one durable, active worker lease."""

import asyncio
import errno
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import uuid

from admission_guard import (AdmissionDenied, check_container_create,
                             check_create, container_worker_socket)
from container_backend import LayeredContainerBackend
from container_lifecycle_journal import ContainerLifecycleJournal
from request_journal import request_digest
from rollout_workerd import RolloutWorker
from work_scheduler import HostSample, ResourceBudget, WorkScheduler


class Sampler:
    def sample(self):
        return HostSample(0, 10000, 10000, 0, 0)


class Client:
    def __init__(self):
        self.probe = None

    async def run_microvm(self, spec, *, request_id):
        await self.probe("microvm", request_id, spec.service_args())
        return type("Sandbox", (), {"id": uuid.uuid4().hex[:12]})()

    async def run_container(self, spec, *, request_id):
        await self.probe("container", request_id, spec.lifecycle_args())
        return type("Sandbox", (), {"id": uuid.uuid4().hex[:12]})()


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_requires_matching_active_lease_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            budget = ResourceBudget(cpu=2, memory_mb=1024, disk_mb=2048,
                                    network_mbps=2, api_episode_slots=2,
                                    api_inflight=1, api_rpm=10, api_tpm=10000,
                                    min_memory_free_mb=0, min_disk_free_mb=0)
            scheduler = WorkScheduler(budget, Sampler())
            client = Client()
            worker = RolloutWorker(client, state_dir=Path(directory), scheduler=scheduler)
            rollout_id = uuid.uuid4().hex
            async def probe(backend, request_id, args):
                digest = request_digest("create", None, args)
                check = lambda value: worker.dispatch({"operation": "admission_check", "args": value})
                self.assertEqual(await check({"request_id": request_id,
                                              "digest": digest, "backend": backend}),
                                 {"admitted": True})
                self.assertEqual(await check({"request_id": uuid.uuid4().hex,
                                              "digest": digest, "backend": backend}),
                                 {"admitted": False})
                self.assertEqual(await check({"request_id": request_id,
                                              "digest": "0" * 64, "backend": backend}),
                                 {"admitted": False})
            # run_microvm is awaited by the worker, so this hook can inspect
            # the worker before its create request commits.
            client.probe = probe
            result = await worker.dispatch({"operation": "create", "args": {
                "task_id": "admission-test", "rollout_id": rollout_id}})
            self.assertEqual(result["state"], "ACTIVE")
            pending = worker.rollouts[rollout_id].pending
            self.assertIsNone(pending)
            self.assertEqual(await worker.dispatch({"operation": "admission_check", "args": {
                "request_id": uuid.uuid4().hex, "digest": "0" * 64}}),
                             {"admitted": False})
            container_id = uuid.uuid4().hex
            container = await worker.dispatch({"operation": "create", "args": {
                "task_id": "container-admission-test", "rollout_id": container_id,
                "profile": {"backend": "container", "environment": "erofs_overlay",
                            "storage": "local", "lifecycle": "stop"}}})
            self.assertEqual(container["state"], "ACTIVE")
            worker.store.lock.close()

    async def test_guard_fails_closed_and_checks_exact_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.sock"
            with self.assertRaises(AdmissionDenied):
                check_create(path, uuid.uuid4().hex, {"idle_ttl_seconds": 300})
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(path))
            except OSError as exc:
                listener.close()
                if exc.errno == errno.EPERM:
                    self.skipTest("Unix socket bind is blocked by the local sandbox")
                raise
            listener.listen(1)
            seen = []
            def respond():
                connection, _ = listener.accept()
                with connection:
                    seen.append(json.loads(connection.makefile("rb").readline()))
                    connection.sendall(b'{"ok":true,"result":{"admitted":true}}\n')
            thread = threading.Thread(target=respond)
            thread.start()
            request_id = uuid.uuid4().hex
            args = {"idle_ttl_seconds": 300}
            try:
                check_create(path, request_id, args)
            finally:
                thread.join(timeout=2)
                listener.close()
            self.assertEqual(seen[0]["args"], {"request_id": request_id,
                "digest": request_digest("create", None, args),
                "backend": "microvm"})

    async def test_container_config_fails_closed_when_broken(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(container_worker_socket(root))
            (root / ".admission-worker.json").write_text("not json")
            with self.assertRaises(AdmissionDenied):
                check_container_create(root, uuid.uuid4().hex, {})

    async def test_container_journal_and_backend_reject_before_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".admission-worker.json").write_text(json.dumps({
                "version": 1, "worker_socket": str(root / "missing.sock")}))
            journal = ContainerLifecycleJournal(root)
            request_id = uuid.uuid4().hex
            args = {"environment_id": "example", "storage": "local",
                    "memory_mb": 512, "cpus": 1.0, "qos": "default"}
            effects = []
            with self.assertRaises(AdmissionDenied):
                journal.execute(request_id, "create", None, args,
                                lambda: effects.append("created"))
            self.assertFalse(effects)
            self.assertFalse((root / "lifecycle-requests" / (request_id + ".json")).exists())
            backend = object.__new__(LayeredContainerBackend)
            backend.root, backend.environment_id, backend.storage = root, "example", "local"
            with self.assertRaises(AdmissionDenied):
                backend.create(memory_mb=512, cpus=1.0, sandbox_id=request_id)
            self.assertFalse((root / request_id).exists())
