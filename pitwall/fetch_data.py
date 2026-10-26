"""Download a bounded historical session. No final-results endpoint is used."""
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .data import ROOT, ENDPOINTS, build_timeline, digest


def fetch(endpoint, params):
    url = 'https://api.openf1.org/v1/' + endpoint + '?' + urllib.parse.urlencode(params)
    context = ssl.create_default_context()
    # Use an available trust bundle on Python installations without a configured
    # OS certificate store. HTTPS verification is never disabled.
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'F1Pitwall-LearningProject/0.1'})
            with urllib.request.urlopen(request, context=context, timeout=25) as response:
                rows = json.load(response)
            if not isinstance(rows, list):
                raise ValueError(f'{endpoint} did not return an observation list')
            return rows
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError('Download retry budget exhausted')


def main():
    project = json.loads((ROOT / 'project.json').read_text())
    session = project['race']['session_key']
    driver = project['driver']['driver_number']
    raw_dir = ROOT / 'data' / 'raw'
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = {}
    sources = {}
    for endpoint in ('sessions', 'drivers', *ENDPOINTS):
        params = {'session_key': session}
        if endpoint not in ('sessions', 'weather', 'race_control'):
            params['driver_number'] = driver
        path = raw_dir / f'{endpoint}.json'
        if path.exists():
            rows = json.loads(path.read_text())
            print(f'{endpoint}: {len(rows)} cached records', flush=True)
        else:
            rows = fetch(endpoint, params)
            path.write_text(json.dumps(rows, indent=2, allow_nan=False) + '\n')
            print(f'{endpoint}: {len(rows)} downloaded records', flush=True)
            time.sleep(.4)
        raw[endpoint] = rows
        sources[endpoint] = {'count': len(rows), 'sha256': digest(rows), 'query': params}
    timeline = build_timeline(raw, project)
    manifest = {'imported_at': datetime.now(timezone.utc).isoformat(),
                'provider': 'https://openf1.org/docs/', 'sources': sources,
                'timeline_sha256': digest(timeline), 'events': len(timeline['events']),
                'dropped_records': timeline['dropped_records']}
    # Keep the previous complete import usable if downloading or validation fails.
    target = ROOT / 'data' / 'timeline.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(timeline, indent=2, allow_nan=False) + '\n')
    temporary.replace(target)
    (ROOT / 'data' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Imported {len(timeline["events"])} timestamped observations; omitted {len(timeline["dropped_records"])} incomplete records', flush=True)


if __name__ == '__main__':
    main()
