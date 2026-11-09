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


COMPAT_BICEP = re.compile(r"(param waveCompatibleCapabilities string = ')[a-f0-9,]*(')")
COMPAT_JSON = re.compile(r'("waveCompatibleCapabilities": \{\s*"type": "string",\s*"defaultValue": ")[a-f0-9,]*(")')
COMMIT_BICEP = re.compile(r"(param waveSourceCommit string = ')[a-f0-9]*(')")
COMMIT_JSON = re.compile(r'("waveSourceCommit": \{\s*"type": "string",\s*"defaultValue": ")[a-f0-9]*(")')
MAX_COMPATIBLE = 8


def compatible_capabilities(text, capability_id):
    """Earlier certificates this release may replace in place: the one currently pinned plus the ones it
    already listed, newest first. Only called for releases qualified for same-protocol upgrades."""
    current = CAP_BICEP.search(text)
    listed = COMPAT_BICEP.search(text)
    ids = ([current.group(0).split("'")[1]] if current else []) + (listed.group(0).split("'")[1].split(',') if listed else [])
    seen = []
    for value in ids:
        if value and value != capability_id and value not in seen:
            seen.append(value)
    return ','.join(seen[:MAX_COMPATIBLE])


def pin(text, images, capability_id=None, source_commit=None, upgradable=False):
    text = REF.sub(lambda m: images[m.group(1)], text)
    if capability_id and upgradable:
        compatible = compatible_capabilities(text, capability_id)
        text = COMPAT_BICEP.sub(lambda m: m.group(1) + compatible + m.group(2), text)
        text = COMPAT_JSON.sub(lambda m: m.group(1) + compatible + m.group(2), text)
    if source_commit:
        text = COMMIT_BICEP.sub(lambda m: m.group(1) + source_commit + m.group(2), text)
        text = COMMIT_JSON.sub(lambda m: m.group(1) + source_commit + m.group(2), text)
    if capability_id:
        text = CAP_BICEP.sub(lambda m: m.group(1) + capability_id + m.group(2), text)
        text = CAP_JSON.sub(lambda m: m.group(1) + capability_id + m.group(2), text)
    return text


if __name__ == '__main__':
    release = json.loads(Path(sys.argv[1]).read_text())
    images = release['images']
    for name in TARGETS:
        path = DEPLOY / name
        path.write_text(pin(path.read_text(), images, release.get('capabilityId'), release.get('sourceCommit'),
                            release.get('upgradeProtocol') == release.get('protocol') is not None))
