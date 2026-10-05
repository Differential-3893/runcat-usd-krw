# Maintenance

Read [README](../README.md) and [the data contract](DATA_CONTRACT.md) before
changing behavior. Preserve the daily comparison definition, three normal card
rows, paths, personal-key ownership, 300-second job, and single-request sampling.
No claim of universal defect freedom follows from a finite test suite.

## Regression gates

```sh
python3 -B publish_github.py --check-package
python3 -B -m unittest discover -s tests -v
sh -n install.sh
sh -n uninstall.sh
sh -n apply_local.sh
```

Parsing/formatting tests cover arrows, rounding, missing data, malformed numbers,
unknown symbols, daily period, distinct `timestamp`/`last_quote_at` clocks, provider inconsistencies,
nonsecret diagnostics and limited/redirect-denying HTTP requests. Installation
uses mocked launchctl and synthetic quotes in temporary user directories;
rollback, settings preservation, key non-copying, idempotency and path collisions
are checked. Actual shell bootstrap and interprocess lock tests are separate.
Publisher tests do not create remote repositories, but use real temporary Git
commits to verify source-only staging.

CI additionally runs on Python 3.10/3.13 and Ubuntu/macOS. Development here used
Python 3.13.5 on Linux; CI configuration is not a report that those remote jobs
already ran. Live API entitlement, timer/UI observation and macOS behavior must
be verified on the installation machine. Network and market timestamps are
separate; the installer cannot turn an old closed-market quote into a current one.

## Source integrity manifest

The explicit PUBLIC_FILES tuple in `publish_github.py` is the release allowlist.
The release manifest protects this delivery against accidental edits and an
unexpected upload file list. It is not a digital signature: an attacker replacing
both code and manifest is outside this mechanism. After intentionally reviewed
source changes, regenerate the manifest and run all tests:

```sh
python3 - <<'PY'
import hashlib, json, pathlib, runpy
root = pathlib.Path.cwd()
paths = runpy.run_path(str(root / 'publish_github.py'))['PUBLIC_FILES']
manifest = {'version': '1.0.0', 'sha256': {
    name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths
}}
(root / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY
```

For a subsequent different release, use a reviewed Git/PR workflow; the initial
publisher intentionally refuses to overwrite a different nonempty repository.
Tests and documentation must change with the producer, not as an afterthought.

## CI pins and operations

Official action tags were resolved via the GitHub API on 2026-10-05:

- checkout v7.0.1: `3d3c42e5aac5ba805825da76410c181273ba90b1`
- setup-python v6.0.0: `e797f83bcb11b83ae66e0230d6156d7c80228e7c`

Full SHA pinning, read-only token permissions, `persist-credentials: false`,
10-minute job timeouts and offline tests are intentional. Review official action
updates before changing pins. No automatic dependency PR bot, account credentials,
protected-branch settings or scheduled market-data CI is installed.

The installer accepts this package or the reviewed legacy producer hash. It does
not overwrite unreviewed local changes. It only supports known FX plist fields;
unknown manual tuning is not silently removed. Preserve error messages with
useful fixed causes but do not reflect arbitrary external strings into logs.
The API key never belongs in backups or the release allowlist.

No remotely published release or local installation should be claimed merely
because the build tests passed: record `ALL LOCAL CHECKS PASSED`, the exact pushed
commit, and that commit's CI separately. New mandatory fixes need a reproducible
failure/security exposure in the current code; optional features are not evidence
that the running display is broken.
