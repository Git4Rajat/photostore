#!/usr/bin/env python3
"""Build a queue scaler patch from a RAW ARM GET, preserving identity/metadata.

Offline preparation only. Do not use `az containerapp show` projections as the
input. Review the generated diff, then apply with `az rest --method patch` and
verify KEDA events and actual queue processing. A new ARM snapshot is required
before preparing each change; never restore an old whole-template snapshot.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path


def build_patch(resource, rule_name, queue_length, *, max_replicas=None):
    if type(queue_length) is not int or queue_length < 1:
        raise ValueError('Queue length must be a positive integer')
    template = deepcopy(resource['properties']['template'])
    scale = template['scale']
    if scale.get('minReplicas', 0) != 0:
        raise ValueError('Wave apps must preserve scale to zero')
    matches = [r for r in scale['rules'] if r.get('name') == rule_name]
    if len(matches) != 1 or matches[0].get('custom', {}).get('type') != 'azure-queue':
        raise ValueError('Select exactly one existing Azure Queue rule')
    for rule in scale['rules']:
        custom = rule.get('custom', {})
        if custom.get('type') != 'azure-queue':
            continue
        metadata = custom.get('metadata', {})
        if not all(metadata.get(k) for k in ('accountName', 'queueName', 'queueLength', 'queueLengthStrategy')):
            raise ValueError('Raw ARM queue metadata is incomplete')
        if not custom.get('identity') and not custom.get('auth'):
            raise ValueError('Raw ARM scaler identity/authentication is missing')
        identity = custom.get('identity')
        assigned = resource.get('identity', {})
        if identity == 'system' and 'SystemAssigned' not in assigned.get('type', ''):
            raise ValueError('Scaler requires the app system identity')
        if identity and identity != 'system' and identity not in assigned.get('userAssignedIdentities', {}):
            raise ValueError('Scaler user identity is not assigned to the app')
    matches[0]['custom']['metadata']['queueLength'] = str(queue_length)
    if max_replicas is not None:
        if type(max_replicas) is not int or max_replicas < 1:
            raise ValueError('Maximum replicas must be a positive integer')
        scale['maxReplicas'] = max_replicas
    # An explicit old suffix would try to recreate an already named revision.
    template.pop('revisionSuffix', None)
    return dict(properties=dict(template=template))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resource-file', required=True, help='JSON from az rest GET, including resource identity')
    parser.add_argument('--output', required=True)
    parser.add_argument('--rule', required=True)
    parser.add_argument('--queue-length', required=True, type=int)
    parser.add_argument('--max-replicas', type=int)
    args = parser.parse_args()
    resource = json.loads(Path(args.resource_file).read_text())
    patch = build_patch(resource, args.rule, args.queue_length, max_replicas=args.max_replicas)
    path = Path(args.output)
    with path.open('x', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(patch, stream, indent=2)
        stream.write('\n')
    print(json.dumps(dict(resourceId=resource['id'], rule=args.rule,
        before=resource['properties']['template']['scale'], after=patch['properties']['template']['scale'])))


if __name__ == '__main__':
    main()
