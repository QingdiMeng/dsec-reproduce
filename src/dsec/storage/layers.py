"""Local immutable OverlayBD checkpoint objects with durable per-sandbox holds."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid

from dsec._persistence import atomic_json


class SharedSnapshotLayers:
    def __init__(self, root, share_directory):
        self.root = Path(root)/'.fork-layers'
        self.objects = self.root/'objects'
        self.references = self.root/'references'
        self.share_directory = share_directory
        self.lock = threading.RLock()
        for path in (self.root, self.objects, self.references):
            path.mkdir(mode=0o700, exist_ok=True)
            share_directory(path)

    def owns(self, path):
        path = Path(path)
        return path.parent == self.objects and re.fullmatch(r'[0-9a-f]{64}\.commit', path.name) is not None

    def validate(self, path):
        path = Path(path)
        if not self.owns(path) or path.is_symlink() or not path.is_file():
            raise ValueError('Invalid shared checkpoint object: '+str(path))
        return path

    def _reference(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r'[0-9a-f]{12}', sid):
            raise ValueError('Invalid shared checkpoint owner')
        return self.references/(sid+'.json')

    def hold(self, sid, paths):
        with self.lock:
            paths = sorted({str(self.validate(p)) for p in paths})
            file = self._reference(sid)
            previous = json.loads(file.read_text()) if file.exists() else []
            atomic_json(file, sorted(set(previous+paths)))

    def publish_and_hold(self, sid, layer, digest):
        """Copy once at seal time; never mutate the episode's existing snapshot."""
        self._reference(sid)
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid shared checkpoint digest')
        target = self.objects/(digest+'.commit')
        with self.lock:
            if not target.exists():
                temp = self.objects/('.pending-'+uuid.uuid4().hex)
                try:
                    checksum = hashlib.sha256()
                    with Path(layer).open('rb') as src, temp.open('xb') as dst:
                        while block := src.read(1024*1024):
                            checksum.update(block)
                            dst.write(block)
                        dst.flush()
                        os.fsync(dst.fileno())
                    if checksum.hexdigest() != digest:
                        raise ValueError('Checkpoint layer changed during publication')
                    self.share_directory(temp, mode=0o660)
                    os.replace(temp, target)
                    fd = os.open(self.objects, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    temp.unlink(missing_ok=True)
            self.hold(sid, [target])
        return target

    def release(self, sid):
        with self.lock:
            self._reference(sid).unlink(missing_ok=True)
            self._gc()

    def _gc(self):
        keep = set()
        for file in self.references.glob('*.json'):
            keep.update(json.loads(file.read_text()))
        for path in self.objects.glob('*.commit'):
            self.validate(path)
            if str(path) not in keep:
                path.unlink()
        for directory in (self.objects, self.references):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def reconcile(self, active_ids):
        """Only an exclusive durable manager calls this before serving requests."""
        with self.lock:
            for file in self.references.glob('*.json'):
                if file.stem not in active_ids:
                    file.unlink()
            for temp in self.objects.glob('.pending-*'):
                if temp.is_symlink() or not temp.is_file():
                    raise ValueError('Invalid pending checkpoint object')
                temp.unlink()
            self._gc()
