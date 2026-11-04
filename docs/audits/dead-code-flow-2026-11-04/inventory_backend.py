"""Conservative backend name-reference candidates; never a deletion allow-list.

Includes references in strings/comments (useful for getattr and dispatch tables).
Does not infer dynamic name construction or external consumers.
"""
import ast
import collections
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
files = list((ROOT / 'backend').rglob('*.py'))
production = [p for p in files if 'tests' not in p.parts and 'scripts' not in p.parts]
text = {p: p.read_text() for p in files}
tokens = collections.Counter(re.findall(r'\b[A-Za-z_]\w*\b', '\n'.join(text[p] for p in production)))
test_text = '\n'.join(text[p] for p in files if 'tests' in p.parts)
script_text = '\n'.join(text[p] for p in files if 'scripts' in p.parts)
external_text = '\n'.join(p.read_text() for base in ['deploy', '.github']
    for p in (ROOT / base).rglob('*') if p.is_file() and p.suffix in {'.py', '.sh', '.yml', '.yaml', '.bicep'})
hooks = {'post_fork', 'post_worker_init', 'worker_exit'}
qualification = {'ClusteringJournal', 'run_wave_assignment', '_load_wave_faces'}
result = []
for file in production:
    for node in ast.parse(text[file]).body:
        if (not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                or node.decorator_list or tokens[node.name] != 1):
            continue
        pattern = r'\b' + re.escape(node.name) + r'\b'
        scripts = len(re.findall(pattern, script_text))
        external = len(re.findall(pattern, external_text))
        tests = len(re.findall(pattern, test_text))
        classification = ('gunicorn_runtime_hook' if file.name == 'gunicorn.conf.py' and node.name in hooks
            else 'qualification_harness' if node.name in qualification
            else 'operational_reference' if scripts or external
            else 'candidate_test_only' if tests else 'candidate_unreferenced')
        result.append(dict(file=str(file.relative_to(ROOT)), line=node.lineno,
            lines=node.end_lineno-node.lineno+1, name=node.name, testMentions=tests,
            operationalMentions=scripts+external, classification=classification))
print(json.dumps(dict(method='Top-level undecorated definitions with one textual name occurrence in production backend Python.',
    definitions=result), indent=2))
