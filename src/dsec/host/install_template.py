"""Template for a reviewed, standalone root installer; never imports runtime code."""

INSTALLER = '''#!/usr/bin/python3
import argparse, grp, hashlib, json, os, pathlib, pwd, stat, subprocess, sys, tempfile

SPECS = __SPECS__
STATE = __STATE__
GUARD_SOURCE = __GUARD_SOURCE__
RUNTIME_IDENTITY = __RUNTIME_IDENTITY__

def check_node_platform():
    namespace = {}
    exec(compile(GUARD_SOURCE, 'embedded-node-probe.py', 'exec'), namespace)
    namespace['require_eligible'](namespace['observe'](administrator=True))
    user, uid, gid = RUNTIME_IDENTITY
    account = pwd.getpwnam(user)
    if (account.pw_uid != uid or account.pw_uid <= 0 or grp.getgrnam('kvm').gr_gid != gid
            or gid not in os.getgrouplist(user, account.pw_gid)):
        raise RuntimeError('Reviewed runtime account or kvm group membership changed')
    # Execute pinned standard-library-only code after dropping to the real
    # service identity. Root access alone does not prove runtime device access.
    code = GUARD_SOURCE + '\\nimport json, pwd\\n' + (
        'account = pwd.getpwnam(%r)\\n'
        'if account.pw_uid != %d or %d not in os.getgrouplist(account.pw_name, account.pw_gid):\\n'
        '    raise RuntimeError("Reviewed runtime identity changed")\\n'
        'os.initgroups(account.pw_name, account.pw_gid)\\n'
        'os.setgid(account.pw_gid)\\nos.setuid(account.pw_uid)\\n'
        'report = observe()\\nprint(json.dumps(report))\\nrequire_eligible(report)\\n') % (user, uid, gid)
    result = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Runtime account lacks Node prerequisites; run preflight as that account')

def directory(path, mode=0o755):
    path = pathlib.Path(path)
    if path.exists():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Unsafe administrator directory: '+str(path))
    else:
        directory(path.parent)
        path.mkdir(mode=mode)

def main():
    parser = argparse.ArgumentParser(description='Install reviewed DSec instance network privileges')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Administrator execution is required')
    check_node_platform()
    root = pathlib.Path(__file__).resolve().parent
    blobs = {}
    for source, target, mode, expected in SPECS:
        blob = (root/source).read_bytes()
        if hashlib.sha256(blob).hexdigest() != expected:
            raise RuntimeError('Reviewed bundle content changed: '+source)
        path = pathlib.Path(target)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError('Unsafe existing target: '+str(path))
            if path.read_bytes() != blob:
                raise RuntimeError('Refusing to replace a different installation: '+str(path))
        blobs[source] = blob
    compile(blobs['netns-helper.py'], 'netns-helper.py', 'exec')
    with tempfile.TemporaryDirectory(prefix='dsec-privilege-check-') as temporary:
        sudoers = pathlib.Path(temporary)/'sudoers'
        sudoers.write_bytes(blobs['sudoers'])
        subprocess.run(['visudo','-cf',str(sudoers)], check=True)
    if not args.apply:
        print(json.dumps({'status':'checked','targets':[x[1] for x in SPECS],'state':STATE}))
        return
    for source, target, mode, expected in SPECS:
        path = pathlib.Path(target)
        directory(path.parent)
        fd, temp = tempfile.mkstemp(prefix='.dsec-install-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(blobs[source]); stream.flush(); os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
            os.replace(temp, path)
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(parent)
            finally: os.close(parent)
        finally:
            pathlib.Path(temp).unlink(missing_ok=True)
    directory(STATE, mode=0o700)
    if pathlib.Path(STATE).stat().st_mode & 0o077:
        raise RuntimeError('Network state must be root-only')
    print(json.dumps({'status':'installed','targets':[x[1] for x in SPECS],'state':STATE}))

if __name__ == '__main__':
    main()
'''
