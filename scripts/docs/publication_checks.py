"""Check public text and explicitly declared evidence derivatives."""
import copy
import hashlib
import json
from pathlib import Path
import re


GENERIC_ACCOUNTS = {'dev', 'ocudu', 'user', 'username', 'runner', 'ubuntu', 'root', 'USER'}
HOME_PATH = re.compile(r'/(Users|home)/([A-Za-z0-9_.-]+)')
PROCESS_TEXT = re.compile(
    r'user[- ](?:approved|authorized|requested|confirmed|owned)\b|'
    r"the user[’']s (?:instruction|request)|the user explicitly|"
    r'AskUserQuestion|(?:parallel|multi)-agent review|agent misread|'
    r'conversation context|AGENT_GOAL\.mimo\.md|CLAUDE\.md|'
    r'Claude 세션|다른 에이전트|사용자 (?:승인|결정)', re.I)


def text_issues(path, *, governance=False):
    """Report categories and line numbers, never matched personal values."""
    try:
        text = path.read_bytes().decode('utf-8')
    except UnicodeDecodeError:
        return []
    if '\0' in text:
        return []
    issues = []
    for number, line in enumerate(text.splitlines(), 1):
        if any(m[1] == 'Users' or m[2] not in GENERIC_ACCOUNTS for m in HOME_PATH.finditer(line)):
            issues.append(f'{number}: personal home path')
        if not governance and PROCESS_TEXT.search(line):
            issues.append(f'{number}: conversation-specific narration')
    return issues


def public_text_errors(repo, site):
    errors = []
    roots = ['docs', 'apps', 'include', 'src', 'scripts', 'tests', 'integrations',
             'use_cases', 'benchmarks', 'archive']
    paths = [p for name in roots for p in (repo / name).rglob('*') if p.is_file()]
    paths += [repo / name for name in ['README.md', 'AGENT.md', 'AGENT_GOAL.md',
                                     'AGENT_HARNESS.md', 'AGENT_PROGRESS.md']]
    paths += [p for p in site.rglob('*') if p.is_file()]
    for path in paths:
        if any(part in {'__pycache__', '.pytest_cache', '.build', '.venv-docs'} for part in path.parts):
            continue
        # Policy files and the scanner's literal signatures are intentional.
        governance = (path.parent == repo and path.name.startswith('AGENT')) or path == repo / 'scripts/docs/publication_checks.py'
        display = path.relative_to(site) if path.is_relative_to(site) else path.relative_to(repo)
        errors.extend(f'{display}:{issue}' for issue in text_issues(path, governance=governance))
    return errors


def evidence_payload_digest(value, fields):
    """Hash all JSON values except the explicitly enumerated path metadata."""
    value = copy.deepcopy(value)
    for field in fields:
        parts = [p.replace('~1', '/').replace('~0', '~') for p in field['pointer'].split('/')[1:]]
        parent = value
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
        parent[key] = None
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def evidence_hashes(repo, inventory):
    """Return expected public hashes while retaining the original inventory."""
    originals = {r['destination']: r['sha256'] for r in inventory['files'] if r['kind'] == 'evidence'}
    expected = dict(originals)
    manifest = json.loads((repo / 'docs/_compat/redactions.json').read_text())
    seen, errors = set(), []
    for record in manifest['files']:
        name = record['path']
        if name in seen or name not in originals or record['original_sha256'] != originals[name]:
            errors.append(f'invalid evidence derivative provenance: {name}')
            continue
        seen.add(name)
        try:
            path = repo / name
            data = json.loads(path.read_text())
            fields = record['path_fields']
            pointers = [field['pointer'] for field in fields]
            if not fields or len(set(pointers)) != len(pointers):
                raise ValueError('missing or repeated path fields')
            for field in fields:
                value = data
                for part in field['pointer'].split('/')[1:]:
                    part = part.replace('~1', '/').replace('~0', '~')
                    value = value[int(part)] if isinstance(value, list) else value[part]
                if not isinstance(value, str) or value.startswith('/') or '${HOME}' in value:
                    raise ValueError('path metadata must be relative')
                if hashlib.sha256(value.encode()).hexdigest() != field['value_sha256']:
                    raise ValueError('path field changed')
            if evidence_payload_digest(data, fields) != record['measurement_sha256']:
                raise ValueError('measurement payload changed')
            expected[name] = record['public_sha256']
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            errors.append(f'invalid evidence derivative content: {name}')
    return expected, errors
