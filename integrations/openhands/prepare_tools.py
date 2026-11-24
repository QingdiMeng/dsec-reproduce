"""Publish explicitly prepared portable Python/tools as a shared EROFS artifact.

No downloading, package installation, task execution or privileged mounting.
"""
import argparse
import json
from pathlib import Path
import tarfile
import tempfile

from dsec.storage.erofs import build_tar_layer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-root', type=Path, required=True)
    parser.add_argument('--dependencies', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--mkfs', default='mkfs.erofs')
    args = parser.parse_args()
    interpreter = args.python_root.resolve(strict=True)
    dependencies = args.dependencies.resolve(strict=True)
    if not (interpreter/'bin/python').is_file() or not dependencies.is_dir():
        raise ValueError('Supply the prepared portable Python root and target site-packages directory')
    if args.out.exists():
        raise FileExistsError(args.out)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.openhands-tools-', dir=args.out.parent) as root:
        archive = Path(root)/'tools.tar'
        with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT, dereference=False) as tar:
            # Layers compose at the guest root, not at the shared mountpoint.
            # Include its ancestors so the exact declared mount exists after
            # OverlayFS composition, with traversal permission for task users.
            for name in ('opt', 'opt/dsec-openhands'):
                entry = tarfile.TarInfo(name)
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o755
                tar.addfile(entry)
            tar.add(interpreter, arcname='opt/dsec-openhands/python')
            tar.add(dependencies, arcname='opt/dsec-openhands/site-packages')
        print(json.dumps(build_tar_layer(archive, args.out, mkfs=args.mkfs)))


if __name__ == '__main__':
    main()
