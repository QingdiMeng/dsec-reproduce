"""Run the pinned upstream OSWorld compare_table metric on a Calc replay."""
import argparse
import importlib
import json
from pathlib import Path
import sys
import types


def score(upstream, task, result, expected):
    upstream = upstream.resolve(strict=True)
    task = json.loads(task.read_text())
    evaluator = task['evaluator']
    if evaluator['func'] != 'compare_table' or evaluator['result']['type'] != 'vm_file':
        raise ValueError('Expected a Calc compare_table task')
    if (result.name != evaluator['result']['dest'] or
            expected.name != evaluator['expected']['dest']):
        raise ValueError('Workbook names do not match task manifest')
    # Load the upstream table metric and utilities without importing unrelated
    # browser, image, and network metrics through the package initializer.
    root = upstream/'desktop_env/evaluators/metrics'
    name = 'desktop_env.evaluators.metrics'
    package = types.ModuleType(name)
    package.__path__ = [str(root)]
    sys.path.insert(0, str(upstream))
    sys.modules[name] = package
    metric = importlib.import_module(name+'.table')
    return dict(task_id=task['id'], score=metric.compare_table(
        str(result), str(expected), **evaluator['options']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('upstream', 'task', 'result', 'expected'):
        parser.add_argument('--'+arg, type=Path, required=True)
    print(json.dumps(score(**vars(parser.parse_args()))))


if __name__ == '__main__':
    main()
