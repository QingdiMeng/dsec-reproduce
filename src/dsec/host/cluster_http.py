"""Bounded TLS JSON ingress shared by controllers and node gateways."""
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import socketserver
import ssl
import threading
from dsec._tls import TLSFiles


class ClusterHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 32

    def __init__(self, address, dispatch, *, cert_file=None, key_file=None,
                 client_ca_file=None, node_token=None, max_requests=32,
                 allow_loopback_http=False, console=False, security_file=None):
        if type(max_requests) is not int or not 1 <= max_requests <= 512:
            raise ValueError('Invalid request bound')
        if not (cert_file or security_file) and not (allow_loopback_http and address[0] in ('localhost', '127.0.0.1', '::1')):
            raise ValueError('Remote server requires TLS')
        if bool(cert_file) != bool(key_file):
            raise ValueError('TLS certificate/key pair required')
        self.context = None
        self.tls=None
        if cert_file or security_file:
            self.tls=TLSFiles(server=True,cert_file=cert_file,key_file=key_file,client_ca_file=client_ca_file,security_file=security_file)
            self.context=self.tls.snapshot().context
        if node_token is not None and (self.context is None or self.context.verify_mode!=ssl.CERT_REQUIRED or
                (not hasattr(node_token,'accepts') and (not isinstance(node_token,str) or len(node_token)<32))):
            raise ValueError('Node gateway requires controller mTLS and a strong internal credential')
        if type(console) is not bool or (console and node_token is not None):
            raise ValueError('Console may only be enabled on a controller')
        self.console = console
        self.dispatch, self.node_token = dispatch, node_token
        self.slots = threading.BoundedSemaphore(max_requests)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def security_status(self):
        view=self.tls.snapshot() if self.tls else None
        return dict(tls_generation=view.generation if view else 0,leaf_sha256=view.leaf if view else None,
            trust_sha256=view.trust if view else None,peer_sha256=list(view.peers) if view else [],
            node_auth_generation=getattr(getattr(self.node_token,'reader',None),'generation',None))


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        self.tls_view=self.server.tls.snapshot() if self.server.tls else None
        if self.tls_view:
            self.request = self.tls_view.context.wrap_socket(self.request, server_side=True)
        super().setup()

    def finish(self):
        try:
            super().finish()
        finally:
            if self.server.context:
                # wrap_socket moved the descriptor out of socketserver's raw
                # socket. Close the owned TLS socket explicitly, not via GC.
                self.request.close()

    def log_message(self, *_):
        # Credentials, commands and raw task material are not access logs.
        pass

    def do_GET(self):
        from dsec.host.cluster_console import HEADERS, asset
        content = asset(self.path) if self.server.console else None
        if content is None:
            self.send_error(404)
            return
        data, mime = content
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        for name, value in HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def do_POST(self):
        operation_id = None
        try:
            if self.path != '/v1/call' or self.headers.get('Transfer-Encoding'):
                raise ValueError('Unsupported request framing')
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1:
                raise ValueError('One Content-Length required')
            length = int(lengths[0])
            if not 0 < length <= 131072:
                raise ValueError('Invalid request size')
            credentials = self.headers.get_all('Authorization', [])
            if len(credentials) != 1 or not credentials[0].startswith('Bearer '):
                raise PermissionError('Authentication required')
            token = credentials[0][7:]
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete request body')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('Object request required')
            operation_id = payload.get('request_id')
            if self.tls_view:self.server.tls.fence(self.tls_view,self.request.getpeercert(binary_form=True))
            verifier=self.server.node_token
            if verifier is not None and not (verifier.accepts(token) if hasattr(verifier,'accepts') else hmac.compare_digest(token,verifier)):
                raise PermissionError('Controller identity rejected')
            if verifier is not None and payload.get('operation')=='node_security':
                result=self.server.security_status()
            else:result = self.server.dispatch(token, payload)
            if payload.get('operation')=='health' and isinstance(result,dict):
                result=dict(result,transport_security=self.server.security_status())
            response = dict(request_id=operation_id, ok=True, result=result)
        except Exception as exc:
            kind = getattr(exc, 'kind', type(exc).__name__)
            response = dict(request_id=operation_id, ok=False, error=dict(type=kind, message=str(exc),
                details=getattr(exc, 'details', {})))
        data = json.dumps(response, separators=(',', ':')).encode()
        if len(data) > 8*1024**2:
            data = json.dumps(dict(request_id=operation_id, ok=False,
                error=dict(type='RequestOutcomeUnknown', message='Oversized response; query committed request'))).encode()
        try:
            self.send_response(200)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
        except (OSError, ConnectionError):
            pass
