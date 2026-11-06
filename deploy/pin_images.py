"""Pin the one-click "Deploy to Azure" image defaults to the immutable digests
of a qualified release (wave-release.json), so a fresh deploy never follows
:latest and a later publish can't silently upgrade it. Stdlib only."""
import json
import re
import sys
from pathlib import Path

DEPLOY = Path(__file__).parent
TARGETS = ('main.bicep', 'resources.bicep', 'azuredeploy.json')
REF = re.compile(r'ghcr\.io/[a-z0-9_./-]+/photostore-(backend|frontend|ipworker)(?::[A-Za-z0-9_.-]+|@sha256:[a-f0-9]{64})')


def pin(text, images):
    return REF.sub(lambda m: images[m.group(1)], text)


if __name__ == '__main__':
    release = json.loads(Path(sys.argv[1]).read_text())
    images = release['images']
    for name in TARGETS:
        path = DEPLOY / name
        path.write_text(pin(path.read_text(), images))
