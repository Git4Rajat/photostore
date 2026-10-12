"""Publish approved support with an isolated index; never stage the user's work."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[2]
    branch = sys.argv[1]
    if not branch.startswith('codex/'):
        raise ValueError('Expected a codex/ deployment branch')
    paths = ['.gitignore', 'frontend/package.json', 'frontend/package-lock.json',
             'frontend/benchmark.html', 'frontend/vite.benchmark.config.mjs', 'backend/benchmark_upload.py',
             'backend/tests/test_benchmark_upload.py', 'backend/tests/test_raw_finalize_fast_path.py',
             'docs/raw-upload-benchmark.md']
    paths.extend(str(path.relative_to(root)) for folder in ['frontend/src/benchmark', 'deploy/benchmark']
                 for path in (root / folder).rglob('*') if path.is_file())
    with tempfile.TemporaryDirectory(prefix='rawbench-index-') as temp:
        env = {**os.environ, 'GIT_INDEX_FILE': str(Path(temp) / 'index')}

        def git(*arguments):
            return subprocess.check_output(['git', *arguments], cwd=root, env=env, text=True).strip()

        existing = subprocess.run(['git', 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}'], cwd=root)
        if existing.returncode == 0:
            raise ValueError('Deployment branch already exists; choose a fresh name')
        git('read-tree', 'HEAD')
        git('apply', '--cached', '--directory=backend', 'deploy/benchmark/overlay.patch')
        for path in paths:
            git('add', '--', path)
        # The dispatchable workflow name must exist on the default branch. Only
        # this deployment branch replaces its body; default branch is untouched.
        blob = git('hash-object', '-w', 'deploy/benchmark/publish-images.yml')
        git('update-index', '--add', '--cacheinfo', f'100644,{blob},.github/workflows/publish-images.yml')
        tree = git('write-tree')
        commit = git('commit-tree', tree, '-p', git('rev-parse', 'HEAD'), '-m', 'Add isolated RAW upload benchmark overlays')
        git('update-ref', f'refs/heads/{branch}', commit)
        subprocess.run(['git', 'push', 'origin', f'{branch}:refs/heads/{branch}'], cwd=root, check=True)
        print(f'{branch} {commit}')


if __name__ == '__main__':
    main()
