"""Verify the fixed 477-file backup against private Drive metadata. Never deletes."""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
BACKUP = ROOT / 'Backups/PanBridge-20260930-tasks13-14'


def load_items():
    manifest = json.loads((BACKUP / 'source-manifest.json').read_text())
    proof = json.loads((BACKUP / 'verification.json').read_text())
    items = manifest['files']
    if not (proof['all_sha256_match'] is True and proof['source_metadata_unchanged'] is True
            and proof['files'] == len(items) == 477
            and proof['bytes'] == sum(x['bytes'] for x in items) == 43642267164):
        raise ValueError('Local backup proof does not match the fixed manifest')
    paths = set()
    clean = []
    for item in items:
        path = PurePosixPath(item['path'])
        original = item['file']['job_id']
        if (path.is_absolute() or '..' in path.parts or len(path.parts) != 2
                or path.parts[0] != str(original) or original not in (13, 14)
                or item['path'] in paths or not isinstance(item['bytes'], int) or item['bytes'] < 0
                or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Unsafe or duplicate manifest item')
        paths.add(item['path'])
        clean.append({'backup_path': item['path'], 'original_job_id': original,
                      'relative_path': item['file']['relative_path'],
                      'bytes': item['bytes'], 'sha256': item['sha256']})
    return clean


def save_report(report):
    # JSON evidence, not credentials. Keep it private and replace atomically.
    destination = BACKUP / 'google-verification.json'
    fd, name = tempfile.mkstemp(prefix='.google-verification-', dir=BACKUP)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=30, help='Maximum metadata GETs; use 477 for final fresh verification')
    parser.add_argument('--plan-only', action='store_true', help='Validate local manifest without network')
    parser.add_argument('--map-only', action='store_true', help='Read-only Oracle file mapping; no Google API or proof replacement')
    args = parser.parse_args()
    if not 1 <= args.limit <= 477:
        parser.error('limit must be between 1 and 477')
    items = load_items()
    if args.plan_only:
        print(json.dumps({'files': len(items), 'bytes': sum(e['bytes'] for e in items), 'deletes_files': False}))
        return
    payload = 'ITEMS=' + repr(items) + '\nLIMIT=' + repr(args.limit) + '\nMAP_ONLY=' + repr(args.map_only) + '\n'
    payload += (Path(__file__).resolve().parent / 'verify_panbridge_google_remote.py').read_text()
    try:
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=15', 'ubuntu@152.70.86.29',
            'cd /home/ubuntu/panbridge && .venv/bin/python -'], input=payload, text=True,
            capture_output=True, timeout=1200)
    except subprocess.TimeoutExpired:
        save_report({'complete': False, 'all_sha256_match': False, 'error_code': 'SSH_TIMEOUT'})
        raise RuntimeError('Verification timed out; backup preserved') from None
    try:
        report = json.loads(result.stdout)
    except ValueError:
        save_report({'complete': False, 'all_sha256_match': False, 'error_code': 'INVALID_RESPONSE'})
        raise RuntimeError('Verification connection failed; backup preserved') from None
    if result.returncode or 'error_code' in report:
        save_report({'complete': False, 'all_sha256_match': False,
                     'error_code': report.get('error_code', 'SSH_FAILED')})
        print(json.dumps({'complete': False, 'error_code': report.get('error_code', 'SSH_FAILED'),
                          'backup_preserved': True}))
        raise SystemExit(1)
    if report.get('files') != 477 or report.get('bytes') != 43642267164:
        save_report({'complete': False, 'all_sha256_match': False, 'error_code': 'UNEXPECTED_TOTAL'})
        raise RuntimeError('Unexpected verification response; backup preserved')
    if args.map_only:
        print(json.dumps(report))
        return
    save_report(report)
    print(json.dumps({k: v for k, v in report.items() if k != 'observations'}))


if __name__ == '__main__':
    main()
