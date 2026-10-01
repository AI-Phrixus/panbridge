"""Read-only Oracle-side metadata verification. No download, deletion or sharing."""
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import re
import sqlite3
import time

JOB_MAP = {13: 16, 14: 18}
EXPECTED = {16: (935, 68297220656), 18: (405, 25976502866)}


def plan(connection, items):
    jobs = {r['id']: dict(r) for r in connection.execute(
        'select id,destination,target_account_id from jobs where id in (16,18)')}
    if set(jobs) != {16, 18}:
        raise ValueError('JOB_MISSING')
    rows = defaultdict(list)
    for row in connection.execute(
        'select id,job_id,relative_path,size,status,downloaded_bytes,uploaded_bytes,'
        'meta_json from files where job_id in (16,18)'):
        rows[(row['job_id'], row['relative_path'])].append(dict(row))
    for jid, (count, size) in EXPECTED.items():
        candidates = [r for key, group in rows.items() if key[0] == jid for r in group]
        if len(candidates) != count or sum(r['size'] for r in candidates) != size:
            raise ValueError('JOB_TOTAL_MISMATCH')
        if jobs[jid]['destination'] != 'google' or not jobs[jid]['target_account_id']:
            raise ValueError('TARGET_NOT_BOUND_GOOGLE')
    result, seen, seen_ids = [], set(), set()
    for item in items:
        original = item['original_job_id']
        jid = JOB_MAP.get(original)
        key = (jid, item['relative_path'])
        if jid is None or key in seen:
            raise ValueError('DUPLICATE_OR_UNKNOWN_SOURCE')
        seen.add(key)
        found = rows.get(key, [])
        entry = {**item, 'job_id': jid, 'verified': False}
        if len(found) != 1:
            entry['result'] = 'MISSING' if not found else 'AMBIGUOUS'
        else:
            row = found[0]
            entry['file_id'] = row['id']
            if row['size'] != item['bytes']:
                entry['result'] = 'SIZE_MISMATCH'
            elif row['status'] != 'done':
                entry['result'] = 'NOT_DONE'
            elif row['downloaded_bytes'] != item['bytes'] or row['uploaded_bytes'] != item['bytes']:
                entry['result'] = 'INCOMPLETE_BYTES'
            else:
                delivery = json.loads(row['meta_json'] or '{}').get('google_delivery') or {}
                remote_id = delivery.get('item_id')
                if not isinstance(remote_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', remote_id):
                    entry['result'] = 'DELIVERY_ID_MISSING'
                elif remote_id in seen_ids:
                    entry['result'] = 'DUPLICATE_DELIVERY_ID'
                    # Both original mappings must fail if they point at one object.
                    for earlier in result:
                        if earlier.get('google_file_id') == remote_id:
                            earlier['result'] = 'DUPLICATE_DELIVERY_ID'
                else:
                    entry.update(result='READY', google_file_id=remote_id)
                    seen_ids.add(remote_id)
        result.append(entry)
    return jobs, result


def compare(entry, data):
    if data.get('id') != entry['google_file_id']:
        return 'ID_MISMATCH'
    if data.get('trashed') is not False:
        return 'TRASHED_OR_UNKNOWN'
    if str(data.get('size')) != str(entry['bytes']):
        return 'GOOGLE_SIZE_MISMATCH'
    digest = data.get('sha256Checksum')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        return 'SHA256_MISSING'
    if digest.lower() != entry['sha256']:
        return 'SHA256_MISMATCH'
    return 'MATCH'


async def verify(items, limit):
    # All credential access occurs on Oracle; no token is returned or persisted here.
    import httpx
    from app.security import decrypt_json
    from app.sinks.google import GoogleDriveSink, API
    connection = sqlite3.connect('file:/home/ubuntu/panbridge/data/app.db?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    jobs, entries = plan(connection, items)
    bound = {j['target_account_id'] for j in jobs.values()}
    if len(bound) != 1:
        raise ValueError('JOB_ACCOUNT_MISMATCH')
    account = next(iter(bound))
    generation = None

    async def access(force=False):
        nonlocal generation
        row = connection.execute("select payload from credentials where provider='google'").fetchone()
        if row is None:
            raise ValueError('CREDENTIAL_UNAVAILABLE')
        credential = decrypt_json(row[0])
        if credential.get('account_id') != account:
            raise ValueError('CREDENTIAL_ACCOUNT_CHANGED')
        session = credential.get('session_id')
        if not session or (generation is not None and session != generation):
            raise ValueError('CREDENTIAL_SESSION_CHANGED')
        generation = session
        if float(credential.get('expires_at') or 0) <= time.time() + 30:
            # Never refresh or write credentials from this separate process.
            raise ValueError('WAIT_FOR_SERVICE_TOKEN_REFRESH')
        return credential['access_token']

    sink = GoogleDriveSink(access, 'metadata-verifier', account)
    started = datetime.now(timezone.utc).isoformat()
    checked = 0
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        async def identity():
            response = await sink._request(client, 'GET', API + '/about',
                                           params={'fields': 'user(permissionId)'})
            sink._check(response)
            if (response.json().get('user') or {}).get('permissionId') != account:
                raise ValueError('GOOGLE_ACCOUNT_MISMATCH')
        await identity()
        for entry in entries:
            if entry['result'] != 'READY' or checked >= limit:
                continue
            response = await sink._request(client, 'GET', API + '/files/' + entry['google_file_id'],
                params={'fields': 'id,size,sha256Checksum,trashed'})
            checked += 1
            if response.status_code == 404:
                entry['result'] = 'GOOGLE_FILE_MISSING'
                continue
            sink._check(response)
            data = response.json()
            entry.update(result=compare(entry, data), google_size=data.get('size'),
                         google_sha256=data.get('sha256Checksum'), trashed=data.get('trashed'),
                         observed_at_utc=datetime.now(timezone.utc).isoformat())
            entry['verified'] = entry['result'] == 'MATCH'
        await identity()
    # Recheck account binding and each delivery after API calls; do not trust a stale plan.
    latest_jobs, latest_entries = plan(connection, items)
    unchanged = latest_jobs == jobs and all(
        old.get('file_id') == new.get('file_id') and old.get('google_file_id') == new.get('google_file_id')
        and new['result'] == 'READY'
        for old, new in zip(entries, latest_entries) if old['verified'])
    connection.close()
    matches = sum(e['verified'] for e in entries)
    complete = len(entries) == 477 and matches == 477 and unchanged
    return {'schema': 1, 'started_at_utc': started, 'verified_at_utc': datetime.now(timezone.utc).isoformat(),
            'files': len(entries), 'bytes': sum(e['bytes'] for e in entries),
            'checked_this_run': checked, 'matched_this_run': matches,
            'account_matches': True, 'source_bindings_unchanged': unchanged,
            'all_sha256_match': complete, 'all_size_match': complete,
            'all_untrashed': complete, 'complete': complete,
            'ready_count': sum(e['result'] in ('READY', 'MATCH') for e in entries),
            'not_done_count': sum(e['result'] == 'NOT_DONE' for e in entries), 'observations': entries}


if __name__ == '__main__':
    try:
        if MAP_ONLY:
            connection = sqlite3.connect('file:/home/ubuntu/panbridge/data/app.db?mode=ro', uri=True)
            connection.row_factory = sqlite3.Row
            jobs, entries = plan(connection, ITEMS)
            connection.close()
            summary = dict(Counter(e['result'] for e in entries))
            print(json.dumps({'files': len(entries), 'bytes': sum(e['bytes'] for e in entries),
                              'mapping_counts': summary, 'ready_count': summary.get('READY', 0),
                              'account_bindings_agree': len({j['target_account_id'] for j in jobs.values()}) == 1,
                              'google_api_checked': False, 'complete': False}))
        else:
            print(json.dumps(asyncio.run(verify(ITEMS, LIMIT)), ensure_ascii=False))
    except Exception as exc:
        # Never print raw exception messages, request URLs, credentials, or tracebacks.
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[A-Z_]+', str(exc)) else type(exc).__name__
        print(json.dumps({'complete': False, 'error_code': code}))
        raise SystemExit(2)
