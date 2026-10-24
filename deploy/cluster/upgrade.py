"""Reviewable offline API image context and pinned rolling/rollback overlays.

Requires the installed core release tooling. Never calls Docker or changes the
deployment, credentials, services, data volumes or etcd membership.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from dsec.contracts.release import same_contract
from dsec.host.release_artifacts import inspect_wheel


def write(path,value):path.write_text(json.dumps(value,indent=2)+'\n')


def context(deployment,wheel,sha256,out):
    deployment,wheel,out=Path(deployment).resolve(strict=True),Path(wheel).resolve(strict=True),Path(out).resolve()
    release=inspect_wheel(wheel,sha256)
    metadata=json.loads((deployment/'deployment.json').read_text())
    if not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}',metadata['python_image']):raise ValueError('Pinned Python base image required')
    files={name:(deployment/'image'/name).read_bytes() for name in ('Dockerfile','initialize.py')}
    for name,data in files.items():
        if hashlib.sha256(data).hexdigest()!=metadata['source_sha256'][name]:raise ValueError('Deployment image source changed')
    previous=os.umask(0o077)
    try:
        out.mkdir(mode=0o700,parents=True,exist_ok=False);(out/'wheels').mkdir()
        for name,data in files.items():(out/name).write_bytes(data)
        (out/'wheels'/wheel.name).write_bytes(wheel.read_bytes())
        inspect_wheel(out/'wheels'/wheel.name,sha256)
        write(out/'release.json',release)
        write(out/'build.json',dict(python_image=metadata['python_image'],wheel_sha256=sha256,release=release))
    finally:os.umask(previous)
    return dict(directory=str(out),release=release,services_changed=False)


def overlays(deployment,source,target,source_image,target_image,out):
    deployment,out=Path(deployment).resolve(strict=True),Path(out).resolve()
    same_contract(source,target)
    if source==target or source_image==target_image:raise ValueError('Distinct source/target runtime and image identities required')
    for value in (source_image,target_image):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',value):raise ValueError('Immutable local Docker image IDs required')
    configuration=json.loads((deployment/'config/controller.json').read_text())
    if not configuration.get('node_lifecycle'):raise ValueError('Managed membership required')
    previous=os.umask(0o077)
    try:
        out.mkdir(mode=0o700,parents=True,exist_ok=False)
        for role in ('api1','api2'):
            for action,image in (('upgrade',target_image),('rollback',source_image)):
                write(out/f'{action}-{role}.json',dict(services={role:dict(image=image,pull_policy='never')}))
        write(out/'plan.json',dict(source=source,target=target,source_image=source_image,target_image=target_image,
            unchanged_sha256={name:hashlib.sha256((deployment/name).read_bytes()).hexdigest()
                              for name in ('compose.yaml','config/controller.json','deployment.json')},
            order=['api1','verify authenticated API health and ledger','api2','verify authenticated API health and ledger'],
            no_automatic_rollback=True,etcd_upgrade=False,services_changed=False))
    finally:os.umask(previous)
    return dict(directory=str(out),services_changed=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    for name in ('context','plan'):
        command=sub.add_parser(name);command.add_argument('--deployment',type=Path,required=True);command.add_argument('--out',type=Path,required=True)
        if name=='context':command.add_argument('--wheel',type=Path,required=True);command.add_argument('--sha256',required=True)
        else:
            command.add_argument('--source-release',type=Path,required=True);command.add_argument('--target-release',type=Path,required=True)
            command.add_argument('--source-image',required=True);command.add_argument('--target-image',required=True)
    args=parser.parse_args()
    if args.action=='context':result=context(args.deployment,args.wheel,args.sha256,args.out)
    else:result=overlays(args.deployment,json.loads(args.source_release.read_text()),json.loads(args.target_release.read_text()),args.source_image,args.target_image,args.out)
    print(json.dumps(result))


if __name__=='__main__':main()
