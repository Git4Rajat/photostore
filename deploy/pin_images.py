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


def declared(text, kind):
    """Every value the template declares for ``kind`` ('capability', 'compatible' or 'commit'), in either
    syntax. The compiled template repeats each parameter (top level and nested deployment), and all of them
    must agree."""
    patterns = dict(capability=(CAP_BICEP, CAP_JSON), compatible=(COMPAT_BICEP, COMPAT_JSON),
                    commit=(COMMIT_BICEP, COMMIT_JSON))[kind]
    return [text[m.end(1):m.start(2)] for pattern in patterns for m in pattern.finditer(text)]


def compatible_capabilities(text, capability_id):
    """Earlier certificates this release may replace in place: the one currently pinned plus the ones it
    already listed, newest first. Only called for releases qualified for same-protocol upgrades."""
    current, listed = declared(text, 'capability'), declared(text, 'compatible')
    ids = current[:1] + (listed[0].split(',') if listed else [])
    seen = []
    for value in ids:
        if value and value != capability_id and value not in seen:
            seen.append(value)
    return ','.join(seen[:MAX_COMPATIBLE])


def pin(text, images, capability_id=None, source_commit=None, upgradable=False, compatible=None):
    """``compatible`` is the predecessor list to write. Callers pinning several templates must compute it
    once from a single source so every template carries the identical list; when omitted it is derived from
    this text alone, which is only correct for the Bicep sources."""
    text = REF.sub(lambda m: images[m.group(1)], text)
    if capability_id and upgradable:
        if compatible is None:
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


def check_consistent(texts):
    """Raise unless every template declares the same capability, predecessor list and source commit.

    An existing library stays bound to its old certificate until the new release lists it as a predecessor
    (UPLOAD_WAVE_COMPATIBLE_CAPABILITIES). If any template omits that list, a deployment from it is rejected
    by every role ("not qualified for the library wave protocol") until an operator migrates by hand."""
    problems = []
    for kind in ('capability', 'compatible', 'commit'):
        values = {name: declared(text, kind) for name, text in texts.items()}
        if any(not found for found in values.values()):
            problems.append(f'{kind}: not declared in ' + ', '.join(n for n, f in values.items() if not f))
        distinct = {v for found in values.values() for v in found}
        if len(distinct) > 1:
            problems.append(f'{kind}: templates disagree: ' + '; '.join(f'{n}={sorted(set(f))}' for n, f in values.items()))
    if problems:
        raise ValueError('Inconsistent wave pins: ' + ' | '.join(problems))


def pin_all(texts, release):
    """Pin every template to ``release``, returning the new texts. ``main.bicep`` is the single source for
    the predecessor chain. Raises, writing nothing, if the result is inconsistent or drops the certificate
    the previous release was bound to."""
    images, capability = release['images'], release.get('capabilityId')
    upgradable = release.get('upgradeProtocol') == release.get('protocol') is not None
    previous = (declared(texts['main.bicep'], 'capability') or [''])[0]
    compatible = compatible_capabilities(texts['main.bicep'], capability) if capability and upgradable else None
    pinned = {name: pin(text, images, capability, release.get('sourceCommit'), upgradable, compatible)
              for name, text in texts.items()}
    check_consistent(pinned)
    if capability and upgradable and previous and previous != capability:
        if previous not in (declared(pinned['main.bicep'], 'compatible') or [''])[0].split(','):
            raise ValueError('The new release does not list the previously pinned certificate as a predecessor')
    return pinned


if __name__ == '__main__':
    release = json.loads(Path(sys.argv[1]).read_text())
    texts = {name: (DEPLOY / name).read_text() for name in TARGETS}
    try:
        pinned = pin_all(texts, release)
    except ValueError as error:
        sys.exit(str(error))
    for name, text in pinned.items():
        (DEPLOY / name).write_text(text)
