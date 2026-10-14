"""Offline validation of browser receipts; does not call Azure or upload originals."""
import argparse
import hashlib
import io
import json
import subprocess
from pathlib import Path

from PIL import Image
from video_metadata import decode_video_metadata


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 2)


def verify(root, audit_name='audit.json', result_name='results.json', exiftool=None):
    evidence = root / 'browser-evidence'
    audit = json.loads((evidence / audit_name).read_text())
    sources = {row['name']: row for row in json.loads((root / 'manifest.json').read_text())['rows']}
    rows = []
    raw_categories = {'redcode_raw', 'redcode_raw_newer', 'canon_cinema_raw_light_cut',
                      'canon_cinema_raw_light', 'nikon_n_raw', 'nikon_r3d_ne',
                      'blackmagic_raw', 'prores_raw', 'arriraw_mxf'}
    for result in audit['rows']:
        name = result['name']
        source = sources[name]
        original = root / name
        digest = hashlib.sha256()
        with original.open('rb') as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        assert digest.hexdigest() == source['sha256'], name + ': original changed'
        report = result['pipeline']['preparationReport']
        assert report['assets']['preview']['target'] is False
        row = {'name': name, 'category': source['category'], 'bytes': source['bytes'],
               'group': 'negative' if source['category'].startswith('negative_') else 'raw' if source['category'] in raw_categories else 'ordinary',
               'preparationMs': report['elapsedMs'], 'metadataAccepted': False, 'thumbnailAccepted': False}
        if result.get('metadata'):
            received = (evidence / (name + '.metadata.bin')).read_bytes()
            assert received == (evidence / (name + '.json')).read_bytes()
            descriptor = result['pipeline']['clientMetadata']
            assert descriptor['sourceSha256'] == source['sha256']
            try:
                tags = decode_video_metadata(received, descriptor, source['bytes'])
            except ValueError as error:
                raise AssertionError(name + ': backend rejected packet: ' + str(error)) from error
            row.update(metadataAccepted=True, metadataBytes=len(received),
                       metadataReadBytes=result['metadata']['readBytes'], metadataReadCalls=result['metadata']['readCalls'],
                       readBudgetReached=tags.get('MetadataReadBudgetReached') == 'true',
                       metadataGenerationMs=report['assets']['metadata']['generationMs'],
                       tags=tags)
            assert row['metadataReadBytes'] <= 32 * 1024 * 1024
            assert row['metadataReadCalls'] <= 2048
        else:
            row['metadataFailure'] = report['assets']['metadata'].get('generationReason')
            assert 'clientMetadata' not in result['pipeline']
        if result.get('automatic'):
            received = (evidence / (name + '.thumbnail.bin')).read_bytes()
            assert received == (evidence / (name + '.jpg')).read_bytes()
            assert 0 < len(received) <= 512000
            with Image.open(io.BytesIO(received)) as image:
                assert image.format == 'JPEG' and max(image.size) <= 120
                extrema = image.convert('RGB').getextrema()
                nonblank = max(high for low, high in extrema) - min(low for low, high in extrema) > 20
                row.update(thumbnailAccepted=True, thumbnailBytes=len(received), dimensions=list(image.size), nonblank=nonblank,
                           thumbnailGenerationMs=report['assets']['thumbnail']['generationMs'])
                runtime = report['assets']['thumbnail'].get('runtime')
                row['thumbnailRuntime'] = runtime
                if runtime == 'embedded_video_jpeg':
                    row['thumbnailReadBytes'] = report['assets']['thumbnail']['readBytes']
                    row['thumbnailReadCalls'] = report['assets']['thumbnail']['readCalls']
                    assert row['thumbnailReadBytes'] <= 4 * 1024 * 1024
                    assert row['thumbnailReadCalls'] <= 2048
                    if exiftool:
                        source_jpeg = (evidence / (name + '.embedded-source.jpg')).read_bytes()
                        matches = False
                        for field in ('ThumbnailImage', 'PreviewImage'):
                            reference_jpeg = subprocess.check_output([exiftool, '-b', '-' + field, str(original)])
                            if source_jpeg and reference_jpeg.startswith(source_jpeg):
                                row['embeddedReferenceTag'] = field
                                matches = True
                                break
                        assert matches, name + ': embedded source mismatch'
                        with Image.open(io.BytesIO(source_jpeg)) as source_image:
                            assert source_image.format == 'JPEG'
                            row['embeddedSourceDimensions'] = list(source_image.size)
                        row['embeddedSourceMatchesExifTool'] = True
                if row['group'] != 'negative':
                    assert nonblank, name + ': low-contrast thumbnail needs manual review'
        else:
            row['thumbnailFailure'] = report['assets']['thumbnail'].get('generationReason')
            assert 'thumbnail' not in result['pipeline'].get('clientProcessing', {})
        if exiftool:
            # Local ground truth only; this is not part of deployed video processing.
            reference = json.loads(subprocess.check_output([
                exiftool, '-j', '-n', '-Make', '-Model', '-ImageWidth', '-ImageHeight',
                '-Duration', '-VideoFrameRate', '-CreateDate', '-DateTimeOriginal',
                '-GPSLatitude', '-GPSLongitude', '-Warning', str(original),
            ], text=True))[0]
            reference.pop('SourceFile', None)
            row['localExifToolReference'] = reference
            tags = row.get('tags', {})
            row['missingCameraIdentityFields'] = [
                key for key in ('Make', 'Model') if reference.get(key) and not tags.get(key)
            ]
        rows.append(row)
    by_name = {row['name']: row for row in rows}
    if 'C0023S01.mxf' in by_name:
        assert by_name['C0023S01.mxf']['dimensions'] == [120, 68], 'Anamorphic geometry regression'
    if 'dv84.mov' in by_name:
        assert by_name['dv84.mov']['dimensions'] == [68, 120], 'Portrait rotation regression'
        assert by_name['dv84.mov']['tags']['Model'] == 'iPhone 12 mini'
    if 'Close-Up_with_Skintones.mov' in by_name:
        assert by_name['Close-Up_with_Skintones.mov']['readBudgetReached'] is True
    if 'mov-3elist-encrypted.mov' in by_name:
        assert not by_name['mov-3elist-encrypted.mov']['thumbnailAccepted'], 'Encrypted pixels must not be claimed decoded'
    if 'RRSF49-short.webm' in by_name:
        assert by_name['RRSF49-short.webm']['nonblank'] is False, 'The flat-black reference must remain black'
    summary = {}
    for group in ['ordinary', 'raw', 'negative']:
        selected = [row for row in rows if row['group'] == group]
        summary[group] = {'files': len(selected), 'metadataAccepted': sum(row['metadataAccepted'] for row in selected),
            'thumbnailAccepted': sum(row['thumbnailAccepted'] for row in selected),
            'partialMetadata': sum(row.get('readBudgetReached', False) for row in selected),
            'preparationP50Ms': percentile([row['preparationMs'] for row in selected], .5),
            'preparationP95Ms': percentile([row['preparationMs'] for row in selected], .95)}
    output = {'cloudUploads': 0, 'originalUploads': 0, 'localSidecarReceiptsOnly': True,
              'browser': audit['browser'], 'summary': summary, 'rows': rows}
    (root / result_name).write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps({'browser': audit['browser'], 'summary': summary}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', type=Path)
    parser.add_argument('--audit', default='audit.json')
    parser.add_argument('--output', default='results.json')
    parser.add_argument('--exiftool', help='Optional local reference executable; never used by the upload pipeline')
    args = parser.parse_args()
    verify(args.corpus, args.audit, args.output, args.exiftool)
