"""Small client for AgentENV's pinned uvm-ublk-daemon wire protocol.

The daemon and its Rust OverlayBD implementation own all block I/O.  This
module only sends the length-prefixed JSON control messages defined by
AgentENV storage/ublk-daemon/src/protocol.rs at 6cccaa7842bd5be2051111d4f74d9e37aa721244.
"""

import json
from pathlib import Path
import socket
import struct


MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class UblkDaemonError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"ublk daemon {status}: {message}")


class UblkDaemonClient:
    def __init__(self, socket_path):
        self.socket_path = Path(socket_path)

    @staticmethod
    def _recv_exact(stream, size):
        data = bytearray()
        while len(data) < size:
            chunk = stream.recv(size - len(data))
            if not chunk:
                raise EOFError("ublk daemon closed before completing response")
            data.extend(chunk)
        return bytes(data)

    def call(self, request, *, timeout=30):
        if not isinstance(request, dict) or not isinstance(request.get("kind"), str):
            raise ValueError("ublk daemon request requires a kind")
        body = json.dumps(request, separators=(",", ":")).encode()
        if len(body) > MAX_MESSAGE_BYTES:
            raise ValueError("ublk daemon request exceeds protocol limit")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(timeout)
            stream.connect(str(self.socket_path))
            stream.sendall(struct.pack(">I", len(body)) + body)
            length = struct.unpack(">I", self._recv_exact(stream, 4))[0]
            if length > MAX_MESSAGE_BYTES:
                raise ValueError("ublk daemon response exceeds protocol limit")
            response = json.loads(self._recv_exact(stream, length))
        if not isinstance(response, dict) or not isinstance(response.get("status"), str):
            raise ValueError("invalid ublk daemon response")
        if response["status"] in ("error", "terminal_error", "invalid_request"):
            raise UblkDaemonError(response["status"], response.get("message", ""))
        return response

    def create_runtime_device(self, source_image_config, global_config, runtime_dir,
                              *, read_only=False, requested_virtual_size=None,
                              known_source_virtual_size=None, allow_shrink=False,
                              runtime_upper_mode="logStructured", timeout=360):
        response = self.call({
            "kind": "create_overlaybd_runtime_device",
            "source_image_config": str(source_image_config),
            "global_config": str(global_config),
            "runtime_dir": str(runtime_dir),
            "read_only": read_only,
            "runtime_upper_mode": runtime_upper_mode,
            "requested_virtual_size": requested_virtual_size,
            "known_source_virtual_size": known_source_virtual_size,
            "allow_shrink": allow_shrink,
        }, timeout=timeout)
        if response["status"] != "overlaybd_runtime_device_created":
            raise ValueError(f"unexpected create response: {response['status']}")
        return response

    def restack_snapshot(self, dev_id, output_layer_path, *, timeout=360):
        response = self.call({"kind": "restack_snapshot", "dev_id": int(dev_id),
                              "output_layer_path": str(output_layer_path)}, timeout=timeout)
        if response["status"] != "restack_snapshot_created":
            raise ValueError(f"unexpected restack response: {response['status']}")
        return response

    def delete(self, dev_id):
        response = self.call({"kind": "delete", "dev_id": int(dev_id)})
        if response["status"] != "deleted":
            raise ValueError(f"unexpected delete response: {response['status']}")

    def get_features(self):
        response = self.call({"kind": "get_features"})
        if response["status"] != "features":
            raise ValueError(f"unexpected features response: {response['status']}")
        return int(response["flags"])
