"""Validated, daemon-wide guest HTTP(S) egress proxy configuration."""

import ipaddress
import re
import shlex
from urllib.parse import urlsplit


def validate_proxy_url(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Egress proxy must be an HTTP URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or not parsed.hostname or not parsed.port or
            parsed.username or parsed.password or parsed.path or parsed.query or
            parsed.fragment):
        raise ValueError("Egress proxy must be http://IPv4:port without credentials")
    ipaddress.IPv4Address(parsed.hostname)
    if not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid egress proxy port")
    return f"http://{parsed.hostname}:{parsed.port}"


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


def guest_proxy_command(command, proxy_url, bypass_hosts=()):
    bypass_hosts = validate_proxy_bypass_hosts(bypass_hosts)
    if proxy_url is None:
        return command
    quoted = shlex.quote(proxy_url)
    bypass = shlex.quote(','.join(("localhost", "127.0.0.1", "::1",
                                  "169.254.110.1", "169.254.110.2", *bypass_hosts)))
    return ("export http_proxy=" + quoted + " https_proxy=" + quoted +
            " HTTP_PROXY=" + quoted + " HTTPS_PROXY=" + quoted +
            " no_proxy=" + bypass + " NO_PROXY=" + bypass + "; "
            + command)
