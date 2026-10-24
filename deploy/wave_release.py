"""Portable release contract for the fresh Azure CLI deployment (stdlib only)."""
import hashlib
import json
import re

PROTOCOL = 'library-wave-v1'
ROLES = ('core','upload','vision','cluster','index-worker','wave-recovery','archive','recovery')
IMAGE = re.compile(r'ghcr\.io/[a-z0-9_./-]+@sha256:[a-f0-9]{64}')


def release_contract(images, source):
    if set(images) != {'backend','frontend','ipworker'} or not re.fullmatch(r'[a-f0-9]{40}',source):
        raise ValueError('One source commit and all three release images are required')
    if any(not IMAGE.fullmatch(image) for image in images.values()):
        raise ValueError('Release images must be immutable GHCR digests, not tags')
    deployments = {role: images['ipworker'] if role=='vision' else images['backend'] for role in ROLES}
    body = json.dumps([PROTOCOL,deployments],sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return dict(protocol=PROTOCOL,sourceCommit=source,images=images,deployments=deployments,
                capabilityId=hashlib.sha256(body.encode()).hexdigest())


def validate_release(release):
    expected = release_contract(release['images'],release['sourceCommit'])
    if any(release.get(k)!=v for k,v in expected.items()):
        raise ValueError('Release capability or role images were changed')
    if release.get('qualification') != 'wave-unit-tests-passed':
        raise ValueError('Use the wave-release artifact from the successful qualification job')
    return expected


if __name__ == '__main__':
    import argparse
    from pathlib import Path
    parser=argparse.ArgumentParser();parser.add_argument('--image-dir',required=True);parser.add_argument('--source',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();records=[json.loads(p.read_text()) for p in Path(args.image_dir).glob('*.json')]
    if len(records)!=3 or any(r['sourceCommit']!=args.source for r in records):
        raise ValueError('Mixed source commits or incomplete image publication')
    release=release_contract({r['name']:r['image'] for r in records},args.source)
    release['qualification']='wave-unit-tests-passed'
    release['upgradeProtocol']=PROTOCOL
    Path(args.output).write_text(json.dumps(release,indent=2)+'\n')
