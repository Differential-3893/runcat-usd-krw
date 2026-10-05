# RunCat USD/KRW

A small, user-level USD/KRW metric for [RunCat Neo](https://github.com/runcat-dev/RunCatNeo)
on macOS, using a personal Twelve Data API key. No pip dependencies, privileged
helper, account scraping, historical-price database or extra background daemon.

Illustrative menu-bar text (synthetic values, not a live quote):

```text
₩1,344 ▲0.28%
```

The card shows:

```text
USD / KRW
1 USD: ₩1,343.87
Change: ▲0.28%
Quote Time: 10/5 21:15 KST
```

**Change compares the quote's `close` with Twelve Data's `previous_close` for an
explicit `interval=1day`.** It is not a comparison with the last poll, 24 hours
ago, Korean midnight, a Korean bank's reference rate, or a guaranteed executable
buy/sell price. `▲` means more KRW per USD; it is not a good/bad rating.
The quote clock comes from `last_quote_at`; the separate `timestamp` is the
daily bar opening and is never mislabeled as the latest quote. Missing quote time
is shown as `—`, with a separately labeled `Bar Start`.
See [the data contract](docs/DATA_CONTRACT.md) for precision, missing values,
market closure and timestamps.

## Requirements

- macOS, logged into a GUI session (no `sudo`).
- Python **3.10+**. The existing recorded Homebrew Python is retained on upgrades.
- RunCat Neo Custom Metrics.
- A Twelve Data API key entitled to `USD/KRW` on `/quote`.
- `gh` and `git` only for the optional GitHub publisher, not for the sampler.

Working access to the old `/exchange_rate` does **not** by itself prove quote
entitlement. Installation tests your own saved key before replacing the old job.
We cannot certify your plan from a source review. No fallback feed, trial sign-up,
paid plan, or second quote request is silently selected.

## Upgrade the existing local FX integration

From this downloaded directory:

```sh
sh apply_local.sh
```

This verifies the release files, runs offline tests, and upgrades the matching
`dev.runcat.fx-usdkrw` job. It keeps:

```text
~/.runcat/runcat-fx/update-fx.py
~/.runcat/runcat-fx/api-key
~/.runcat/usd-krw.json
~/Library/LaunchAgents/dev.runcat.fx-usdkrw.plist
```

The API key stays on your Mac. Do not upload it here or to GitHub. The installer
can tighten an existing key file to mode `600`, without changing its contents.
That permission tightening remains even if the later data probe fails.

A private temporary probe checks quote access and a usable daily change before
stopping the installed job. Then only the FX producer, its plist and its metric
are backed up. The key and logs are not copied into the backup. An installed
producer is executed to prove a new atomic write; the LaunchAgent is then
registered with `RunAtLoad` and **StartInterval 300**. Success prints:

```text
LOCAL CHECK PASSED
RUNTIME SETTINGS PRESERVED: ...
ALL LOCAL CHECKS PASSED
```

Detected installation failure restores prior target files and launch state.
Restoration errors print `RESTORE INCOMPLETE`, not success. These are not power-loss
or SIGKILL guarantees. Keep the printed `Backup:` path.

This is a local install plus manual live-quote check, not an automated observation
of the RunCat UI, login reboot, or a full five-minute timer cycle. A valid market
quote can be old while the market is closed; file-write freshness and quote age
are separate checks. No other RunCat integration is installed or updated. Setup/removal commands
serialize with an empty `.dev.runcat.fx-usdkrw.setup.lock` under LaunchAgents;
they may create that lock before validating the rest of the installation.

The first upgraded install ordinarily makes a probe request, an installed-script
request, and a `RunAtLoad` request (up to three requests, plus timer/manual overlap).
Normal background execution makes **one request per scheduled invocation**, no
immediate retries. At an ideal five-minute cadence this is 288 requests per 24
hours, excluding installation and manual checks; sleep can reduce it. Your data
plan and other applications share its allowance.

## New installation

Store your own key using a hidden interactive prompt:

```sh
python3 -B scripts/manage_install.py set-key
sh apply_local.sh
```

No API key goes in shell history, the plist, command arguments, test fixtures,
README or GitHub. For nondefault paths, supply the same explicit `--key-file`
to both the key setup and first install. Existing users with a valid saved key
should **not** run `set-key` again.

In RunCat Neo add `~/.runcat/usd-krw.json` as a Custom Metrics source. An existing
registration need not be re-added when its path stays unchanged. Menu-bar width
is controlled by RunCat; this JSON cannot shrink only the percentage's font or
set red/blue text. Verify that your menu bar has enough room for the short label.

## Saved settings and supported changes

The installer uses **explicit command option → recorded installation → fresh
default**. A different `python3` in today's shell does not replace a saved runtime
Python. Generic ambient `RUNCAT_*` or `TWELVEDATA_API_KEY` values are not new
installation preferences. The saved plist contains only owned, nonsecret paths.

```sh
# Examples only; each executable/file must exist on your Mac.
sh install.sh --python /opt/homebrew/bin/python3
sh install.sh --output "$HOME/.runcat/custom-fx.json"
sh install.sh --key-file "$HOME/.runcat/private/fx-api-key"
```

Fresh install also supports `--data-dir`. Moving an existing data directory in
place is refused. Old output/key files are not moved or deleted when a deliberate
new path is chosen. Unrecorded historical shell overrides cannot be recovered:
specify them once during migration. Symlinked data paths, unknown plist tuning,
unknown producer revisions, and paths colliding with the Codex/battery outputs
are rejected before installation. Stable runtime Python symlinks are allowed.

The reviewed old producer is recognized by its SHA-256, not by a username or
machine path. Repeating the same successful installation skips replacement and
backs up nothing new, but still rechecks the live quote.

## Manual commands (from this source directory)

These discover the **saved Python, key path and output path**, not today's shell
runtime preferences:

```sh
python3 -B scripts/manage_install.py verify
python3 -B scripts/manage_install.py refresh
python3 -B scripts/manage_install.py diagnose
python3 -B scripts/manage_install.py show
```

`verify` checks installed-source equality, saved configuration, registration,
and a fresh atomic write with daily change. `refresh` makes the same strict
one-shot quote check. `diagnose` performs one request and prints allowlisted
numeric/state fields (including comparison basis and age), without replacing
the metric. It may create the empty sampling-lock file. Before upgrade it uses
the new source to help diagnose `/quote` access. `show` only reads the saved
metric, so it is **not** evidence of a fresh market quote.

For development only, direct `python3 update-fx.py` uses its invoking environment:
`RUNCAT_HOME`, `RUNCAT_OUT_FILE`, `RUNCAT_FX_KEY_FILE`, and optionally a directly
supplied `TWELVEDATA_API_KEY`. This is not the recommended installed-runtime path.
The old official `RUNCAT_FX_API_BASE=.../exchange_rate` migrates to `/quote`; arbitrary
servers are rejected to avoid sending credentials to another host.

## Failure handling and privacy

A failed HTTP, decoding, core quote or filesystem operation returns nonzero;
the failed attempt does not replace the last good metric. It does not claim the
old value is current. Inspect both RunCat's last-updated footer and `Quote Time`.
An invalid/missing daily comparison can still update a valid current price, with
`Change: —` and an explicit status row. Strict install/verify checks reject this
partial result via `--require-change` instead of claiming the requested change feature works.

Network calls use verified HTTPS, a fixed official host, `Authorization: apikey …`
(rather than a key in the URL), denied redirects, a 64 KiB response limit and a
15-second network-I/O timeout. That I/O timeout is not an absolute process deadline;
installer child processes also have a 25-second limit. No retry loop is added.
Diagnostics and normal errors never echo raw server messages, URLs containing a
key, response headers, tokens or Python tracebacks.

A user-owned `.usd-krw.json.lock` serializes samples at the output path. It contains
no key or telemetry and is not removed while sampling. A busy invocation fails
without making another API request. The scheduler tries again at its next time.

Logs are normally empty and remain at the recorded stdout/stderr paths under
`~/.runcat/runcat-fx/`. They contain only application-owned errors. They are not
uploaded or rotated by this program.

## Rollback and uninstall

To restore this installer's exact saved files and launch state, use the printed
backup directory (replace the example with your actual path):

```sh
python3 -B scripts/manage_install.py rollback "$HOME/.runcat/backups/fx-usdkrw-<printed-backup>"
```

Rollback can restore an older snapshot; its original timestamp is retained. A
changed backup or different current installation paths is not blindly restored.

```sh
sh uninstall.sh
```

Uninstall does not require a still-working saved runtime Python. It removes
**only** the owned FX job and producer, after backup. It keeps
the API key, metric, logs, empty lock, backups and unrelated files. Remove the
Custom Metrics source in RunCat when no longer using it. No recursive deletion
of `~/.runcat` occurs.

## Publish this reviewed source with gh

The release publisher is for **first publication**, or resuming the identical
initial publication. It is not a general overwrite/synchronization tool.

```sh
gh auth refresh -h github.com -s workflow
python3 -B publish_github.py --push --public --wait-ci
```

The default destination is `Differential-3893/runcat-usd-krw`. It requires gh to be
signed in as that owner. Other maintainers can explicitly set `--repo owner/name`.
`--public` explicitly authorizes creating a public repository. Use `--private`
instead when appropriate; existing visibility is not changed automatically.
Without `--push` the command is a read-only preview.

The publisher stages only a hardcoded list of reviewed files in a new temporary
Git repository and uses gh as Git's credential helper for the single nonforced
push. It does not change global Git configuration or inspect/stage your local
runtime directory. A pre-existing nonempty repository with different content
is refused, not overwritten. The exact same release is skipped idempotently.
After push, it verifies remote source blobs and waits for the four exact-commit
CI jobs when `--wait-ci` is present. CI failure/timeout is nonzero but does not
undo an already published commit; rerunning the same command rechecks it. An
interrupted creation may leave an empty repository that the next run can resume.
No other repositories, branch rules, paid services or release objects are changed.

## Tests and maintenance

```sh
python3 -B publish_github.py --check-package
python3 -B -m unittest discover -s tests -v
```

Tests use synthetic data, temporary HOME directories and mocked network/launchctl.
Some tests use real local child processes, advisory locks and a temporary Git
repository; none calls Twelve Data or needs an API key. CI covers Python 3.10 and
3.13 on Linux/macOS with full-SHA-pinned actions, read-only permissions and no
persisted checkout credentials. Native sensor/API/UI behavior is not certified
by offline tests. See [maintenance](docs/MAINTENANCE.md).

## License and data

Code: [MIT](LICENSE). This is an unofficial integration, not endorsed by Twelve
Data or RunCat. Each user obtains their own data access and must follow the
provider's terms. Publishing code does not grant data redistribution rights.
Only synthetic numerical examples are distributed; no real quote archive or
credential is included. The code is a display tool, not financial advice.
