# Private backup verification (read-only)

These tools are for the already-authorized original jobs 13/14 backup, mapped to
Google jobs 16/18 on the existing Oracle deployment. They are not general-purpose
download, retry, deletion or storage-management tools. Run from the repository.

```sh
python3 ops/verify_panbridge_google_backup.py --plan-only
python3 ops/verify_panbridge_google_backup.py --map-only
python3 ops/verify_panbridge_google_backup.py --limit 477
```

- `--plan-only`: local validation of the exact 477-file / 43,642,267,164-byte
  manifest and completed Mac backup proof, without network.
- `--map-only`: read-only SQLite lookup on Oracle, verifying unique relative-path
  mappings, expected job totals, Google-bound destinations and complete byte
  counts. No Google API calls and no replacement of prior Google evidence.
- Verification mode: fetch official Google metadata for mapped `done` files,
  including size, SHA-256 and trashed state. Match the Google API account to the
  jobs before and after the run. Use a fresh 477-file run for final acceptance.

Credentials are decrypted only inside the Oracle process through the existing
PanBridge security module. Only existing, unexpired service-managed access tokens
are used; this tool does not refresh tokens, save credentials, export secrets or
write the application database. The existing GoogleDriveSink performs GET requests
only; no public sharing, file update or deletion is performed. Unexpected failures
produce only sanitized error categories, never raw request/credential exceptions.

Evidence is saved privately to the workspace backup's `google-verification.json`.
Partial observations are explicitly `complete=false`; they are not a cleanup
permit. Failed verification clears the acceptance flag rather than retaining an
old successful result as if it were current. The final cleanup must still rehash
all local files, reject symlinks/unsafe paths, and recheck local metadata immediately
before deleting only the delegated whitelist. These tools never delete Mac files.

For normal monitoring, run mapping only. Until all 477 mappings are ready, keep the
Mac backup. Once all are ready, run a fresh complete Google verification; do not
repeatedly query hundreds of unchanged Google objects every 30 minutes.

## Evidence, 2026-10-01

At 08:14 UTC (17:14 Japan time), 399 mapped files were checked against official
Google metadata. All 399 matched size/SHA-256, were untrashed, and had consistent
account/delivery binding. The remaining 78 were not done; full backup acceptance
and Mac cleanup remain pending. No files or jobs were changed.

Nine offline adversarial tests cover duplicate/ambiguous mappings, changed totals,
incomplete byte counts, duplicate/unsafe delivery IDs, missing hashes, trash state,
size/hash mismatch and correct mappings. Run:

```sh
cd ops
python3 -m unittest -v test_panbridge_backup_verifier.py
```

Offline tests are not proof of all-file delivery or real-player compatibility.
