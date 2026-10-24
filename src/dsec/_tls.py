"""Reloadable verified TLS snapshots and pre-request certificate fencing."""
from dataclasses import dataclass
import hashlib
import http.client
import json
from pathlib import Path
import re
import ssl
import tempfile
import threading
import urllib.request

from dsec._security_files import read_bytes, VersionedFile


@dataclass(frozen=True)
class TLSView:
    context: object
    trust: str
    peers: tuple[str,...]
    generation: int
    leaf: str | None


class TLSFiles:
    def __init__(self,*,server=False,security_file=None,ca_file=None,cert_file=None,key_file=None,client_ca_file=None):
        self.server,self.reader=server,VersionedFile(security_file) if security_file else None
        self.static=dict(ca_file=ca_file,cert_file=cert_file,key_file=key_file,client_ca_file=client_ca_file)
        if self.reader and any(self.static.values()):raise ValueError('Use a security bundle or fixed TLS paths, not both')
        self.lock=threading.RLock();self.current=None;self.signature=None

    def _configuration(self,value):
        allowed={'generation','ca_file','cert_file','key_file','client_ca_file','peer_sha256'}
        if set(value)-allowed:raise ValueError('Unsupported TLS security fields')
        paths={key:value.get(key) for key in self.static}
        if any(p is not None and (not isinstance(p,str) or not Path(p).is_absolute()) for p in paths.values()):
            raise ValueError('TLS bundle paths must be absolute')
        peers=value.get('peer_sha256',[])
        if (not isinstance(peers,list) or len(peers)>16 or len(peers)!=len(set(peers)) or
                any(not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',v) for v in peers)):
            raise ValueError('Invalid certificate fingerprints')
        return paths,tuple(sorted(peers)),value['generation']

    def snapshot(self):
        with self.lock:
            paths,peers,generation=(self.reader.read(self._configuration) if self.reader else (self.static,(),0))
            if bool(paths['cert_file'])!=bool(paths['key_file']):raise ValueError('TLS certificate/key pair required')
            if self.server and (not paths['cert_file'] or paths['ca_file']):raise ValueError('Explicit server TLS certificate required')
            if not self.server and paths['client_ca_file']:raise ValueError('Client TLS uses ca_file')
            data={key:read_bytes(path,private=key=='key_file') if path else b'' for key,path in paths.items()}
            signature=hashlib.sha256(json.dumps({k:hashlib.sha256(v).hexdigest() for k,v in data.items()},sort_keys=True).encode()+repr(peers).encode()).hexdigest()
            if self.current is not None and self.reader and generation==self.current.generation and signature!=self.signature:
                raise ValueError('TLS material changed without a new publication generation')
            if self.current is not None and signature==self.signature and generation==self.current.generation:return self.current
            ca=data['client_ca_file' if self.server else 'ca_file']
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER) if self.server else ssl.create_default_context(cadata=ca.decode('ascii') if ca else None)
            context.minimum_version=ssl.TLSVersion.TLSv1_2
            if self.server and peers and not ca:raise ValueError('Pinned client identities require verified client TLS')
            if ca:
                if self.server:
                    context.load_verify_locations(cadata=ca.decode('ascii'));context.verify_mode=ssl.CERT_REQUIRED
            if data['cert_file']:
                with tempfile.TemporaryDirectory(prefix='dsec-tls-') as tmp:
                    for name,key in (('cert','cert_file'),('key','key_file')):
                        path=Path(tmp)/name;path.write_bytes(data[key]);path.chmod(0o600)
                    context.load_cert_chain(str(Path(tmp)/'cert'),str(Path(tmp)/'key'))
            trust=hashlib.sha256(ca).hexdigest()
            leaf=None
            if data['cert_file']:
                match=re.search(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',data['cert_file'].decode('ascii'),re.S)
                leaf=hashlib.sha256(ssl.PEM_cert_to_DER_cert(match.group())).hexdigest()
            self.current=TLSView(context,trust,peers,generation,leaf);self.signature=signature
            return self.current

    def fence(self,view,peer):
        current=self.snapshot()
        if current.trust!=view.trust:
            raise ssl.SSLError('TLS trust changed before request acceptance')
        if current.peers and (not peer or hashlib.sha256(peer).hexdigest() not in current.peers):
            raise ssl.SSLError('Peer certificate is not in the current identity set')


class ReloadingOpener:
    def __init__(self,files):
        self.files,self.lock,self.view,self.opener=files,threading.Lock(),None,None
        self._current()

    def _current(self):
        with self.lock:
            view=self.files.snapshot()
            if self.view is view:return self.opener
            files=self.files
            class Connection(http.client.HTTPSConnection):
                def connect(inner):
                    super().connect()
                    try:files.fence(view,inner.sock.getpeercert(binary_form=True))
                    except Exception:inner.close();raise
            class Handler(urllib.request.HTTPSHandler):
                def https_open(inner,request):return inner.do_open(Connection,request,context=view.context)
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self,*args,**kwargs):return None
            self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),Handler(context=view.context),NoRedirect())
            self.view=view
            return self.opener

    def open(self,*args,**kwargs):return self._current().open(*args,**kwargs)
