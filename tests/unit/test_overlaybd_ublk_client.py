import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest

from overlaybd_ublk_client import UblkDaemonClient, UblkDaemonError


class UblkProtocolTest(unittest.TestCase):
    def _exchange(self, reply, request):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daemon.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            captured = []

            def serve():
                with server:
                    connection, _ = server.accept()
                    with connection:
                        size = struct.unpack(">I", connection.recv(4))[0]
                        payload = bytearray()
                        while len(payload) < size:
                            payload.extend(connection.recv(size - len(payload)))
                        captured.append(json.loads(payload))
                        body = json.dumps(reply).encode()
                        # Split the frame to exercise the client's exact-read loop.
                        frame = struct.pack(">I", len(body)) + body
                        connection.sendall(frame[:5])
                        connection.sendall(frame[5:])

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                result = request(UblkDaemonClient(path))
            finally:
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            return result, captured[0]

    def test_runtime_device_request_matches_agentenv_wire_format(self):
        reply = {"status": "overlaybd_runtime_device_created", "dev_id": 17,
                 "device_path": "/dev/ublkb17", "actual_virtual_size": 4096,
                 "runtime_image_config_path": "/tmp/runtime/image.json"}
        result, sent = self._exchange(
            reply, lambda client: client.create_runtime_device(
                "/tmp/source/image.json", "/tmp/global.json", "/tmp/runtime"))
        self.assertEqual(result["device_path"], "/dev/ublkb17")
        self.assertEqual(sent["kind"], "create_overlaybd_runtime_device")
        self.assertEqual(sent["runtime_upper_mode"], "logStructured")
        self.assertIsNone(sent["requested_virtual_size"])

    def test_terminal_restack_failure_is_not_mistaken_for_snapshot(self):
        reply = {"status": "terminal_error", "message": "upper seal outcome unknown"}
        with self.assertRaises(UblkDaemonError) as caught:
            self._exchange(reply, lambda client: client.restack_snapshot(3, "/tmp/diff.commit"))
        self.assertEqual(caught.exception.status, "terminal_error")


if __name__ == "__main__":
    unittest.main()
