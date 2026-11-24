"""Local SGLang capture gateway for an external, single-GPU training recipe.

This is not a DSec service or an OpenHands agent implementation. It forwards
unchanged chat requests while retaining native sampled IDs and probabilities.
The shared GPU lease also lets a trainer explicitly pause inference between
requests while sandbox tools continue independently.
"""
import argparse
from contextlib import contextmanager
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.request
from uuid import uuid4

from .training import SampledGeneration


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value, allow_nan=False))
    temporary.chmod(0o600)
    temporary.replace(path)


@contextmanager
def gpu_lease(root, role):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    began = time.time()
    with (root/'gpu.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        acquired = time.time()
        try:
            yield
        finally:
            ended = time.time()
            with (root/'gpu-timeline.jsonl').open('a') as out:
                out.write(json.dumps(dict(role=role,waiting_since=began,acquired=acquired,
                                           released=ended))+'\n')
            fcntl.flock(lock, fcntl.LOCK_UN)


class NativeRecords:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.offsets = {}
        self.records = {}
        self.lock = threading.Lock()

    def get(self, identity, seconds=15):
        deadline = time.monotonic()+seconds
        while True:
            with self.lock:
                for path in sorted(self.directory.glob('*.log*')):
                    try:
                        stream=path.open()
                    except FileNotFoundError:
                        # A logger may rename the entry between glob and open.
                        continue
                    with stream:
                        stat=os.fstat(stream.fileno())
                        identity_key=(stat.st_dev,stat.st_ino)
                        offset=self.offsets.get(identity_key,0)
                        stream.seek(offset if offset<=stat.st_size else 0)
                        while True:
                            start=stream.tell()
                            line=stream.readline()
                            if not line or not line.endswith('\n'):
                                self.offsets[identity_key]=start
                                break
                            brace=line.find('{')
                            if brace < 0:
                                continue
                            event=json.loads(line[brace:])
                            if event.get('event')=='request.finished':
                                self.records[event['out']['meta_info']['id']]=event
                if identity in self.records:
                    return self.records.pop(identity)
            if time.monotonic()>deadline:
                raise TimeoutError('Native sampled-token record missing: '+identity)
            time.sleep(.05)


class TrainingGateway(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, *, upstream, model, root, native_logs):
        self.root=Path(root)
        self.upstream=upstream.rstrip('/')
        self.model=model
        self.native=NativeRecords(native_logs)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        super().__init__(address, TrainingHandler)


class TrainingHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != '/health':
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

    def do_POST(self):
        match=re.fullmatch(r'/episodes/([A-Za-z0-9_-]+)/v1/chat/completions',self.path)
        if not match:
            self.send_error(404)
            return
        request_id=uuid4().hex
        directory=self.server.root/'episodes'/match[1]/'generations'
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        record=dict(request_id=request_id,received=time.time())
        try:
            length=int(self.headers['Content-Length'])
            if not 0 < length <= 16*1024*1024:
                raise ValueError('Invalid request size')
            body=json.loads(self.rfile.read(length))
            if body.get('stream') or body.get('n',1)!=1 or body['model']!=self.server.model:
                raise ValueError('Single nonstreaming configured-model request required')
            record['request']=body
            with gpu_lease(self.server.root,'generate:'+match[1]):
                state=json.loads((self.server.root/'policy.json').read_text())
                if state.get('phase','ready')!='ready':
                    raise RuntimeError('Inference policy is not in a published ready state')
                forwarded=dict(body,logprobs=True,top_logprobs=0,return_token_ids=True)
                if state.get('adapter'):
                    forwarded['model']=self.server.model+':'+state['adapter']
                request=urllib.request.Request(self.server.upstream+'/v1/chat/completions',
                    data=json.dumps(forwarded).encode(),headers={'Content-Type':'application/json'})
                with self.server.opener.open(request,timeout=600) as response:
                    payload=response.read()
                value=json.loads(payload)
                record.update(response=value,effective_model=forwarded['model'],
                              policy_version=state['version'])
                native=self.server.native.get(value['id'])
                purpose='agent' if body.get('tools') else 'summary'
                sample=SampledGeneration.from_sglang(native,value,
                    policy_version=state['version'],purpose=purpose)
                record.update(sample=sample.as_dict(),native=native,response=value,
                              effective_model=forwarded['model'],completed=time.time())
                write_json(directory/(value['id']+'.json'),record)
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:
            record.update(error_type=type(exc).__name__,error=str(exc),completed=time.time())
            write_json(directory/('error-'+request_id+'.json'),record)
            self.send_error(502,'Exact training capture failed; inspect gateway evidence')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--native-logs',type=Path,required=True)
    parser.add_argument('--upstream',default='http://127.0.0.1:31026')
    parser.add_argument('--model',default='dsec-swegym-minicpm5-2b')
    parser.add_argument('--port',type=int,default=31027)
    args=parser.parse_args()
    server=TrainingGateway(('127.0.0.1',args.port),upstream=args.upstream,
        model=args.model,root=args.root,native_logs=args.native_logs)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.server_close()


if __name__=='__main__':
    main()
