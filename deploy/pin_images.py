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


# The capability ID is the digest of the pinned images, so it moves with them. Bicep sources carry it as a
# param default; the compiled template as the parameter's defaultValue.
CAP_BICEP = re.compile(r"(param waveCapabilityId string = ')[a-f0-9]*(')")
CAP_JSON = re.compile(r'("waveCapabilityId": \{\s*"type": "string",\s*"defaultValue": ")[a-f0-9]*(")')


def pin(text, images, capability_id=None):
    text = REF.sub(lambda m: images[m.group(1)], text)
    if capability_id:
        text = CAP_BICEP.sub(lambda m: m.group(1) + capability_id + m.group(2), text)
        text = CAP_JSON.sub(lambda m: m.group(1) + capability_id + m.group(2), text)
    return text


if __name__ == '__main__':
    release = json.loads(Path(sys.argv[1]).read_text())
    images = release['images']
    for name in TARGETS:
        path = DEPLOY / name
        path.write_text(pin(path.read_text(), images, release.get('capabilityId')))
