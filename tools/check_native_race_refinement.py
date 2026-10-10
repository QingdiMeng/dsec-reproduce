"""Replay controlled implementation traces and require failing code mutations.

Runs the canonical regression tests, not a Python reimplementation of the FSM.
Negative controls mutate a single reviewed boundary (two for stop finality),
require assertion failures, then check the SAME trace against safe/unsafe TLA+.
"""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from check_concurrency_model import checked_jar, fetch_jar, TLC_SHA256

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from tests.unit import test_native_races as regressions
from dsec.runtime.sessions import jobs, service
from dsec.runtime import container_edge, transitions

CONTROLS = (
    ('queued-cancel', 'test_queued_cancel_prevents_dispatch_and_survives_query',
     'lost_cancel', 'PendingCancelIsAcknowledged'),
    ('stop-before-callback', 'test_stop_is_final_despite_late_unknown_callback',
     'unfenced', 'StoppedIsFinal'),
    ('old-incarnation', 'test_old_incarnation_callback_cannot_stop_replacement',
     'unfenced', 'OldCallbackCannotRetireReplacement'),
    ('container-native-first', 'test_container_native_readers_exclude_stop_and_owner_close',
     'missing_guard', 'StopExcludesNativeActivity'),
)
SOURCES = ('src/dsec/runtime/sessions/jobs.py', 'src/dsec/runtime/sessions/service.py',
    'src/dsec/runtime/container_edge.py', 'src/dsec/runtime/transitions.py',
    'src/dsec/runtime/requests.py', 'src/dsec/runtime/lifecycle.py',
    'src/dsec/runtime/registry_store.py', 'tests/unit/test_native_races.py',
    'tests/unit/test_native_sdk.py', 'tests/unit/test_native_guest.py',
    'tests/unit/test_container_edge.py', 'tests/unit/test_edge_assembly.py', 'guest_native.c',
    'tools/verify_native_races.py')


def mutated(module, changes, out):
    source = Path(module.__file__).read_text()
    for before, after in changes:
        if source.count(before) != 1:
            raise ValueError(f'Code/model boundary changed in {module.__name__}: {before!r}')
        source = source.replace(before, after, 1)
    target = out / (module.__name__ + '.py')
    target.write_text(source)
    namespace = dict(module.__dict__)
    exec(compile(source, str(target), 'exec'), namespace)
    return namespace


@contextmanager
def mutation(name, out):
    with ExitStack() as stack:
        if name == 'queued-cancel':
            namespace = mutated(jobs, [('job = self.active.get(args["lookup_id"])', 'job = None')], out)
            stack.enter_context(patch.object(jobs.NativeJobs, 'cancel', namespace['NativeJobs'].cancel))
        elif name in ('stop-before-callback', 'old-incarnation'):
            namespace = mutated(service, [('if (getattr(sandbox, "native_incarnation", 0) == incarnation\n'
                '                    and sandbox.state == "RUNNING"):\n'
                '                sandbox._fail("native_operation_outcome_unknown")',
                'if True:\n                sandbox._fail("native_operation_outcome_unknown")')], out)
            stack.enter_context(patch.object(service, 'native_operation', namespace['native_operation']))
            if name == 'stop-before-callback':
                namespace = mutated(transitions, [('        if sandbox.state == "STOPPED":\n            return\n', '')], out)
                stack.enter_context(patch.object(transitions.LifecycleController, 'fail', namespace['LifecycleController'].fail))
        elif name == 'container-native-first':
            namespace = mutated(container_edge, [('if self.native_activity.get(sandbox_id):', 'if False:')], out)
            stack.enter_context(patch.object(container_edge.ContainerRuntime, 'dispatch', namespace['ContainerRuntime'].dispatch))
        else:
            raise ValueError(name)
        yield


def tests(out, method=None):
    regressions.NativeRaceTests.traces = {}
    suite = (unittest.defaultTestLoader.loadTestsFromTestCase(regressions.NativeRaceTests)
             if method is None else unittest.TestSuite([regressions.NativeRaceTests(method)]))
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    (out/'regression.log').write_text(log.getvalue())
    traces = dict(regressions.NativeRaceTests.traces)
    (out/'observed.json').write_text(json.dumps(traces, indent=2, sort_keys=True)+'\n')
    # A skip, setup/cleanup error, or unexpected exception is never a valid
    # negative control. Only the specified behavioral assertions may fail.
    good = result.wasSuccessful() and not result.skipped
    bad = len(result.failures) == 1 and not result.errors and not result.skipped
    return traces, good, bad, result.testsRun


def tla(value, field=None):
    if isinstance(value, dict):
        return '[' + ', '.join(f'{key} |-> {tla(item, key)}' for key, item in value.items()) + ']'
    if isinstance(value, list):
        if field != 'active':
            raise ValueError('Unexpected list projection')
        return '{' + ', '.join(tla(item) for item in value) + '}'
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, (str, int)):
        return json.dumps(value)
    raise ValueError('Unsupported observation')


def replay(java, jar, out, name, trace, fault='none', expected=None, invariant=None):
    case = out/name
    case.mkdir()
    module = trace['model']
    for source in (module+'.tla', 'RaceReplay.tla'):
        shutil.copyfile(ROOT/'verification'/source, case/source)
    (case/'RaceInput.tla').write_text('---- MODULE RaceInput ----\nEXTENDS '+module+'\n'
        + 'Observed == <<'+',\n'.join(tla(s) for s in trace['observed'])+'>>\n====\n')
    (case/'RaceReplay.cfg').write_text('SPECIFICATION ReplaySpec\nCHECK_DEADLOCK FALSE\n'
        + 'CONSTANT Fault = '+json.dumps(fault)+'\nINVARIANTS TraceConforms'
        + (' '+invariant if invariant else '')+'\n')
    result = subprocess.run([java, '-Xmx512m', '-XX:+UseParallelGC', '-jar', str(jar),
        '-workers', '1', '-seed', '1', '-fp', '0', '-metadir', str(case/'states'), 'RaceReplay'],
        cwd=case, capture_output=True, text=True, timeout=60)
    log = result.stdout+result.stderr
    (case/'tlc.log').write_text(log)
    passed = (result.returncode == 12 and f'Invariant {expected} is violated' in log if expected
              else result.returncode == 0 and 'Model checking completed. No error has been found.' in log)
    item = dict(check=name, passed=passed, exit_code=result.returncode, expected_violation=expected)
    print(json.dumps(item), flush=True)
    if not passed:
        print(log[-8000:], flush=True)
    return item


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    dependency = parser.add_mutually_exclusive_group(required=True)
    dependency.add_argument('--jar', type=Path)
    dependency.add_argument('--fetch', action='store_true')
    parser.add_argument('--cache', type=Path, default=Path(tempfile.gettempdir())/'dsec-tla')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--java', default='java')
    parser.add_argument('--observed', type=Path,
        help='Replay a passed verify_native_races Linux report instead of running fixture/mutation tests')
    args = parser.parse_args()
    java = shutil.which(args.java)
    if java is None or (args.observed is None and shutil.which('cc') is None):
        parser.error('Java and a C compiler are required; checks cannot be skipped')
    jar = checked_jar(args.jar) if args.jar else fetch_jar(args.cache)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    sources = out/'sources'
    sources.mkdir()
    files = [ROOT/p for p in SOURCES] + list((ROOT/'verification').glob('*')) + [Path(__file__).resolve()]
    hashes = {}
    for source in files:
        if not source.is_file():
            continue
        name = str(source.relative_to(ROOT))
        hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        target = sources/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    if args.observed is not None:
        raw = args.observed.read_bytes()
        observed = json.loads(raw)
        expected = {
            'microvm-queued-cancel': 'QueueCancellation',
            'container-queued-cancel': 'QueueCancellation',
            'microvm-stop-before-callback': 'NativeCallbackFence',
            'microvm-old-incarnation': 'NativeCallbackFence',
            'container-native-first': 'ContainerNativeGate',
            'container-stop-first': 'ContainerNativeGate',
        }
        if (observed.get('status') != 'passed' or observed.get('cleanup_errors')
                or set(observed.get('traces', {})) != set(expected)):
            raise ValueError('Expected a completed real Linux acceptance report with all six traces')
        cleanup = observed.get('cleanup', {})
        for key in ('alive_vmm_pids', 'private_containers', 'active_native_operations', 'private_directories'):
            if key not in cleanup or cleanup[key]:
                raise ValueError('Missing or unsuccessful Linux cleanup evidence: ' + key)
        if any(trace.get('model') != expected[name] or not trace.get('observed')
               for name, trace in observed['traces'].items()):
            raise ValueError('Linux trace/model mapping changed; review the projection')
        (out/'linux-observed.json').write_bytes(raw)
        checks = [replay(java, jar, out, name, trace)
                  for name, trace in observed['traces'].items()]
        report = dict(checks=checks, tlc_sha256=TLC_SHA256, source_sha256=hashes,
            observed_report_sha256=hashlib.sha256(raw).hexdigest(),
            producer_identities=observed['identities'],
            boundary='Six controlled real Linux backend traces against original Next; not full refinement or liveness')
        (out/'report.json').write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
        raise SystemExit(0 if all(c['passed'] for c in checks) else 1)
    checks = []
    fixed = out/'fixed'
    fixed.mkdir()
    traces, good, _, count = tests(fixed)
    checks.append(dict(check='regressions-fixed', passed=good, tests=count))
    if not good:
        print((fixed/'regression.log').read_text(), flush=True)
    for name, trace in traces.items():
        checks.append(replay(java, jar, out, name+'-safe', trace))
    for name, method, fault, invariant in CONTROLS:
        case = out/(name+'-mutation')
        case.mkdir()
        with mutation(name, case):
            traces, _, bad, count = tests(case, method)
        checks.append(dict(check=name+'-regression-catches-mutation', passed=bad, tests=count))
        if not bad or name not in traces:
            print((case/'regression.log').read_text(), flush=True)
            continue
        trace = traces[name]
        checks.append(replay(java, jar, out, name+'-unsafe-conforms', trace, fault))
        checks.append(replay(java, jar, out, name+'-unsafe-violation', trace, fault, invariant, invariant))
        checks.append(replay(java, jar, out, name+'-safe-rejects', trace, 'none', 'TraceConforms'))
    report = dict(checks=checks, tlc_sha256=TLC_SHA256, source_sha256=hashes,
        boundary='Controlled trace conformance for three targeted races; instrumented Docker/KVM drivers. '
                 'Not full implementation refinement, liveness, or Linux production acceptance.')
    (out/'report.json').write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
    raise SystemExit(0 if len(checks) == 22 and all(c['passed'] for c in checks) else 1)


if __name__ == '__main__':
    main()
