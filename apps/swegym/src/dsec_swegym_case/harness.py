"""Use the pinned official SWE-Gym fork for script generation and scoring."""
import hashlib
import importlib
from pathlib import Path

from dsec_swegym_case.dataset import validate_task

HARNESS_COMMIT='242429c188fcfd06aad13fce9a54d450470bf0ac'
PINS={
    'constants':'5bd655172c9a9dfcb494d9c52bcbcc0d79a59cd20c39b8bbdc41ab8b0a9baf14',
    'grading':'ce6dbe44af58ec602d5906af14cd08e816500ac13fc9a6d0f88ee79375113619',
    'log_parsers':'44ce810ca5bc14fb07f5f765cf6955ad9fac9218f1e1f7112914edbd52fc825f',
    'test_spec':'94207893a3dc148fc8d010d642bca3e94a6881be4f271265dd0c36fb562da749'}


def official_modules():
    try:modules={name:importlib.import_module('swebench.harness.'+name) for name in PINS}
    except ImportError as exc:raise RuntimeError('Install this optional case with [harness]; the DSec core does not include SWE-Gym') from exc
    for name,module in modules.items():
        if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()!=PINS[name]:
            raise RuntimeError('SWE-Gym harness differs from pinned fork '+HARNESS_COMMIT+': '+name)
    return modules


class OfficialHarness:
    def __init__(self,task):
        self.task=validate_task(task);self.modules=official_modules()
        constants=self.modules['constants'];factory=self.modules['test_spec']
        specs=constants.MAP_REPO_VERSION_TO_SPECS[task['repo'].lower()][task['version']]
        # The instance image already contains the frozen repository/environment.
        # Only generate the official eval script; rebuilding environment specs
        # would unnecessarily fetch requirements and regenerate Docker images.
        commands=factory.make_eval_script_list(task,specs,'testbed','/testbed',task['base_commit'],task['test_patch'])
        self.spec=factory.TestSpec(task['instance_id'].lower(),task['repo'].lower(),task['version'],[],commands,[],
                                   'x86_64',task['FAIL_TO_PASS'],task['PASS_TO_PASS'])

    @property
    def script(self):return self.spec.eval_script

    def grade(self,path,patch,execution):
        if execution.get('timed_out') is not False or execution.get('truncated') is not False:
            raise RuntimeError('Verifier timeout or incomplete output; no valid score')
        if not isinstance(execution.get('exit_code'),int):raise RuntimeError('Missing verifier completion')
        # Pytest failures are valid negative results. The official eval script
        # can return the final checkout's status; never score its exit code alone.
        constants=self.modules['constants'];grading=self.modules['grading']
        statuses,applied=grading.get_logs_eval(str(path))
        if not applied:raise RuntimeError('Official verifier did not acknowledge the prediction patch')
        expected=set(self.task['FAIL_TO_PASS'])|set(self.task['PASS_TO_PASS'])
        if not expected.intersection(statuses):raise RuntimeError('No expected test was observed; retain log as invalid evidence')
        prediction=dict(instance_id=self.spec.instance_id,model_name_or_path='dsec',model_patch=patch)
        result=grading.get_eval_report(self.spec,prediction,str(path),include_tests_status=True)[self.spec.instance_id]
        return dict(score=int(result['resolved']),official_report=result,harness_commit=HARNESS_COMMIT,
                    observed_expected_tests=len(expected.intersection(statuses)),expected_tests=len(expected))
