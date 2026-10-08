#!/usr/bin/python3
"""Root-owned, sudo-scoped network helper for one DSec microVM per netns.

Install this file root-owned. The caller may only name its own 12-hex sandbox
directory and a bounded address slot. No caller-provided command or path is
ever executed. Firecracker is launched inside the namespace after dropping to
the configured unprivileged account.
"""
import ipaddress
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import time

CONFIG = Path("/etc/dsec-netns-helper.json")
STATE = Path("/var/lib/dsec-netns")
SLOT_OFFSET = 0
MAX_SLOTS = 32768
SID = re.compile(r"[0-9a-f]{12}\Z")
VM_IP = "169.254.110.2"
TAP_IP = "169.254.110.1"
VM_CIDR = VM_IP + "/30"
TAP_CIDR = TAP_IP + "/30"
VETH_POOL = ipaddress.IPv4Network("10.231.0.0/16")
PRIVATE = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10",
           "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
           "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4")


def run(*args, capture=False):
    result = subprocess.run(args, check=True, text=True,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    return result.stdout if capture else None


def exists(*args):
    return subprocess.run(args, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def checked_host_context():
    """Named network handles must survive the calling service's termination.

    The narrowly scoped helper manages host-visible handles. Inheriting a
    private mount namespace would leave empty /run/netns files on its exit.
    Refuse this context before touching links, firewall rules or state files.
    """
    for kind in ('mnt', 'net'):
        if os.readlink(f'/proc/self/ns/{kind}') != os.readlink(f'/proc/1/ns/{kind}'):
            raise RuntimeError(f'Network helper requires the host {kind} namespace; request refused')


def checked_config():
    info = CONFIG.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("Network helper config must be root-owned and immutable to the user")
    cfg = json.loads(CONFIG.read_text())
    required = ("uid", "gid", "runtime_root", "firecracker", "firecracker_sha256", "uplink", "dns")
    if any(key not in cfg for key in required):
        raise ValueError("Incomplete network helper config")
    if (not isinstance(cfg["uid"], int) or cfg["uid"] <= 0 or
            not isinstance(cfg["gid"], int) or cfg["gid"] <= 0 or
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", cfg["uplink"])):
        raise ValueError("Invalid network helper identity/uplink")
    ipaddress.IPv4Address(cfg["dns"])
    proxy = cfg.get("egress_proxy")
    if proxy is not None:
        if not isinstance(proxy, dict) or set(proxy) != {"ip", "port"}:
            raise ValueError("Invalid egress proxy configuration")
        ipaddress.IPv4Address(proxy["ip"])
        if (not isinstance(proxy["port"], int) or isinstance(proxy["port"], bool)
                or not 1 <= proxy["port"] <= 65535):
            raise ValueError("Invalid egress proxy port")
    for key in ("runtime_root", "firecracker"):
        if not isinstance(cfg[key], str) or not cfg[key].startswith("/"):
            raise ValueError("Network helper paths must be absolute")
    for prefix in ('firecracker', 'dax_firecracker'):
        if prefix not in cfg:
            continue
        if not isinstance(cfg[prefix], str) or not cfg[prefix].startswith('/') or not re.fullmatch(
                r'[0-9a-f]{64}', cfg.get(prefix+'_sha256', '')):
            raise ValueError('Configured VMM requires an absolute path and SHA-256 pin')
    return cfg


def names(sid, slot):
    if not SID.fullmatch(sid) or not 0 <= slot < MAX_SLOTS:
        raise ValueError("Invalid sandbox ID or network slot")
    host_ip = ipaddress.IPv4Address(int(VETH_POOL.network_address) + 2 * (slot + SLOT_OFFSET))
    peer_ip = ipaddress.IPv4Address(int(host_ip) + 1)
    return {"namespace": "dsec-" + sid, "host_veth": "dh" + sid,
            "peer_veth": "dn" + sid, "slot":slot,
            "host_ip":str(host_ip), "peer_ip":str(peer_ip),
            "host_cidr":str(host_ip) + "/31", "peer_cidr":str(peer_ip) + "/31",
            "tap":"tap0", "guest_cidr":VM_CIDR, "gateway":TAP_IP}


def state_file(sid):
    return STATE / (sid + ".json")


def loaded(sid):
    value = json.loads(state_file(sid).read_text())
    if value.get("id") != sid or not isinstance(value.get("slot"), int):
        raise ValueError("Invalid helper state")
    return value


def active_vmm(cfg, sid):
    expected = str(Path(cfg["runtime_root"]) / sid / "api.sock").encode()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
            if expected in args:
                return True
        except (OSError, PermissionError):
            continue
    return False


def host_rules(cfg):
    uplink = cfg["uplink"]
    if not exists("ip", "-4", "route", "get", "1.1.1.1"):
        raise RuntimeError("No IPv4 route")
    route = run("ip", "-4", "route", "get", "1.1.1.1", capture=True)
    if f" dev {uplink} " not in route:
        raise RuntimeError("Configured uplink differs from active route")
    if run("sysctl", "-n", "net.ipv4.ip_forward", capture=True).strip() != "1":
        raise RuntimeError("Host IPv4 forwarding is disabled")
    rules = [
        ("filter", "INPUT", ["-i", "dh+", "-s", "10.231.0.0/16", "-j", "DROP"]),
        ("filter", "FORWARD", ["-i", "dh+", "-o", "dh+", "-j", "DROP"]),
        ("filter", "FORWARD", ["-i", "dh+", "-o", uplink,
                               "-s", "10.231.0.0/16", "-j", "ACCEPT"]),
        ("filter", "FORWARD", ["-i", uplink, "-o", "dh+", "-d", "10.231.0.0/16",
                               "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED",
                               "-j", "ACCEPT"]),
        ("nat", "POSTROUTING", ["-s", "10.231.0.0/16", "-o", uplink,
                                "-j", "MASQUERADE"]),
    ]
    for table, chain, rule in rules:
        tagged = ["-m", "comment", "--comment", "dsec-netns"] + rule
        base = ["iptables", "-w", "-t", table]
        if not exists(*base, "-C", chain, *tagged):
            run(*base, "-I" if table == "filter" else "-A", chain,
                *(["1"] if table == "filter" else []), *tagged)


def namespace_rules(name, cfg):
    prefix = ["ip", "netns", "exec", name, "iptables", "-w"]
    for chain in ("INPUT", "OUTPUT", "FORWARD"):
        run(*prefix, "-P", chain, "DROP")
        run(*prefix, "-F", chain)
    run(*prefix, "-A", "INPUT", "-i", "lo", "-j", "ACCEPT")
    run(*prefix, "-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT")
    run(*prefix, "-A", "FORWARD", "-i", "vpeer", "-o", "tap0", "-m", "conntrack",
        "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
    # A guest can set arbitrary source addresses. Only its assigned /32 may
    # leave the TAP; otherwise it could impersonate another veth slot.
    run(*prefix, "-A", "FORWARD", "-i", "tap0", "!", "-s", VM_IP + "/32",
        "-j", "DROP")
    for proto in ("udp", "tcp"):
        run(*prefix, "-A", "FORWARD", "-i", "tap0", "-o", "vpeer",
            "-d", cfg["dns"], "-p", proto, "--dport", "53", "-j", "ACCEPT")
    if cfg.get("egress_proxy"):
        proxy = cfg["egress_proxy"]
        run(*prefix, "-A", "FORWARD", "-i", "tap0", "-o", "vpeer",
            "-d", proxy["ip"] + "/32", "-p", "tcp", "--dport",
            str(proxy["port"]), "-j", "ACCEPT")
    for cidr in PRIVATE:
        run(*prefix, "-A", "FORWARD", "-i", "tap0", "-o", "vpeer",
            "-d", cidr, "-j", "DROP")
    run(*prefix, "-A", "FORWARD", "-i", "tap0", "-o", "vpeer", "-j", "ACCEPT")
    nat = ["ip", "netns", "exec", name, "iptables", "-w", "-t", "nat"]
    run(*nat, "-A", "POSTROUTING", "-s", VM_IP + "/32", "-o", "vpeer",
        "-j", "MASQUERADE")


def create(cfg, sid, slot):
    spec = names(sid, slot)
    state = state_file(sid)
    if state.exists():
        previous = loaded(sid)
        if previous["slot"] != slot:
            raise RuntimeError("Sandbox network slot changed")
        if exists("ip", "netns", "exec", spec["namespace"], "true") and exists(
                "ip", "link", "show", "dev", spec["host_veth"]):
            return spec
        if active_vmm(cfg, sid):
            raise RuntimeError("Network missing while VMM is still alive")
    for other in STATE.glob("*.json"):
        if other != state and json.loads(other.read_text()).get("slot") == slot:
            raise RuntimeError("Network slot already owned")
    if exists("ip", "netns", "exec", spec["namespace"], "true") or exists(
            "ip", "link", "show", "dev", spec["host_veth"]):
        raise RuntimeError("Unregistered network resource exists")
    host_rules(cfg)
    name = spec["namespace"]
    try:
        run("ip", "netns", "add", name)
        run("ip", "link", "add", spec["host_veth"], "type", "veth", "peer",
            "name", spec["peer_veth"])
        run("ip", "link", "set", spec["peer_veth"], "netns", name)
        run("ip", "addr", "add", spec["host_cidr"], "dev", spec["host_veth"])
        run("ip", "link", "set", spec["host_veth"], "up")
        run("ip", "-n", name, "link", "set", spec["peer_veth"], "name", "vpeer")
        run("ip", "-n", name, "addr", "add", spec["peer_cidr"], "dev", "vpeer")
        run("ip", "-n", name, "link", "set", "vpeer", "up")
        run("ip", "netns", "exec", name, "ip", "tuntap", "add", "dev", "tap0",
            "mode", "tap", "user", str(cfg["uid"]))
        run("ip", "-n", name, "addr", "add", TAP_CIDR, "dev", "tap0")
        run("ip", "-n", name, "link", "set", "tap0", "up")
        run("ip", "-n", name, "link", "set", "lo", "up")
        run("ip", "-n", name, "route", "add", "default", "via", spec["host_ip"])
        run("ip", "netns", "exec", name, "sysctl", "-q", "-w",
            "net.ipv4.ip_forward=1")
        run("ip", "netns", "exec", name, "sysctl", "-q", "-w",
            "net.ipv6.conf.all.disable_ipv6=1")
        namespace_rules(name, cfg)
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = state.with_suffix(".tmp")
        temp.write_text(json.dumps({"id":sid, "slot":slot, "created_at":time.time()}))
        os.replace(temp, state)
    except Exception:
        if exists("ip", "link", "show", "dev", spec["host_veth"]):
            run("ip", "link", "del", spec["host_veth"])
        if exists("ip", "netns", "exec", name, "true"):
            run("ip", "netns", "del", name)
        raise
    return spec


def inspect(sid):
    value = loaded(sid)
    spec = names(sid, value["slot"])
    if not exists("ip", "netns", "exec", spec["namespace"], "true"):
        raise RuntimeError("Sandbox network namespace missing")
    if not exists("ip", "link", "show", "dev", spec["host_veth"]):
        raise RuntimeError("Sandbox host veth missing")
    spec["namespace_inode"] = os.stat("/run/netns/" + spec["namespace"]).st_ino
    return spec


def list_networks():
    result = []
    for state in sorted(STATE.glob("*.json")):
        sid = state.stem
        if not SID.fullmatch(sid):
            raise RuntimeError("Unexpected network state file")
        value = loaded(sid)
        result.append({"id": sid, "slot": value["slot"]})
    return result


def release(cfg, sid):
    value = loaded(sid)
    spec = names(sid, value["slot"])
    if active_vmm(cfg, sid):
        raise RuntimeError("Refusing to remove network of a live VMM")
    name = spec["namespace"]
    if exists("ip", "netns", "exec", name, "true"):
        pids = run("ip", "netns", "pids", name, capture=True).strip()
        if pids:
            raise RuntimeError("Other processes still own the network namespace")
    if exists("ip", "link", "show", "dev", spec["host_veth"]):
        run("ip", "link", "del", spec["host_veth"])
    if exists("ip", "netns", "exec", name, "true"):
        run("ip", "netns", "del", name)
    state_file(sid).unlink()
    return {"released":sid}


def checked_binary(cfg, *, dax=False):
    key = 'dax_firecracker' if dax else 'firecracker'
    if key not in cfg:
        raise ValueError('Requested VMM alias is not configured')
    binary = Path(cfg[key]).resolve(strict=True)
    digest = hashlib.sha256()
    with binary.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != cfg[key+'_sha256']:
        raise RuntimeError('VMM binary differs from the administrator pin')
    return binary


def attest(cfg, sid, pid):
    spec = inspect(sid)
    if pid <= 1:
        raise ValueError("Invalid VMM PID")
    proc = Path(f"/proc/{pid}")
    current_binary = proc.joinpath('exe').resolve(strict=True)
    dax = 'dax_firecracker' in cfg and current_binary == Path(cfg['dax_firecracker']).resolve(strict=True)
    expected_binary = str(checked_binary(cfg, dax=dax))
    expected_argv = [expected_binary, "--api-sock",
                     str(Path(cfg["runtime_root"]) / sid / "api.sock")]
    argv = proc.joinpath("cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
    if (str(proc.joinpath("exe").resolve(strict=True)) != expected_binary or
            argv != expected_argv or proc.stat().st_uid != cfg["uid"] or
            os.stat(proc / "ns/net").st_ino != spec["namespace_inode"]):
        raise RuntimeError("VMM process identity or namespace mismatch")
    fields = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()
    return {"pid":pid, "start_ticks":fields[19],
            "boot_id":Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "exe":expected_binary, "argv":argv, "uid":cfg["uid"]}


def launch(cfg, sid, log_name):
    dax = log_name.endswith('@dax39')
    if dax:
        log_name = log_name.removesuffix('@dax39')
    if log_name not in ("boot.log", "restore.log"):
        raise ValueError("Unexpected VMM log name")
    spec = inspect(sid)
    directory = Path(cfg["runtime_root"]) / sid
    root = Path(cfg["runtime_root"]).resolve(strict=True)
    if directory.parent.resolve(strict=True) != root:
        raise ValueError("Invalid sandbox directory")
    st = directory.lstat()
    private = stat.S_IMODE(st.st_mode) == 0o700
    shared = stat.S_IMODE(st.st_mode) == 0o2770 and st.st_gid == cfg['gid']
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != cfg["uid"] or not (private or shared):
        raise RuntimeError("Sandbox directory owner/mode mismatch")
    log = directory / log_name
    log_st = log.lstat()
    if not stat.S_ISREG(log_st.st_mode) or log_st.st_uid != cfg["uid"]:
        raise RuntimeError("VMM log owner mismatch")
    binary = checked_binary(cfg, dax=dax)
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    try:
        command = ["ip", "netns", "exec", spec["namespace"], "setpriv",
                   "--reuid", str(cfg["uid"]), "--regid", str(cfg["gid"]),
                   "--init-groups", str(binary),
                   "--api-sock", str(directory / "api.sock")]
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=fd,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                close_fds=True)
    finally:
        os.close(fd)
    expected = binary.resolve()
    try:
        for _ in range(100):
            try:
                current = Path(f"/proc/{proc.pid}/exe").resolve(strict=True)
                if current == expected:
                    process_identity = attest(cfg, sid, proc.pid)
                    return {"pid":proc.pid, "namespace":spec["namespace"],
                            "namespace_inode":spec["namespace_inode"],
                            "namespace_attested":True,"identity":process_identity}
            except FileNotFoundError:
                break
            time.sleep(.01)
        raise RuntimeError("Firecracker did not replace the namespace launcher")
    except Exception:
        if proc.poll() is None:
            fd = os.pidfd_open(proc.pid)
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                if not select.select([fd], [], [], 5)[0]:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
            finally:
                os.close(fd)
        raise


def main():
    if os.geteuid() != 0 or len(sys.argv) < 2:
        raise SystemExit("Root helper usage: list | ensure|inspect|launch|release SID [slot|log]")
    cfg = checked_config()
    checked_host_context()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_info = STATE.lstat()
    if not stat.S_ISDIR(state_info.st_mode) or state_info.st_uid != 0 or state_info.st_mode & 0o077:
        raise RuntimeError("Network helper state directory must be root-only")
    action = sys.argv[1]
    sid = sys.argv[2] if len(sys.argv) >= 3 else None
    if action != "list" and (sid is None or not SID.fullmatch(sid)):
        raise ValueError("Invalid sandbox ID")
    with (STATE / "manager.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == "ensure" and len(sys.argv) == 4:
            result = create(cfg, sid, int(sys.argv[3]))
        elif action == "inspect" and len(sys.argv) == 3:
            result = inspect(sid)
        elif action == "launch" and len(sys.argv) == 4:
            result = launch(cfg, sid, sys.argv[3])
        elif action == "release" and len(sys.argv) == 3:
            result = release(cfg, sid)
        elif action == "attest" and len(sys.argv) == 4:
            result = attest(cfg, sid, int(sys.argv[3]))
        elif action == "list" and len(sys.argv) == 2:
            result = list_networks()
        else:
            raise ValueError("Invalid helper operation")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
