"""Build a networkless smoke guest from an explicitly pinned local image."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile


def build(image, source, output):
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('Supply an exact local Docker image ID')
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    actual = subprocess.check_output(['docker', 'image', 'inspect', image,
                                      '--format', '{{.Id}}'], text=True).strip()
    if actual != image:
        raise ValueError('Image identity differs')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.dsec-guest-', dir=output.parent) as temp:
        work = Path(temp)
        root = work/'root'
        root.mkdir()
        cid = subprocess.check_output(['docker', 'create', '--network', 'none', image, '/bin/true'],
                                      text=True).strip()
        try:
            with (work/'root.tar').open('wb') as stream:
                subprocess.run(['docker', 'export', cid], stdout=stream, check=True)
        finally:
            subprocess.run(['docker', 'rm', cid], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['tar', '--no-same-owner', '-xf', str(work/'root.tar'), '-C', str(root)], check=True)
        if not (root/'bin/sh').exists() and not (root/'bin/sh').is_symlink():
            raise ValueError('Trusted base image needs /bin/sh')
        subprocess.run(['gcc', '-static', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-o', str(root/'dsec-agent'), str(source)], check=True)
        for name in ('proc', 'sys', 'dev', 'tmp'):
            (root/name).mkdir(exist_ok=True)
        (root/'tmp').chmod(0o1777)
        init = root/'dsec-init'
        init.write_text('#!/bin/sh\nset -eu\nmount -t proc proc /proc\n'
                        'mount -t sysfs sysfs /sys\nexec /dsec-agent\n')
        init.chmod(0o755)
        disk = work/'guest.ext4'
        with disk.open('wb') as stream:
            stream.truncate(64*1024**2)
        subprocess.run(['mkfs.ext4', '-q', '-F', '-d', str(root), str(disk)], check=True)
        disk.chmod(0o444)
        # This output is a prepared immutable template, not a running VM disk.
        # Keep it on the same filesystem and publish without replacing a file.
        import os
        os.link(disk, output)
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'status':'passed', 'template':str(output), 'sha256':digest,
            'source_image_id':image, 'source_agent_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'bytes':output.stat().st_size, 'network':False, 'task':'counter-example'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--agent-source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.image, args.agent_source, args.out)))


if __name__ == '__main__':
    main()
