"""Split a pinned local image into pmem directory layers and a lean Docker archive.

Only stopped export containers touch host Docker. Task code is never executed on
this host. The original runtime Config is preserved; selected directory contents
are removed from the lean image and supplied by guest EROFS/OverlayFS mounts.
"""
import copy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from dsec.contracts.layers import manifest as layer_manifest
from dsec.storage.erofs import build_tar_layer


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def member_path(name):
    path=PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:raise ValueError('Noncanonical flat rootfs member')
    return '/'+str(path).removeprefix('./') if str(path)!='.' else '/'


def partition_rootfs(archive, targets, root_tar, layer_tars):
    """Stream a flattened export without extracting it or losing ownership."""
    layer_manifest(dict(schema=1,layers=[dict(sha256='0'*64,bytes=128*2**20)],mounts=list(targets)))
    if len(targets)!=len(layer_tars):raise ValueError('One tar per shared directory required')
    found=set();sizes=[0]*len(targets);retained=0
    from contextlib import ExitStack
    with ExitStack() as stack:
        source=stack.enter_context(tarfile.open(archive))
        root=stack.enter_context(tarfile.open(root_tar,'w',format=tarfile.PAX_FORMAT))
        layers=[stack.enter_context(tarfile.open(path,'w',format=tarfile.PAX_FORMAT)) for path in layer_tars]
        def owner(path):
            return next((index for index,target in enumerate(targets)
                         if path==target or path.startswith(target+'/')),None)
        for member in source:
            name=member_path(member.name);selected=owner(name)
            if name in targets:
                if not member.isdir():raise ValueError('Shared root must be a directory, not a link/file')
                found.add(name)
            if member.islnk() and owner(member_path(member.linkname))!=selected:
                raise ValueError('Hardlink crosses shared/private directory boundary')
            destinations=[]
            if selected is None or name in targets:destinations.append(root)
            for index,target in enumerate(targets):
                if selected==index or (member.isdir() and (name=='/' or target.startswith(name+'/'))):
                    destinations.append(layers[index])
            for destination in destinations:
                stream=source.extractfile(member) if member.isfile() else None
                try:destination.addfile(member,stream)
                finally:
                    if stream is not None:stream.close()
            if member.isfile():
                if selected is None:retained+=member.size
                else:sizes[selected]+=member.size
    if found!=set(targets):raise ValueError('Missing shared directory: '+repr(sorted(set(targets)-found)))
    return dict(retained_file_bytes=retained,shared_file_bytes=sizes)


def docker_archive(root_tar, original, output):
    """Write the standard docker-save manifest/config/layer structure."""
    config=dict(created=original['Created'],architecture=original['Architecture'],os=original['Os'],
                config=copy.deepcopy(original['Config']),rootfs=dict(type='layers',diff_ids=['sha256:'+digest(root_tar)]),
                history=[dict(created=original['Created'],created_by='DSec offline shared-directory split')])
    if original.get('Variant'):config['variant']=original['Variant']
    blob=json.dumps(config,sort_keys=True,separators=(',',':')).encode()
    image=hashlib.sha256(blob).hexdigest();config_name=image+'.json'
    manifest=json.dumps([dict(Config=config_name,RepoTags=[],Layers=['root/layer.tar'])],separators=(',',':')).encode()
    with tarfile.open(output,'w',format=tarfile.PAX_FORMAT) as stream:
        for name,data in ((config_name,blob),('manifest.json',manifest)):
            member=tarfile.TarInfo(name);member.mode=0o444;member.size=len(data);stream.addfile(member,io.BytesIO(data))
        stream.add(root_tar,arcname='root/layer.tar',recursive=False)
    return 'sha256:'+image


def split(image, output, layer_root, targets, *, mkfs='mkfs.erofs', reserve_gib=10):
    if not isinstance(image,str) or not re.fullmatch('sha256:[a-f0-9]{64}',image):
        raise ValueError('Exact local image ID required')
    targets=list(targets)
    layer_manifest(dict(schema=1,layers=[dict(sha256='0'*64,bytes=128*2**20)],mounts=targets))
    output=Path(output).absolute();layer_root=Path(layer_root)
    if output.exists() or output.is_symlink():raise FileExistsError(output)
    if not layer_root.is_absolute() or layer_root.is_symlink() or layer_root.resolve(strict=True)!=layer_root:
        raise ValueError('Canonical existing publication root required')
    if type(reserve_gib) is not int or reserve_gib<1:raise ValueError('Disk reserve required')
    inspected=json.loads(subprocess.check_output(['docker','image','inspect',image],text=True))[0]
    if inspected['Id']!=image or inspected['Architecture']!='amd64' or inspected['Os']!='linux':
        raise ValueError('Pinned Linux amd64 image required')
    if inspected['Config'].get('Volumes'):raise ValueError('Image-declared volumes require separate preparation')
    output.parent.mkdir(parents=True,exist_ok=True)
    required=reserve_gib*2**30+4*inspected['Size']
    if min(shutil.disk_usage(output.parent).free,shutil.disk_usage(layer_root).free)<required:
        raise RuntimeError('Image split would violate disk reserve')
    output.mkdir(mode=0o700)
    publication=layer_root/hashlib.sha256(str(output).encode()).hexdigest()[:24]
    publication.mkdir(mode=0o755)
    result=dict(schema=1,status='running',source_image=image,shared_mounts=targets,readonly_layers=[],
                source_runtime_config_sha256=hashlib.sha256(json.dumps(inspected['Config'],sort_keys=True).encode()).hexdigest())
    try:
        with tempfile.TemporaryDirectory(prefix='.rootfs-',dir=output) as temporary:
            work=Path(temporary);exported=work/'export.tar'
            cid=subprocess.check_output(['docker','create','--network','none','--read-only','--entrypoint','/bin/true',image],text=True).strip()
            try:
                with exported.open('wb') as stream:subprocess.run(['docker','export',cid],stdout=stream,check=True)
            finally:subprocess.run(['docker','rm',cid],check=True,stdout=subprocess.DEVNULL)
            root_tar=work/'private.tar';layer_tars=[work/f'layer-{index}.tar' for index in range(len(targets))]
            result.update(partition_rootfs(exported,targets,root_tar,layer_tars))
            result['flat_export_bytes']=exported.stat().st_size
            for index,path in enumerate(layer_tars):
                result['readonly_layers'].append(build_tar_layer(path,publication/f'layer-{index}.erofs',mkfs=mkfs))
            lean=output/'task-lean.tar';result['inner_image']=docker_archive(root_tar,inspected,lean)
            lean.chmod(0o444)
            result.update(task_archive=str(lean),task_archive_sha256=digest(lean),task_archive_bytes=lean.stat().st_size,status='passed')
    except BaseException as exc:
        result.update(status='failed',error=dict(type=type(exc).__name__,message=str(exc)));raise
    finally:(output/'split.json').write_text(json.dumps(result,indent=2)+'\n')
    return result
