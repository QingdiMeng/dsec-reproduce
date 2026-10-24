"""Installed package identity shared by service health; no Host composition."""
from functools import lru_cache
import hashlib
import importlib.metadata
import json
from pathlib import Path
from dsec.contracts.release import COMPATIBILITY, release_ref


def payload_path(name):
    # Ignore installer-created scripts, bytecode and RECORD. These entries
    # survive a wheel install byte-for-byte on every supported host.
    return (name.endswith('.py') or name.startswith('dsec/host/console/')
            or name.endswith('.dist-info/METADATA'))


def fingerprint(files):
    hashes = {name:hashlib.sha256(data).hexdigest() for name,data in files.items()}
    return hashlib.sha256(json.dumps(hashes,sort_keys=True,separators=(',', ':')).encode()).hexdigest()


@lru_cache(maxsize=1)
def installed_release():
    """Identity of loaded installed code; source checkout/old wheel returns None."""
    try:
        import dsec
        package = importlib.metadata.distribution('dsec-reproduce')
        if Path(package.locate_file('dsec/__init__.py')).resolve() != Path(dsec.__file__).resolve():
            return None
        files = {str(p):package.locate_file(p).read_bytes() for p in package.files if payload_path(str(p))}
        return release_ref(dict(build=fingerprint(files),version=package.version,compatibility=COMPATIBILITY))
    except (importlib.metadata.PackageNotFoundError, OSError):
        return None
