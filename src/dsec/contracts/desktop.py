"""Bounded keyboard and pointer batches for an explicitly enabled FullVM display."""
import re


def input_events(events):
    if not isinstance(events, (list, tuple)) or not 1 <= len(events) <= 64:
        raise ValueError('Desktop input requires 1..64 events')
    normalized = []
    for item in events:
        if not isinstance(item, dict) or set(item) != {'type','data'} or not isinstance(item['data'],dict):
            raise ValueError('Invalid desktop input event')
        kind, data = item['type'], item['data']
        if kind == 'abs':
            valid = (set(data)=={'axis','value'} and data['axis'] in ('x','y')
                     and type(data['value']) is int and 0<=data['value']<=32767)
        elif kind == 'btn':
            valid = (set(data)=={'button','down'} and data['button'] in ('left','right','middle','wheel-up','wheel-down')
                     and type(data['down']) is bool)
        elif kind == 'key':
            key = data.get('key')
            valid = (set(data)=={'key','down'} and type(data['down']) is bool
                     and isinstance(key,dict) and set(key)=={'type','data'} and key['type']=='qcode'
                     and isinstance(key['data'],str) and re.fullmatch(r'[a-z0-9_]{1,24}',key['data']) is not None)
        else:
            valid = False
        if not valid:
            raise ValueError('Unsupported desktop input event')
        normalized.append(dict(type=kind,data=dict(data)))
    return normalized
