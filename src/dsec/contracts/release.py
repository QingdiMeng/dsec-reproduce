"""Explicit rolling/rollback compatibility; changes require a migration review."""
from copy import deepcopy
import re


COMPATIBILITY = {'cluster': 'dsec-cluster-v1', 'fleet': 'dsec-node-upgrade-v1', 'coordination': 1, 'edge': 1, 'episode': 1}


def release_ref(value):
    if (not isinstance(value, dict) or set(value) != {'build', 'version', 'compatibility'}
            or not isinstance(value['build'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['build'])
            or not isinstance(value['version'], str) or not re.fullmatch(r'[A-Za-z0-9.+_-]{1,64}', value['version'])
            or not isinstance(value['compatibility'], dict)
            or set(value['compatibility']) != set(COMPATIBILITY)
            or any(type(value['compatibility'][key]) is not type(expected)
                   for key, expected in COMPATIBILITY.items())):
        raise ValueError('Explicit build, package version and persistence/protocol contract required')
    return deepcopy(value)


def same_contract(source, target):
    source, target = release_ref(source), release_ref(target)
    if source['compatibility'] != target['compatibility']:
        raise ValueError('Incompatible protocol or persistence contract; reviewed migration required')
    return target


def coherent_release(services):
    """Every configured role must identify the same loaded package."""
    refs = [release_ref(value) for value in services.values()]
    if not refs or any(value != refs[0] for value in refs):
        raise ValueError('Node gateway, Edge and configured episode service must use the same release')
    return refs[0]
