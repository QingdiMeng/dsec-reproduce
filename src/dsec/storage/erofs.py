"""Publish a staged directory as an immutable, DAX-compatible EROFS layer.

This publisher deliberately disables compression and inline regular-file data.
It does not convert OCI whiteouts or fetch images; existing image publication
remains responsible for preparing the intended filesystem tree.
"""
import hashlib
import io
import os
from pathlib import Path
import tarfile
import subprocess
import tempfile

from dsec.contracts.layers import PROBE, PROBE_DATA, manifest

ALIGNMENT = 128*2**20


def build_layer(source, output, *, mkfs='mkfs.erofs'):
    source=Path(source).resolve(strict=True)
    output=Path(output).absolute()
    if not source.is_dir() or source==Path('/'):
        raise ValueError('Explicit staged layer directory required')
    if output.exists() or output.is_symlink():raise FileExistsError(output)
    if output.is_relative_to(source):raise ValueError('Output must be outside staged layer')
    if (source/PROBE).exists() or (source/PROBE).is_symlink():
        raise ValueError('Reserved DAX probe path already exists')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.erofs-dax-',dir=output.parent) as temporary:
        work=Path(temporary);archive=work/'tree.tar'
        # Preserve numeric uid/gid, hard links and symlinks without privileged
        # extraction or chown. Never flatten OCI layers with this directory API.
        with tarfile.open(archive,'w',format=tarfile.PAX_FORMAT,dereference=False) as stream:
            stream.add(source,arcname='.')
        return build_tar_layer(archive,output,mkfs=mkfs)




def build_tar_layer(archive, output, *, mkfs='mkfs.erofs'):
    """Publish an operator-prepared flat filesystem tar without extraction.

    Numeric ownership, hardlinks and PAX metadata remain in the archive. This
    function is not an OCI layer/whiteout converter. The caller supplies a flat
    rootfs tar (e.g. from a stopped docker-export container).
    """
    archive=Path(archive).resolve(strict=True)
    output=Path(output).absolute()
    if not archive.is_file():raise ValueError('Prepared flat filesystem tar required')
    if output.exists() or output.is_symlink():raise FileExistsError(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.erofs-dax-',dir=output.parent) as temporary:
        work=Path(temporary)
        prepared=work/'tree.tar'
        # Stream archive members without extracting any paths or changing uid.
        with tarfile.open(archive) as source,tarfile.open(prepared,'w',format=tarfile.PAX_FORMAT) as destination:
            for member in source:
                if member.name.lstrip('./')==PROBE:raise ValueError('Reserved DAX probe path already exists')
                stream=source.extractfile(member) if member.isfile() else None
                try:destination.addfile(member,stream)
                finally:
                    if stream is not None:stream.close()
            probe=tarfile.TarInfo(PROBE);probe.size=len(PROBE_DATA);probe.mode=0o444
            destination.addfile(probe,io.BytesIO(PROBE_DATA))
        image=work/'layer.erofs'
        subprocess.run([str(mkfs),'-b','4096','-E','noinline_data','--tar=f',str(image),str(prepared)],
                       check=True,timeout=600)
        # virtio-pmem capacity and guest struct-page allocation use this size.
        with image.open('r+b') as stream:
            stream.truncate(((image.stat().st_size+ALIGNMENT-1)//ALIGNMENT)*ALIGNMENT)
            stream.flush();os.fsync(stream.fileno())
        image.chmod(0o444)
        with image.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        with image.open('rb') as stream:
            stream.seek(1024)
            if stream.read(4)!=b'\xe2\xe1\xf5\xe0':raise ValueError('mkfs did not publish EROFS')
        os.link(image,output)  # Exclusive publication; never replace a live layer.
        directory=os.open(output.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    return dict(path=str(output.resolve()),sha256=digest,bytes=output.stat().st_size)


def guest_manifest(layers, mounts):
    return manifest(dict(schema=1,layers=[{key:layer[key] for key in ('sha256','bytes')} for layer in layers],
                         mounts=mounts))
