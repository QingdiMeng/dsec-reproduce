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


def render(value, language='zh-CN', root=None):
    """Render both editions from identical IDs, states, dependencies and evidence."""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    catalog = json.loads((root/'project/status-translations.json').read_text())
    languages = json.loads((root/'docs/languages.json').read_text())
    primary = {p: lang for lang, names in languages.items() for p in names}
    en = language == 'en'
    if not en and language != 'zh-CN':
        raise ValueError(f'Unsupported status language: {language}')

    def label(english, chinese):
        return english if en else chinese

    def tr(message):
        native = 'zh-CN' if re.search(r'[\u3400-\u9fff]', message) else 'en'
        if native == language:
            return message
        if message not in catalog:
            raise ValueError(f'Missing {language} status translation: {message}')
        return catalog[message]

    def link(ref):
        path = ref['path']
        title = tr(ref['section']) if ref.get('section') else path
        if path in primary and primary[path] != language:
            path = str(Path(path).with_suffix(f'.{language}.md'))
        return f'[{title}](../{path})'

    joiner = label(', ', '、')
    def refs(rows):
        return joiner.join(link(ref) for ref in rows) or '—'

    def item(name, detail):
        return '- '+name+label(': ', '：')+detail

    states = {'accepted': 'Accepted within scope', 'partial': 'Partially implemented or verified',
              'implemented': 'Implemented, pending combined acceptance', 'planned': 'Planned',
              'candidate': 'Candidate', 'ready': 'Ready', 'in_progress': 'In progress',
              'blocked': 'Blocked', 'done': 'Done', 'cancelled': 'Cancelled'} if en else LABELS
    lines = [label('# Project status', '# 项目当前状态'), '',
             label('**English** · [简体中文](PROJECT_STATUS.md)', '[English](PROJECT_STATUS.en.md) · **简体中文**'), '',
             '<!-- Generated by tools/check_project.py --write. -->', '',
             label('The [register](../project/registry.json) is the source of project status. See [work management](../CONTRIBUTING.md#work-management).',
                   '项目状态来自[统一台账](../project/registry.json)。维护流程见[贡献指南](../CONTRIBUTING.zh-CN.md#work-management)。'), '',
             label('Review date', '审阅日期')+' '+value['updated']+' · '+label('Delivery', '交付状态')+' `'+value['delivery']+'`', '',
             label('Historical acceptance applies to the recorded revision and configuration. It survives an omitted rerun and does not establish that a changed working tree has passed or shipped.',
                   '验收证据适用于记录中的版本和配置。本轮未重测的历史记录仍有效；改动后的工作树需另行验证和发布。'), '',
             label('At most one item can be in progress. Candidates require an explicit decision before execution.',
                   '最多一项工作可以执行。候选项须经过明确选择才能开始。'), '',
             label('## Capabilities', '## 能力总览'), '',
             label('| Capability | Status | Evidence | Scope and limits |', '| 能力 | 状态 | 已有证据 | 范围与限制 |'),
             '| --- | --- | --- | --- |']
    for c in value['capabilities']:
        scope = tr(c['scope'])+' '+label('Limits: ', '边界：')+tr(c['limits'])
        cells = [c['id']+' — '+tr(c['title']), states[c['state']], joiner.join(c['evidence']) or '—', scope]
        lines.append('| '+' | '.join(s.replace('|', '\\|').replace('\n', ' ') for s in cells)+' |')
    lines += ['', label('## Work items and candidates', '## 执行与候选任务'), '']
    if not any(w['state'] == 'in_progress' for w in value['work']):
        lines += [label('No development or experiment is in progress. Review the increment, resources and stop condition before selecting work.',
                        '当前没有执行中的开发或实验。选择下一项前，请复核增量、资源和停止条件。'), '']
    for w in value['work']:
        lines += ['<a id="'+w['id'].lower()+'"></a>', '',
                  '### '+w['id']+label(' — ', '：')+tr(w['title']), '',
                  states[w['state']]+' · `'+w['kind']+'` · `'+w['owner']+'`', '',
                  item(label('Capabilities','关联能力'), joiner.join(w['capabilities'])),
                  item(label('Dependencies','依赖'), joiner.join(w['depends_on']) or '—'),
                  item(label('Increment','新增价值'), tr(w['delta'])), item(label('Reuse','复用'), tr(w['reuse'])),
                  item(label('Trigger','触发原因'), tr(w['trigger'])),
                  item(label('Prior evidence','既有证据'), joiner.join(w['prior_evidence']) or '—'),
                  item(label('Resources','资源'), tr(w['resources'])),
                  item(label('Acceptance','验收'), label(' / ', '；').join(tr(s) for s in w['acceptance'])),
                  item(label('Stop condition','停止条件'), tr(w['stop_condition'])),
                  item(label('Completion evidence','完成证据'), joiner.join(w['evidence']) or label('Pending', '待验收')), '']
        for key, name in [('changed_boundary', label('Changed boundary','变更边界')), ('blocker', label('Blocker','阻塞条件'))]:
            if w.get(key):
                lines += [item(name, tr(w[key])), '']
    lines += [label('## Implementation, tests and decisions', '## 实现、测试与决策索引'), '']
    for c in value['capabilities']:
        lines += ['### '+c['id'], '', label('Owner','负责人')+' `'+c['owner']+'` · '+label('Roadmap','路线图')+' '+joiner.join(c['roadmap']), '',
                  item(label('Implementation','实现'), refs(c['sources'])), item(label('Tests and acceptance','回归与验收'), refs(c['tests'])),
                  item(label('Decisions','决策'), refs(c['decisions'])), '']
    lines += [label('## Evidence index','## 证据索引'), '',
              label('Reports retain artifact identities, raw evidence locations and limits. Register checks validate structure only. They neither run VMs nor certify experimental results.',
                    '报告保留制品身份、原始证据位置和限制。台账只引用记录。结构检查不会启动 VM，也不能据此认证实验结果。'), '']
    for e in value['evidence']:
        date_kind = (label('Run date','运行日期') if e['date_kind'] == 'run' else label('Index review date (no rerun)','索引复核日期（非重测）'))
        lines += ['### '+e['id'], '', tr(e['title'])+' · '+date_kind+' '+e['date']+' · `'+e['kind']+'` · `'+e['verdict']+'`', '',
                  item(label('Artifact and configuration','版本身份'), tr(e['identity'])),
                  item(label('Coverage','覆盖'), tr(e['scope'])), item(label('Limits','限制'), tr(e['limits'])),
                  item(label('Records','记录'), refs(e['records'])), '']
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
    parser.add_argument('--write', action='store_true', help='regenerate both language editions of project status')
    parser.add_argument('--changed', action='append', default=[], help='repository-relative changed path for impact suggestions')
    parser.add_argument('--base', help='read-only git diff against this ref, including current tracked changes')
    args = parser.parse_args()
    root = args.project.resolve(); value = load(root)
    errors = validate(root, value)
    if errors: parser.exit(1, '\n'.join(errors)+'\n')
    try:
        views = {root/VIEW: render(value, 'zh-CN', root),
                 root/'docs/PROJECT_STATUS.en.md': render(value, 'en', root)}
    except ValueError as exc:
        parser.exit(1, str(exc)+'\n')
    for view, expected in views.items():
        if args.write:
            view.write_text(expected)
        if not view.is_file() or view.read_text() != expected:
            parser.exit(1, f'{view.name} is stale; run tools/check_project.py --write\n')
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
