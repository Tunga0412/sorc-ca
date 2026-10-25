"""Validate public aggregate exports before creating a GitHub Pages artifact.

This checks structure and deployment eligibility. It never fetches source data
or recalculates scientific intervals. Stale Live observations remain visible
with the existing age warnings; they do not block unrelated website updates.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path
import shutil
from public_data_reader import FIVE, public_constant

LIVE = 'sorctracks_live.html'
HISTORICAL = 'sorctracks_tool.html'
SOURCE = 'https://www.albertahealthservices.ca/br/Page17594.aspx'
STATUSES = {'active_now', 'scheduled', 'unclear'}
STATIC_SUFFIXES = {'.html', '.ico', '.png', '.jpg', '.jpeg', '.webp', '.svg', '.css', '.js', '.json', '.txt', '.xml', '.pdf', '.woff', '.woff2'}
EXCLUDED_ROOTS = {'automation', 'sorcshortages', '.github', '.git', '_site'}

def require(condition, message):
    if not condition:
        raise ValueError(message)

def aware(value):
    require(type(value) is str, 'Timestamp must be a string')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.utcoffset() is not None, 'Timestamp lacks an offset')
    return result

def number(value):
    return type(value) in (int, float) and math.isfinite(value)

def count(value, expected, name):
    require(type(value) is int and value == expected, 'Inconsistent ' + name)

def validate_live(raw, *, now=None):
    require(type(raw) is bytes and 1 <= len(raw) <= 8 * 1024 * 1024, 'Live size outside bound')
    text = raw.decode('utf-8')
    data = public_constant(text, 'LIVE_DATA')
    require(type(data) is dict, 'Live data is not an object')
    require(type(data.get('schema_version')) is int and data['schema_version'] == 8, 'Unreviewed Live schema')
    require(data.get('demo_fixture') is False, 'Live fixture or missing provenance flag')
    now = now or datetime.now(timezone.utc)
    for key in ('source_fetched_at', 'generated_at', 'as_of_timestamp'):
        require(aware(data.get(key)) <= now + timedelta(hours=1), 'Live timestamp is in the future')
    require(data.get('source_url') == SOURCE, 'Unexpected Live source URL')
    require(type(data.get('source_page_count')) is int and 1 <= data['source_page_count'] <= 20, 'Invalid source-page census')
    require(all(marker in text for marker in ('tile.openstreetmap.org', 'basemaps.cartocdn.com', 'backupTiles', 'tileerror')), 'Map fallback missing')
    sites, unmapped = data.get('sites'), data.get('unmapped_disruptions')
    require(type(sites) is list and type(unmapped) is list and len(sites) <= 500, 'Invalid Live site arrays')
    names, items = set(), []
    for site in sites:
        require(type(site) is dict and type(site.get('name')) is str and site['name'] and site['name'] not in names, 'Duplicate or invalid mapped site')
        names.add(site['name'])
        require(number(site.get('lat')) and -90 <= site['lat'] <= 90 and number(site.get('lng')) and -180 <= site['lng'] <= 180, 'Invalid map coordinate')
        rows = site.get('disruptions')
        require(type(rows) is list and rows, 'Mapped site has no notices')
        count(site.get('disruption_count'), len(rows), 'site notice count')
        count(site.get('active_now_count'), sum(x.get('status') == 'active_now' for x in rows if type(x) is dict), 'site active count')
        count(site.get('scheduled_count'), sum(x.get('status') == 'scheduled' for x in rows if type(x) is dict), 'site scheduled count')
        items.extend(rows)
    items.extend(unmapped)
    require(len(items) <= 2000, 'Too many Live notices')
    for item in items:
        require(type(item) is dict and item.get('status') in STATUSES, 'Invalid notice status')
        require(type(item.get('service')) is str and item['service'] and type(item.get('service_layer')) is str and item['service_layer'], 'Notice service missing')
        require(item.get('source_url') == SOURCE, 'Notice source missing')
        windows = item.get('schedule_windows')
        require(type(windows) is list and len(windows) <= 5000, 'Invalid schedule array')
        for window in windows:
            require(type(window) is dict and aware(window.get('start')) < aware(window.get('end')), 'Nonpositive schedule interval')
    count(data.get('active_disruption_count'), len(items), 'all notice count')
    count(data.get('active_site_count'), len(sites), 'mapped site count')
    for status, key in [('active_now', 'active_now_disruption_count'), ('scheduled', 'scheduled_disruption_count'), ('unclear', 'unclear_disruption_count')]:
        count(data.get(key), sum(x['status'] == status for x in items), key)
    for status, key in [('active_now', 'active_now_site_count'), ('scheduled', 'scheduled_site_count')]:
        count(data.get(key), sum(any(x['status'] == status for x in site['disruptions']) for site in sites), key)
    return {'schema_version': 8, 'notice_count': len(items), 'site_count': len(sites), 'source_fetched_at': data['source_fetched_at'], 'sha256': hashlib.sha256(raw).hexdigest()}

def validate_historical(raw):
    require(type(raw) is bytes and 1 <= len(raw) <= 16 * 1024 * 1024, 'Historical size outside bound')
    text = raw.decode('utf-8')
    for key in sorted(FIVE):
        require(type(public_constant(text, key)) is dict, 'Invalid public historical export')
    return {'sha256': hashlib.sha256(raw).hexdigest()}

def build(source, destination):
    source, destination = source.resolve(), destination.absolute()
    require(source.is_dir() and not destination.exists(), 'Artifact directory must be new')
    require(destination.resolve() != source, 'Artifact must not replace source')
    for path in source.rglob('*'):
        if '.git' not in path.relative_to(source).parts:
            require(not path.is_symlink(), 'Symlink in public source')
    result = {'live': validate_live((source / LIVE).read_bytes()), 'historical': validate_historical((source / HISTORICAL).read_bytes())}
    destination.mkdir(parents=True)
    copied = []
    for path in sorted(source.rglob('*')):
        rel = path.relative_to(source)
        if any(part.startswith('.') for part in rel.parts) or rel.parts[0] in EXCLUDED_ROOTS or not path.is_file():
            continue
        if path.suffix.lower() not in STATIC_SUFFIXES and rel.as_posix() != 'CNAME':
            continue
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        require(target.read_bytes() == path.read_bytes(), 'Artifact copy changed bytes')
        copied.append(rel.as_posix())
    for required in ('index.html', LIVE, HISTORICAL, 'CNAME', 'data/consolidated.json', 'data/consolidated.js', 'vendor/chart.umd.js', 'vendor/sorc-charts-v1.js'):
        require(required in copied, 'Missing website asset: ' + required)
    require((destination / 'CNAME').read_text().strip() == 'sorc.ca', 'Custom domain changed')
    result['static_files'] = len(copied)
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), sort_keys=True))

if __name__ == '__main__':
    main()
