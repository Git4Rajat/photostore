"""Deploy pinned overlays to the authorized dev apps, preserving other config."""
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import requests

SUBSCRIPTION = 'c14d0e72-4b72-429d-bf2a-8cede087b9a8'
GROUP = 'forenkladev-rg'
BACKEND = 'ghcr.io/git4rajat/photostore-backend@sha256:762f814c90bc4c01e2bca02bb11adae783c9f663f75935e7da669c5910364d49'
VISION = 'ghcr.io/git4rajat/photostore-ipworker@sha256:4cf28c370bc32e549e346c2c6a2f4cb02ddb1d69e82135c1e60742a200802079'
WEB = 'ghcr.io/git4rajat/photostore-frontend@sha256:d2b16c9753c7cc46d6ba7fc8175e5de07a4f4f082f8bdee7a4eed2dca9b4e4d4'
TARGETS = [('recovery', 'backend', BACKEND), ('core', 'backend', BACKEND),
           ('archive', 'backend', BACKEND), ('cluster', 'backend', BACKEND),
           ('vision', 'ipworker', VISION), ('upload', 'backend', BACKEND),
           ('web', 'frontend', WEB)]
QUERY = '{name:name,image:properties.template.containers[0].image,revision:properties.latestReadyRevisionName,latest:properties.latestRevisionName,running:properties.runningStatus,provisioning:properties.provisioningState}'


def az(*arguments):
    return json.loads(subprocess.check_output(['az', *arguments, '-o', 'json'], text=True))


def pinned_image(package, tag):
    repository = f'git4rajat/photostore-{package}'
    response = requests.get('https://ghcr.io/token', params={'service': 'ghcr.io', 'scope': f'repository:{repository}:pull'}, timeout=60)
    response.raise_for_status()
    token = response.json()['token']
    response = requests.get(f'https://ghcr.io/v2/{repository}/manifests/{tag}', headers={
        'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'}, timeout=60)
    response.raise_for_status()
    digest = response.headers.get('Docker-Content-Digest', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise RuntimeError('Registry did not return a verified image digest')
    return f'ghcr.io/{repository}@{digest}'


def main():
    tag, output = sys.argv[1:]
    if not re.fullmatch(r'rawbench-[a-z0-9-]+', tag):
        raise ValueError('Expected a unique rawbench image tag')
    target_file = Path(output)
    if target_file.exists():
        raise ValueError('Refusing to overwrite deployment evidence')
    if az('account', 'show', '--query', 'id') != SUBSCRIPTION:
        raise RuntimeError('Azure subscription does not match the authorized dev target')
    images = {package: pinned_image(package, tag) for package in ['backend', 'ipworker', 'frontend']}
    before = {}
    for role, _, expected in TARGETS:
        item = az('containerapp', 'show', '-g', GROUP, '-n', f'forenkladev-{role}', '--query', QUERY)
        if item['image'] != expected or item['running'] != 'Running':
            raise RuntimeError(f'Dev app changed before deployment: {role}')
        before[role] = item
    evidence = {'subscription': SUBSCRIPTION, 'group': GROUP, 'tag': tag, 'images': images, 'before': before, 'after': {}}
    target_file.write_text(json.dumps(evidence, indent=2) + '\n')
    for role, package, _ in TARGETS:
        arguments = ['containerapp', 'update', '-g', GROUP, '-n', f'forenkladev-{role}', '--image', images[package], '--query', QUERY]
        if role == 'upload':
            arguments.extend(['--set-env-vars', 'BENCHMARK_UPLOAD_EMAIL=rajatverma57@gmail.com'])
        print(json.dumps({'stage': 'deploying', 'role': role}), flush=True)
        az(*arguments)
        deadline = time.monotonic() + 600
        while True:
            item = az('containerapp', 'show', '-g', GROUP, '-n', f'forenkladev-{role}', '--query', QUERY)
            if item['image'] == images[package] and item['revision'] == item['latest'] and item['provisioning'] == 'Succeeded':
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f'New revision not ready: {role}')
            time.sleep(15)
        evidence['after'][role] = item
        target_file.write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps({'stage': 'deployed', 'role': role, 'revision': item['revision']}), flush=True)


if __name__ == '__main__':
    main()
