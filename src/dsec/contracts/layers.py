"""Portable immutable-layer guest manifest, shared by publication and runtime."""
from pathlib import PurePosixPath
import re

PROBE = '.dsec-dax-probe'
PROBE_DATA = b'DSec EROFS DAX v1\n' * 512


def manifest(value):
    if (not isinstance(value,dict) or set(value)!={'schema','layers','mounts'} or value['schema']!=1
            or not isinstance(value['layers'],list) or not 1<=len(value['layers'])<=8
            or not isinstance(value['mounts'],list) or not 1<=len(value['mounts'])<=32):
        raise ValueError('Invalid guest DAX manifest')
    for layer in value['layers']:
        if (not isinstance(layer,dict) or set(layer)!={'sha256','bytes'}
                or not isinstance(layer['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',layer['sha256'])
                or type(layer['bytes']) is not int or not 128*2**20<=layer['bytes']<=32*2**30
                or layer['bytes']%(128*2**20)):
            raise ValueError('Invalid guest pmem layer')
    paths=[]
    for target in value['mounts']:
        if not isinstance(target,str):raise ValueError('Unsafe guest shared mount')
        path=PurePosixPath(target)
        if (not isinstance(target,str) or not re.fullmatch('/[A-Za-z0-9_./-]+',target)
                or str(path)!=target or '..' in path.parts or len(path.parts)<2
                or path.parts[1] in ('dev','proc','sys','run','tmp','work','var','dsec-helper.py','execution_contract.py','dsec-init','dsec-native','dsec-native-agent')):
            raise ValueError('Unsafe guest shared mount')
        if any(path==old or path.is_relative_to(old) or old.is_relative_to(path) for old in paths):
            raise ValueError('Overlapping shared mount')
        paths.append(path)
    return value
