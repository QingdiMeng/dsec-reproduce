"""Stop only the daemon matching this service registry; leave sandbox VMMs intact."""
import argparse
import json
import os
from pathlib import Path
import select
import signal

def matches_daemon(argv, root):
    """Accept the installed module or the adjacent legacy script, never another service."""
    tail = argv[1:]
    while tail and tail[0] in ("-B", "-I"):
        tail = tail[1:]
    expected = str(Path(__file__).with_name("sandboxd.py").resolve())
    if tail[:2] == ["-m", "sandboxd"]:
        tail = tail[2:]
    elif tail[:1] == [expected]:
        tail = tail[1:]
    else:
        return False
    return tail[:2] == ["--root", str(Path(root).resolve())]

def main():
    from durable_manager import identity
    parser=argparse.ArgumentParser(); parser.add_argument("--root",required=True)
    args=parser.parse_args(); root=Path(args.root).resolve(); record=root/"daemon.json"
    if not record.exists(): return
    saved=json.loads(record.read_text())
    try: fd=os.pidfd_open(saved["pid"])
    except ProcessLookupError: return
    try:
        try: current=identity(saved["pid"])
        except (FileNotFoundError,ProcessLookupError): return
        if saved!=current: return  # PID has changed; never signal its new owner.
        argv=current["argv"]
        if current["uid"]!=os.getuid() or not matches_daemon(argv, root):
            raise RuntimeError("Daemon identity does not match this deployment")
        signal.pidfd_send_signal(fd,signal.SIGTERM)
        if not select.select([fd],[],[],45)[0]:
            signal.pidfd_send_signal(fd,signal.SIGKILL)
            if not select.select([fd],[],[],5)[0]: raise TimeoutError("Daemon did not exit")
    finally: os.close(fd)

if __name__=="__main__": main()
