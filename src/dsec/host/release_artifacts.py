"""Offline wheel validation and installed payload identity; no runtime state."""
import ast
from email.parser import BytesParser
import hashlib
import io
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from dsec.contracts.release import release_ref
from dsec._release import payload_path, fingerprint, installed_release


def inspect_wheel(path, expected_sha256):
    path = Path(path)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError('Wheel SHA256 mismatch')
    with ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts for n in names):
            raise ValueError('Ambiguous or unsafe wheel paths')
        if sum(info.file_size for info in archive.infolist()) > 32*1024**2:
            raise ValueError('Unexpectedly large core wheel')
        metadata, = [n for n in names if n.endswith('.dist-info/METADATA')]
        package = BytesParser().parsebytes(archive.read(metadata))
        if package['Name'] != 'dsec-reproduce' or package.get_all('Requires-Dist'):
            # Optional Miles dependencies remain extra-qualified; no dependency
            # installation belongs to staging the core runtime.
            requirements = package.get_all('Requires-Dist', [])
            if package['Name'] != 'dsec-reproduce' or any('extra ==' not in item for item in requirements):
                raise ValueError('Expected the dependency-free DSec core wheel')
        assignment = next(node for node in ast.parse(archive.read('dsec/contracts/release.py')).body
                          if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='COMPATIBILITY' for t in node.targets))
        contract = ast.literal_eval(assignment.value)
        files = {n:archive.read(n) for n in names if payload_path(n)}
        return release_ref(dict(build=fingerprint(files),version=package['Version'],compatibility=contract))
