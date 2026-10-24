"""Shared validation for configured guest egress hostname exceptions."""
import re


def validate_proxy_bypass_hosts(values):
    if not isinstance(values, (list, tuple)) or len(values) > 32:
        raise ValueError("Proxy bypass hosts must be a list of at most 32 hostnames")
    hosts = []
    for value in values:
        if (not isinstance(value, str) or not 1 <= len(value) <= 253 or
                any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in value.split('.'))):
            raise ValueError("Invalid proxy bypass hostname")
        host = value.lower()
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)
