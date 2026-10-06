"""Fail if a development wheel includes sources outside the release allowlist."""

import argparse
from email.parser import BytesParser
from pathlib import Path
import tomllib
from zipfile import ZipFile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--project", type=Path, default=Path("pyproject.toml"))
    args = parser.parse_args()
    config = tomllib.loads(args.project.read_text())
    settings = config['tool']['setuptools']
    allowed = {name + ".py" for name in settings["py-modules"]}
    data = set()
    root = args.project.resolve().parent
    for package in settings.get('packages', []):
        directory = root/package.replace('.', '/')
        allowed.update(str(path.relative_to(root)) for path in directory.rglob('*.py'))
        for pattern in settings.get('package-data', {}).get(package, []):
            data.update(str(path.relative_to(root)) for path in directory.glob(pattern) if path.is_file())
    with ZipFile(args.wheel) as archive:
        names = set(archive.namelist())
        metadata_names = [name for name in names if name.endswith('.dist-info/METADATA')]
        if len(metadata_names) != 1:
            raise SystemExit('Missing or ambiguous wheel metadata')
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        expression = config['project'].get('license')
        if isinstance(expression, str) and metadata.get('License-Expression') != expression:
            raise SystemExit('Wheel license expression differs from project metadata')
        dist_info = metadata_names[0].rsplit('/', 1)[0]
        license_files = config['project'].get('license-files', settings.get('license-files', []))
        for name in license_files:
            path = dist_info+'/licenses/'+name
            if path not in names or archive.read(path) != (root/name).read_bytes():
                raise SystemExit('Missing or changed license file: '+name)
    sources = {name for name in names if name.endswith(".py")}
    if sources != allowed:
        raise SystemExit(f"Python sources mismatch: missing={sorted(allowed-sources)}, extra={sorted(sources-allowed)}")
    unexpected = names - allowed - data - {name for name in names if '.dist-info/' in name}
    if unexpected or data - names:
        raise SystemExit(f'Package data mismatch: missing={sorted(data-names)}, extra={sorted(unexpected)}')
    forbidden = ("experiments/", "results/", ".env", ".pem", ".key", "tb2-suite")
    leaked = sorted(name for name in names if any(token in name for token in forbidden))
    if leaked:
        raise SystemExit(f"Unexpected data in wheel: {leaked}")
    print(f"Wheel boundary OK: {len(sources)} runtime modules, {len(names)} entries")


if __name__ == "__main__":
    main()
