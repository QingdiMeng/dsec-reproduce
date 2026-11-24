"""Validate the project register and render its read-only status view.

Checks record consistency, not the truth of a physical acceptance result. Reports
retain their tested artifact/configuration identity; a changed file only suggests
impact and never automatically invalidates evidence or reopens implementation.
"""
import argparse
from datetime import date
import json
from pathlib import Path
import re
import subprocess

REGISTRY = 'project/registry.json'
VIEW = 'docs/PROJECT_STATUS.md'
STATES = {'accepted', 'partial', 'implemented', 'planned'}
WORK_STATES = {'candidate', 'ready', 'in_progress', 'blocked', 'done', 'cancelled'}
KINDS = {'feature', 'bugfix', 'regression', 'integration_acceptance', 'performance', 'maintenance'}
OPEN = {'ready', 'in_progress', 'blocked'}
LABELS = {'accepted': '限定范围已验收', 'partial': '部分实现/验收',
          'implemented': '已实现，待组合验收', 'planned': '未实现',
          'candidate': '候选，未承诺', 'ready': '待执行', 'in_progress': '进行中',
          'blocked': '受阻', 'done': '完成', 'cancelled': '取消'}


def load(root):
    return json.loads((Path(root)/REGISTRY).read_text())


def validate(root, value):
    root = Path(root).resolve()
    errors = []
    if not isinstance(value, dict): return ['register: object required']
    def require(ok, message):
        if not ok: errors.append(message)
    def text(value): return isinstance(value, str) and bool(value.strip())
    def ids(value): return isinstance(value, list) and all(text(x) for x in value)
    def reference(value, where):
        if not isinstance(value, dict) or not text(value.get('path')):
            errors.append(where+': file reference required'); return
        name = value['path']
        path = root/name
        if (Path(name).is_absolute() or '..' in Path(name).parts or
                not path.resolve().is_relative_to(root) or not path.is_file() or path.is_symlink()):
            errors.append(where+': missing/unsafe reference '+name); return
        section = value.get('section')
        if section is not None:
            headings = {line.lstrip('#').strip() for line in path.read_text().splitlines()
                        if re.match(r'^#{1,6} ', line)}
            require(section in headings, where+': missing section '+str(section))
    def references(values, where, nonempty=True):
        if not isinstance(values, list) or (nonempty and not values):
            errors.append(where+': references required'); return
        for item in values: reference(item, where)
    def indexed(name, pattern):
        result = {}
        rows = value.get(name)
        if not isinstance(rows, list):
            errors.append(name+': array required'); return result
        for row in rows:
            if not isinstance(row, dict): errors.append(name+': object required'); continue
            key = row.get('id')
            if not isinstance(key, str) or not re.fullmatch(pattern, key):
                errors.append(name+': invalid id '+str(key)); continue
            if key in result:
                errors.append(name+': duplicate '+key); continue
            result[key] = row
        return result
    require(value.get('schema') == 1, 'schema: expected 1')
    try: date.fromisoformat(value['updated'])
    except (KeyError, TypeError, ValueError): errors.append('updated: ISO date required')
    require(value.get('delivery') in {'working_tree', 'committed', 'released'}, 'delivery: invalid state')
    require(value.get('wip_limit') == 1, 'wip_limit: one active execution item required')
    caps = indexed('capabilities', r'CAP-[A-Z][A-Z0-9-]*')
    evidence = indexed('evidence', r'EV-[A-Z][A-Z0-9-]*')
    work = indexed('work', r'TASK-\d{3}')
    for key, row in evidence.items():
        for field in ('title', 'identity', 'scope', 'limits'):
            require(text(row.get(field)), key+': '+field+' required')
        require(row.get('kind') in {'linux', 'local', 'application', 'model', 'process'}, key+': invalid evidence kind')
        require(row.get('verdict') == 'passed', key+': only passed evidence closes a gate; retain failed runs in the report')
        require(row.get('date_kind') in {'run', 'index_review'}, key+': distinguish run date from index review')
        try: date.fromisoformat(row['date'])
        except (KeyError, TypeError, ValueError): errors.append(key+': ISO date required')
        references(row.get('records'), key)
    for key, row in caps.items():
        for field in ('title', 'owner', 'scope', 'limits'):
            require(text(row.get(field)), key+': '+field+' required')
        if not isinstance(row.get('state'), str) or row['state'] not in STATES:
            errors.append(key+': invalid capability state'); continue
        if not ids(row.get('roadmap')) or not ids(row.get('evidence')):
            errors.append(key+': roadmap/evidence must be ID arrays'); continue
        require(isinstance(row.get('roadmap'), list) and bool(row['roadmap']) and all(
            re.fullmatch(r'RM-\d{3}', x or '') and x in (root/'ROADMAP.md').read_text() for x in row['roadmap']),
            key+': existing roadmap IDs required')
        references(row.get('decisions'), key+': decisions')
        references(row.get('sources'), key+': sources', nonempty=row.get('state') != 'planned')
        references(row.get('tests'), key+': tests', nonempty=row.get('state') != 'planned')
        evs = row.get('evidence')
        require(isinstance(evs, list) and all(e in evidence for e in evs), key+': unknown evidence')
        if row.get('state') == 'accepted': require(bool(evs), key+': accepted capability needs scoped evidence')
    for key, row in work.items():
        for field in ('title', 'owner', 'delta', 'reuse', 'trigger', 'stop_condition', 'resources'):
            require(text(row.get(field)), key+': '+field+' required')
        if not isinstance(row.get('kind'), str) or row['kind'] not in KINDS:
            errors.append(key+': invalid work kind'); continue
        if not isinstance(row.get('state'), str) or row['state'] not in WORK_STATES:
            errors.append(key+': invalid work state'); continue
        if not all(ids(row.get(field)) for field in ('capabilities','depends_on','evidence','prior_evidence','reopens')):
            errors.append(key+': capability/dependency/evidence/reopen fields must be ID arrays'); continue
        targets = row.get('capabilities')
        require(isinstance(targets, list) and bool(targets) and all(c in caps for c in targets), key+': capability references required')
        require(isinstance(row.get('acceptance'), list) and bool(row['acceptance']) and all(text(x) for x in row['acceptance']), key+': finite acceptance required')
        require(isinstance(row.get('depends_on'), list) and all(x in work and x != key for x in row['depends_on']), key+': invalid dependency')
        evs = row.get('evidence')
        require(isinstance(evs, list) and all(x in evidence for x in evs), key+': unknown evidence')
        if row.get('state') == 'done': require(bool(evs), key+': done needs evidence')
        if row.get('state') == 'blocked': require(text(row.get('blocker')), key+': blocker required')
        if row.get('state') in {'in_progress', 'done'}:
            require(all(work.get(x, {}).get('state') == 'done' for x in row.get('depends_on', [])), key+': unfinished dependency')
        prior = row.get('prior_evidence')
        require(isinstance(prior, list) and all(x in evidence for x in prior), key+': invalid prior evidence')
        if row.get('state') in OPEN and any(caps.get(c, {}).get('evidence') for c in targets or []):
            require(bool(prior), key+': review prior evidence before reopening work')
        if row.get('kind') == 'feature' and any(caps.get(c, {}).get('state') == 'accepted' for c in targets or []):
            require(text(row.get('changed_boundary')), key+': feature on accepted capability needs a changed boundary')
        reopened = row.get('reopens')
        require(isinstance(reopened, list) and all(x in work and x != key and work[x].get('state') in {'done', 'cancelled'} for x in reopened), key+': invalid reopened task')
        if reopened:
            require(text(row.get('changed_boundary')) and bool(prior), key+': reopen needs changed boundary and prior evidence')
        if row.get('state') in OPEN:
            require(row.get('owner') != 'unassigned', key+': active/ready task needs owner')
    active = [k for k, row in work.items() if row.get('state') == 'in_progress']
    require(len(active) <= 1, 'WIP limit exceeded: '+', '.join(active))
    visiting, visited = set(), set()
    def visit(key):
        if key in visiting: errors.append('dependency cycle at '+key); return
        if key in visited: return
        visiting.add(key)
        deps = work[key].get('depends_on', [])
        for dep in deps if ids(deps) else []:
            if dep in work: visit(dep)
        visiting.remove(key); visited.add(key)
    for key in work: visit(key)
    return errors


def render(value):
    def clean(x): return str(x).replace('|', '\\|').replace('\n', ' ')
    def link(ref):
        label = ref.get('section') or ref['path']
        return '['+label+'](../'+ref['path']+')'
    def refs(rows): return '、'.join(link(x) for x in rows) or '—'
    lines = ['# 项目当前状态', '', '<!-- Generated by tools/check_project.py --write; edit project/registry.json. -->', '',
        '唯一状态源：[project/registry.json](../project/registry.json)。流程见 [CONTRIBUTING](../CONTRIBUTING.md#work-management)。', '',
        f"审阅日期：{value['updated']}；交付状态：`{value['delivery']}`。历史验收不等于当前工作树已发布。", '',
        '本表按能力组织；“限定范围已验收”只表示证据中的配置和版本通过。历史记录不因本轮未重测而失效，也不自动证明改动后的版本。', '',
        '进行中的执行项最多 1 个；候选任务不是执行承诺，不自动升级为下一轮工作。', '',
        '## 能力总览', '', '| 能力 | 状态 | 已有证据 | 范围与限制 |', '| --- | --- | --- | --- |']
    for c in value['capabilities']:
        lines.append('| '+ ' | '.join([c['id']+'：'+c['title'], LABELS[c['state']], '、'.join(c['evidence']) or '—', clean(c['scope'].rstrip('。；; ')+'；边界：'+c['limits'])])+' |')
    lines += ['', '## 执行与候选任务', '']
    if not any(w['state'] == 'in_progress' for w in value['work']): lines += ['当前没有执行中的系统开发/实验；选择下一项前先复核下面的增量、资源和终点。', '']
    for w in value['work']:
        lines += [f"### {w['id']}：{w['title']}", '',
            f"状态：{LABELS[w['state']]}；类型：`{w['kind']}`；负责人：`{w['owner']}`。", '',
            f"- 关联能力：{'、'.join(w['capabilities'])}；依赖：{'、'.join(w['depends_on']) or '无'}。",
            '- 新增价值：'+w['delta'], '- 复用：'+w['reuse'], '- 触发原因：'+w['trigger'],
            '- 既有证据：'+('、'.join(w['prior_evidence']) or '无'), '- 资源：'+w['resources'],
            '- 验收：'+'；'.join(w['acceptance']), '- 停止条件：'+w['stop_condition'],
            '- 完成证据：'+('、'.join(w['evidence']) or '未完成，不得推断通过'), '']
        if w.get('changed_boundary'): lines += ['变更边界：'+w['changed_boundary'], '']
        if w.get('blocker'): lines += ['阻塞条件：'+w['blocker'], '']
    lines += ['## 实现、测试与决策索引', '']
    for c in value['capabilities']:
        lines += ['### '+c['id'], '', '负责人：`'+c['owner']+'`；路线图：'+ '、'.join(c['roadmap'])+'。', '',
            '- 实现：'+refs(c['sources']), '- 回归/验收入口：'+refs(c['tests']), '- 决策：'+refs(c['decisions']), '']
    lines += ['## 证据索引', '', '报告保留原制品身份、原始记录位置和限制；台账只引用，不复制完整实验日志。结构检查不会执行 VM 或认证实验结论。', '']
    for e in value['evidence']:
        date_label = '运行日期' if e['date_kind'] == 'run' else '索引复核日期（非重测）'
        lines += ['### '+e['id'], '', f"{e['title']}；{date_label}：{e['date']}；层级：`{e['kind']}`；结论：`{e['verdict']}`。", '',
            '- 版本身份：'+e['identity'], '- 覆盖：'+e['scope'], '- 限制：'+e['limits'], '- 记录：'+refs(e['records']), '']
    return '\n'.join(lines)


def affected(root, value, changed):
    """Suggest scoped regression owners; never rewrite capability/work state."""
    result = []
    for c in value['capabilities']:
        matches = []
        for name in changed:
            if any(name == r['path'] or ((Path(root)/r['path']).is_dir() and name.startswith(r['path']+'/')) for r in c['sources']+c['tests']): matches.append(name)
        if matches: result.append(dict(capability=c['id'], changed=sorted(set(matches)), tests=[r['path'] for r in c['tests']], prior_evidence=c['evidence']))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--write', action='store_true', help='regenerate only docs/PROJECT_STATUS.md')
    parser.add_argument('--changed', action='append', default=[], help='repository-relative changed path for impact suggestions')
    parser.add_argument('--base', help='read-only git diff against this ref, including current tracked changes')
    args = parser.parse_args()
    root = args.project.resolve(); value = load(root)
    errors = validate(root, value)
    if errors: parser.exit(1, '\n'.join(errors)+'\n')
    view = root/VIEW; expected = render(value)
    if args.write: view.write_text(expected)
    if not view.is_file() or view.read_text() != expected: parser.exit(1, VIEW+' is stale; run tools/check_project.py --write\n')
    changed = list(args.changed)
    if args.base:
        # --end-of-options prevents a ref from being interpreted as a flag.
        raw = subprocess.run(['git','diff','--name-only','-z','--end-of-options',args.base,'--'], cwd=root,check=True,capture_output=True).stdout
        changed += [x for x in raw.decode().split('\0') if x]
    for name in changed:
        if Path(name).is_absolute() or '..' in Path(name).parts: parser.error('changed paths must be repository-relative')
    if args.base or args.changed: print(json.dumps(dict(impact=affected(root,value,changed),note='Review suggestions only; existing evidence and task states are unchanged'), ensure_ascii=False,indent=2))
    else: print(f"Project register passed: {len(value['capabilities'])} capabilities, {len(value['work'])} tasks; generated view current")


if __name__ == '__main__': main()
