"""Internal worker-process composition, Unix RPC framing and CLI lifecycle.

Public applications use the authenticated API. This transport only composes
services; episode transitions live in EpisodeService, never in socket handlers.
"""
import argparse
import asyncio
from dataclasses import fields
import json
import os
from pathlib import Path
import signal
from dsec.sdk.development import DevEdgeClient as DSecClient
from dsec.contracts.resources import ResourceBudget
from dsec.compat.rollout_worker import RolloutWorker
from dsec.rollout.scheduler import WorkScheduler
from dsec.observability.shared import SharedServiceMonitor


def error_response(exc):
    """Preserve Edge error identity and verifier evidence across the worker hop."""
    error = {"type": getattr(exc, "kind", type(exc).__name__), "message": str(exc)}
    details = getattr(exc, "details", None)
    if isinstance(details, dict):
        error["details"] = details
    return {"ok": False, "error": error}


async def serve(socket_path, sandbox_socket, state_dir=None,
                scheduler_budget=None, metrics_port=None, tb2_tasks_dir=None):
    client = DSecClient(sandbox_socket)
    await client.open()
    scheduler = None
    shared_services = None
    if scheduler_budget is not None:
        settings = json.loads(Path(scheduler_budget).read_text())
        budget_keys = {field.name for field in fields(ResourceBudget)}
        budget = ResourceBudget(**{key: value for key, value in settings.items()
                                   if key in budget_keys})
        # Formal scheduled execution requires Edge-owned node leases. Never
        # silently instantiate another physical ledger against an old server.
        if 'node-admission-v1' not in client._features:
            raise RuntimeError('Upgrade sandbox service and configure --node-budget before starting a scheduled worker')
        if settings.get("shared_services") is not None:
            shared_services = SharedServiceMonitor(settings["shared_services"])
        scheduler = WorkScheduler(
            budget, None, node_status=client.node_status,
            dependency_ready=(shared_services.ready if shared_services is not None else None))
        await scheduler.refresh_node_status()
    elif metrics_port is not None:
        raise ValueError("Metrics endpoint requires a scheduler budget")
    worker = RolloutWorker(client, state_dir=state_dir, scheduler=scheduler,
                           tb2_tasks_dir=tb2_tasks_dir)
    await worker.initialize()
    worker.shared_services = shared_services
    if scheduler is not None:
        await scheduler.start()
    if shared_services is not None:
        await shared_services.start()
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        if worker.store is None:
            raise FileExistsError(f"Refusing to replace existing socket: {socket_path}")
        try:
            _, probe_writer = await asyncio.open_unix_connection(str(socket_path))
        except (OSError, ConnectionError):
            socket_path.unlink()  # State-directory lock excludes another worker.
        else:
            probe_writer.close()
            await probe_writer.wait_closed()
            raise FileExistsError(f"Another worker is serving: {socket_path}")

    handler_tasks = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handler_tasks.add(task)
        try:
            try:
                line = await reader.readline()
                if len(line) > 131072 or not line.endswith(b"\n"):
                    raise ValueError("Invalid request frame")
                request = json.loads(line)
                response = {"ok": True, "result": await worker.dispatch(request)}
            except Exception as exc:
                response = error_response(exc)
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            handler_tasks.discard(task)

    old_umask = os.umask(0o077)
    try:
        server = await asyncio.start_unix_server(handle, path=str(socket_path), limit=131073)
    finally:
        os.umask(old_umask)
    os.chmod(socket_path, 0o600)
    metrics_server = None
    if metrics_port is not None:
        async def handle_metrics(reader, writer):
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=2)
                if line.startswith(b"GET /metrics "):
                    body = (scheduler.prometheus_text()
                            + worker.resource_metrics_text()
                            + (shared_services.prometheus_text()
                               if shared_services is not None else "")).encode()
                    status = b"200 OK"
                else:
                    body = b"Not Found\n"
                    status = b"404 Not Found"
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/plain; version=0.0.4\r\n"
                             + b"Content-Length: " + str(len(body)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        metrics_server = await asyncio.start_server(handle_metrics, "127.0.0.1", metrics_port)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)
    print("READY " + str(socket_path), flush=True)
    try:
        await shutdown.wait()
    finally:
        server.close()
        for task in tuple(handler_tasks):
            task.cancel()
        if handler_tasks:
            await asyncio.gather(*tuple(handler_tasks), return_exceptions=True)
        await server.wait_closed()
        if metrics_server is not None:
            metrics_server.close()
            await metrics_server.wait_closed()
        socket_path.unlink(missing_ok=True)
        await worker.close_meters()
        if shared_services is not None:
            await shared_services.close()
        if scheduler is not None:
            await scheduler.close()
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--sandbox-socket", required=True)
    parser.add_argument("--state-dir")
    parser.add_argument("--scheduler-budget")
    parser.add_argument("--metrics-port", type=int)
    parser.add_argument("--tb2-tasks-dir")
    options = parser.parse_args()
    asyncio.run(serve(options.socket, options.sandbox_socket,
                      options.state_dir or str(Path(options.socket).parent / "rollouts"),
                      options.scheduler_budget, options.metrics_port,
                      options.tb2_tasks_dir))


if __name__ == "__main__":
    main()
