#!/usr/bin/env python3
"""User-level FX installation, saved-runtime commands, and bounded rollback."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import fcntl
from datetime import datetime, timezone
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
LABEL = "dev.runcat.fx-usdkrw"
LAUNCHCTL = "/bin/launchctl"
LEGACY_SOURCE_SHA256 = "deb481b9afe5fc6093f79558c913b1f52240e41d1f6e3d6545d888ab3b78c4a3"
ENV_KEYS = {"RUNCAT_HOME", "RUNCAT_OUT_FILE", "RUNCAT_FX_KEY_FILE", "PYTHONDONTWRITEBYTECODE"}
PLIST_KEYS = {"Label", "ProgramArguments", "RunAtLoad", "StartInterval", "StandardOutPath", "StandardErrorPath", "EnvironmentVariables"}


class SetupError(Exception):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def regular(path, missing_ok=True):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise SetupError("A required local file is missing.") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SetupError("Refusing a symlink, nonregular file, or file owned by another user.")


def local_path(value):
    if not isinstance(value, str) or not value.strip():
        raise SetupError("A saved path is empty or malformed.")
    result = Path(value).expanduser()
    if not result.is_absolute():
        raise SetupError("Installation and data paths must be absolute.")
    home = Path.home().resolve()
    if not result.resolve().is_relative_to(home) or result.resolve() == home:
        raise SetupError("FX files must stay inside the current user's home directory.")
    # Reject link-based routing for managed data. Runtime Python aliases are separate.
    current = result
    while current != Path.home() and current != current.parent:
        if current.is_symlink():
            raise SetupError("Managed FX paths must not contain symlinks.")
        current = current.parent
    return result


@contextmanager
def operation_lock():
    """Serialize setup/removal, without a second resident process."""
    path = local_path(str(Path.home() / "Library/LaunchAgents" / ("." + LABEL + ".setup.lock")))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SetupError("Invalid FX setup lock.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("Another FX install/removal is running; no setup changes were made.") from None
        yield
    finally:
        os.close(fd)


def read_plist(path):
    regular(path)
    if not path.exists():
        return None
    if path.stat().st_size > 65536:
        raise SetupError("The existing FX LaunchAgent is too large.")
    try:
        data = plistlib.loads(path.read_bytes())
    except Exception:
        raise SetupError("The existing FX LaunchAgent cannot be parsed; it was not overwritten.") from None
    if not isinstance(data, dict) or data.get("Label") != LABEL or set(data) - PLIST_KEYS:
        raise SetupError("The existing FX LaunchAgent has an unexpected configuration.")
    args = data.get("ProgramArguments")
    if (not isinstance(args, list) or len(args) != 2
            or not all(isinstance(x, str) and x for x in args)
            or Path(args[1]).name != "update-fx.py"
            or not Path(args[0]).is_absolute()):
        raise SetupError("The existing FX LaunchAgent command is unsupported.")
    if type(data.get("StartInterval")) is not int or data["StartInterval"] != 300 or data.get("RunAtLoad") is not True:
        raise SetupError("The existing FX job does not use the expected 300-second schedule.")
    env = data.get("EnvironmentVariables", {})
    if not isinstance(env, dict) or set(env) - (ENV_KEYS | {"RUNCAT_FX_API_BASE"}):
        raise SetupError("Unsupported saved environment; no secrets or unknown settings were copied.")
    if not all(isinstance(v, str) and v for v in env.values()):
        raise SetupError("Malformed saved FX environment.")
    if env.get("RUNCAT_FX_API_BASE", "https://api.twelvedata.com/quote") not in (
        "https://api.twelvedata.com/exchange_rate", "https://api.twelvedata.com/quote"
    ):
        raise SetupError("The saved data service is not the official Twelve Data endpoint.")
    return data


class Config:
    def __init__(self, *, python=None, data_dir=None, output=None, key_file=None, require_python=True):
        self.plist = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        self.old = read_plist(self.plist)
        env = self.old.get("EnvironmentVariables", {}) if self.old else {}
        old_dir = Path(self.old["ProgramArguments"][1]).parent if self.old else None
        root_default = old_dir.parent if old_dir else Path.home() / ".runcat"
        self.home = local_path(str(data_dir) if data_dir is not None else env.get("RUNCAT_HOME", str(root_default)))
        self.install = local_path(str(old_dir or self.home / "runcat-fx"))
        if self.old and data_dir is not None and self.home != local_path(env.get("RUNCAT_HOME", str(root_default))):
            raise SetupError("Changing the data directory of an existing job is not an in-place update.")
        self.script = self.install / "update-fx.py"
        self.key = local_path(str(key_file) if key_file is not None else env.get("RUNCAT_FX_KEY_FILE", str(self.install / "api-key")))
        self.out = local_path(str(output) if output is not None else env.get("RUNCAT_OUT_FILE", str(self.home / "usd-krw.json")))
        self.stdout = local_path(self.old.get("StandardOutPath", str(self.install / "stdout.log")) if self.old else str(self.install / "stdout.log"))
        self.stderr = local_path(self.old.get("StandardErrorPath", str(self.install / "stderr.log")) if self.old else str(self.install / "stderr.log"))
        selected = python if python is not None else self.old["ProgramArguments"][0] if self.old else sys.executable
        if not isinstance(selected, str) or not selected.strip():
            raise SetupError("The selected runtime Python is empty.")
        executable = shutil.which(selected)
        if not executable and (require_python or not Path(selected).is_absolute()):
            raise SetupError("The saved/selected Python is missing. Explicitly use install.sh --python /absolute/path.")
        self.python = os.path.abspath(executable or selected)  # Keep stable Homebrew aliases.
        self.target = f"gui/{os.getuid()}/{LABEL}"
        self.domain = f"gui/{os.getuid()}"
        paths = [self.script, self.plist, self.out, self.key, self.stdout, self.stderr,
                 self.out.with_name("." + self.out.name + ".lock")]
        resolved = [p.resolve() for p in paths]
        if len(set(resolved)) != len(paths) or any(a in b.parents for a in resolved for b in resolved if a != b):
            raise SetupError("FX script, key, data, log and lock paths must be distinct and nonoverlapping.")
        protected = {Path.home() / ".codex/runcat-usage.json", Path.home() / ".codex/hooks.json",
                     Path.home() / ".runcat/battery-power.json", Path.home() / ".runcat/battery-power-history.json"}
        for label in ("dev.runcat.codex-usage", "dev.runcat.battery-power"):
            other = Path.home() / "Library/LaunchAgents" / (label + ".plist")
            if other.is_file():
                try:
                    other_env = plistlib.loads(other.read_bytes()).get("EnvironmentVariables", {})
                    for field in ("RUNCAT_OUT_FILE", "RUNCAT_BATTERY_HISTORY_FILE"):
                        if isinstance(other_env.get(field), str):
                            protected.add(Path(other_env[field]).expanduser())
                except Exception:
                    raise SetupError("Cannot validate another RunCat job's output paths; no FX files were changed.") from None
        if set(resolved) & {p.resolve() for p in protected}:
            raise SetupError("An FX target collides with a Codex or battery integration file.")
        for p in paths:
            regular(p)

    def variables(self):
        return {"RUNCAT_HOME": str(self.home), "RUNCAT_OUT_FILE": str(self.out),
                "RUNCAT_FX_KEY_FILE": str(self.key), "PYTHONDONTWRITEBYTECODE": "1"}

    def environment(self):
        # No ambient FX override, API key, PYTHONPATH or user Python customization.
        env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONNOUSERSITE": "1"}
        for key in ("LANG", "LC_ALL", "LC_CTYPE"):
            if key in os.environ:
                env[key] = os.environ[key]
        env.update(self.variables())
        return env

    def payload(self):
        return {"Label": LABEL, "ProgramArguments": [self.python, str(self.script)],
                "RunAtLoad": True, "StartInterval": 300, "EnvironmentVariables": self.variables(),
                "StandardOutPath": str(self.stdout), "StandardErrorPath": str(self.stderr)}

    def targets(self):
        return [self.script, self.plist, self.out]


def native():
    if sys.platform != "darwin" or os.getuid() == 0:
        raise SetupError("Run installation/verification as your macOS login user, without sudo.")
    if sys.version_info < (3, 10):
        raise SetupError("Python 3.10 or newer is required.")


def launch(*args, check=True):
    result = subprocess.run([LAUNCHCTL, *args], capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise SetupError("launchctl " + args[0] + " failed.")
    return result


def loaded(cfg):
    return launch("print", cfg.target, check=False).returncode == 0


def disabled(cfg):
    result = launch("print-disabled", cfg.domain)
    return bool(re.search(r'"' + re.escape(LABEL) + r'"\s*=>\s*true', result.stdout))


def stop(cfg):
    if loaded(cfg):
        launch("bootout", cfg.target)
        if loaded(cfg):
            raise SetupError("The existing FX job did not stop.")


def start(cfg):
    launch("enable", cfg.target)
    launch("bootstrap", cfg.domain, str(cfg.plist))
    if not loaded(cfg):
        raise SetupError("The new FX job was not registered.")


def atomic_bytes(path, raw, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    regular(path)
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def check_python(cfg):
    result = subprocess.run([cfg.python, "-I", "-c", "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"],
                            env=cfg.environment(), capture_output=True, timeout=10)
    if result.returncode:
        raise SetupError("The selected runtime needs Python 3.10 or newer.")


def secure_key(cfg):
    regular(cfg.key, missing_ok=False)
    # Tightening existing permissions is intentional even if the later API probe fails.
    fd = os.open(cfg.key, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SetupError("The API key file must be a user-owned regular file.")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
            print("API key file permissions set to 600; its contents were not changed.")
    finally:
        os.close(fd)


def run_producer(cfg, script, *flags, env=None):
    result = subprocess.run([cfg.python, str(script), *flags], env=cfg.environment() if env is None else env,
                            text=True, capture_output=True, timeout=25)
    if result.returncode:
        # Report only a fixed category, never interpolate output from a child.
        stderr = result.stderr
        categories = (
            (("Daily change is unavailable/inconsistent",), "Daily change was missing or inconsistent in the quote response."),
            (("authorization", "does not have access"), "Quote access was denied. Check the saved key and its USD/KRW quote entitlement."),
            (("quota/rate limit",), "The Twelve Data API quota/rate limit was reached. Wait before retrying."),
            (("API key permissions",), "The key file needs mode 600."),
            (("Cannot read the API key", "key is empty or malformed"), "The saved API key could not be read or validated."),
            (("Another FX sample is running",), "Another FX sample is running; wait briefly before a manual retry."),
            (("data request failed",), "The Twelve Data request failed; check network access."),
        )
        reason = "The quote or local write could not be verified."
        for needles, text in categories:
            if any(word in stderr for word in needles):
                reason = text
                break
        raise SetupError("FX quote check failed. " + reason + " No raw API error was printed.")
    return result


def probe(cfg):
    # A private temporary output/lock path, not the live source, before stopping any job.
    with tempfile.TemporaryDirectory(prefix="runcat-fx-probe-") as tmp:
        env = cfg.environment()
        env["RUNCAT_OUT_FILE"] = str(Path(tmp) / "probe.json")
        result = run_producer(cfg, ROOT / "update-fx.py", "--diagnose", "--require-change", env=env)
        data = json.loads(result.stdout)
        if (data.get("symbol") != "USD/KRW" or data.get("interval") != "1day"
                or data.get("changeStatus") != "ok" or data.get("percentChange") is None):
            raise SetupError("The API probe did not supply the agreed daily-close change.")


def snapshot(path, since=None):
    regular(path, missing_ok=False)
    if path.stat().st_size > 65536:
        raise SetupError("The metric snapshot is too large.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("title") != "USD / KRW":
        raise SetupError("Unexpected FX snapshot.")
    rows = data.get("metrics")
    if (not isinstance(rows, list) or len(rows) < 3 or not all(isinstance(x, dict) for x in rows)
            or [x.get("title") for x in rows[:3]] != ["1 USD", "Change", "Quote Time"]):
        raise SetupError("FX snapshot rows are invalid.")
    if not isinstance(data.get("metricsBarValue"), str) or not re.fullmatch(r"₩[0-9,]+ (?:[▲▼][0-9]+\.[0-9]{2}%|0\.00%|—)", data["metricsBarValue"]):
        raise SetupError("The FX menu value is invalid.")
    if since is not None:
        if rows[1].get("formattedValue") == "—":
            raise SetupError("The live check did not obtain a usable daily change.")
        stamp = datetime.fromisoformat(data["lastUpdatedDate"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or not since - 1 <= stamp.timestamp() <= time.time() + 1:
            raise SetupError("No freshly written metric was verified.")
    return data


def live_sample(cfg):
    regular(cfg.out)
    before = os.open(cfg.out, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)) if cfg.out.exists() else None
    try:
        since = time.time()
        run_producer(cfg, cfg.script, "--require-change")
        regular(cfg.out, missing_ok=False)
        if before is not None:
            old, new = os.fstat(before), cfg.out.stat()
            if (old.st_dev, old.st_ino) == (new.st_dev, new.st_ino):
                raise SetupError("The live check did not observe an atomic metric replacement.")
        return snapshot(cfg.out, since)
    finally:
        if before is not None:
            os.close(before)


def verify(cfg, *, sample=True):
    if cfg.script.read_bytes() != (ROOT / "update-fx.py").read_bytes():
        raise SetupError("Installed FX source does not match this package.")
    if read_plist(cfg.plist) != cfg.payload() or not loaded(cfg):
        raise SetupError("The saved FX configuration or LaunchAgent registration differs.")
    data = live_sample(cfg) if sample else snapshot(cfg.out)
    print("LOCAL CHECK PASSED")
    print("Output: " + str(cfg.out))
    print("Menu: " + data["metricsBarValue"])
    for row in data["metrics"]:
        print(row["title"] + ": " + row["formattedValue"])
    print("Basis: Twelve Data previous daily-bar close; interval=1day.")
    print("Cadence: 300 seconds. This does not observe the RunCat UI or a full timer cycle.")
    return data


def backup(cfg, was_loaded, was_disabled):
    parent = cfg.home / "backups"
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="fx-usdkrw-" + time.strftime("%Y%m%d-%H%M%S") + "-", dir=parent))
    records = []
    for i, path in enumerate(cfg.targets()):
        regular(path)
        record = {"path": str(path), "file": None, "mode": None, "sha256": None}
        if path.exists():
            raw = path.read_bytes()
            record.update(file=str(i) + ".bak", mode=stat.S_IMODE(path.stat().st_mode), sha256=digest(raw))
            atomic_bytes(directory / record["file"], raw, 0o600)
        records.append(record)
    manifest = {"version": 1, "label": LABEL, "loaded": was_loaded, "disabled": was_disabled,
                "targets": records}
    atomic_bytes(directory / "RESTORE.json", (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
    return directory, manifest


def restore(cfg, directory, record):
    errors = []
    try:
        stop(cfg)
    except Exception:
        errors.append("could not stop FX job")
        return errors  # Never replace a running job's files after a failed stop.
    for item in record["targets"]:
        try:
            path = Path(item["path"])
            regular(path)
            if item["file"] is None:
                path.unlink(missing_ok=True)
            else:
                raw = (directory / item["file"]).read_bytes()
                if digest(raw) != item["sha256"]:
                    raise SetupError("Backup hash mismatch.")
                atomic_bytes(path, raw, item["mode"])
        except Exception:
            errors.append("file restoration failed")
    if not errors:
        try:
            launch("disable" if record["disabled"] else "enable", cfg.target)
            if record["loaded"]:
                launch("bootstrap", cfg.domain, str(cfg.plist))
                if not loaded(cfg):
                    raise SetupError("Old job did not reload.")
        except Exception:
            errors.append("launch state restoration failed")
    return errors


def install(cfg):
    check_python(cfg)
    if cfg.script.exists() and digest(cfg.script.read_bytes()) not in (LEGACY_SOURCE_SHA256, digest((ROOT / "update-fx.py").read_bytes())):
        raise SetupError("The installed FX script is an unreviewed version. It was not overwritten.")
    secure_key(cfg)
    if cfg.old == cfg.payload() and cfg.script.exists() and cfg.script.read_bytes() == (ROOT / "update-fx.py").read_bytes() and loaded(cfg):
        print("ALREADY INSTALLED: rechecking the live quote, without rewriting the installation.")
        verify(cfg)
        print("ALL LOCAL CHECKS PASSED")
        return
    probe(cfg)  # A failed probe does not stop or replace a working installation.
    was_loaded, was_disabled = loaded(cfg), disabled(cfg)
    directory = record = None
    try:
        stop(cfg)
        directory, record = backup(cfg, was_loaded, was_disabled)
        print("Backup: " + str(directory))
        atomic_bytes(cfg.script, (ROOT / "update-fx.py").read_bytes(), 0o755)
        atomic_bytes(cfg.plist, plistlib.dumps(cfg.payload(), sort_keys=False), 0o600)
        cfg.out.parent.mkdir(parents=True, exist_ok=True)
        cfg.stdout.parent.mkdir(parents=True, exist_ok=True)
        cfg.stderr.parent.mkdir(parents=True, exist_ok=True)
        live_sample(cfg)  # Real installed producer, before launchd's RunAtLoad query.
        start(cfg)
        verify(cfg, sample=False)
    except BaseException:
        if record is not None:
            failures = restore(cfg, directory, record)
        else:
            failures = []
            if was_loaded and not loaded(cfg):
                try:
                    launch("bootstrap", cfg.domain, str(cfg.plist))
                except Exception:
                    failures.append("previous job reload failed")
        if failures:
            raise SetupError("INSTALL FAILED; RESTORE INCOMPLETE. Keep the printed backup and terminal output.") from None
        raise SetupError("INSTALL FAILED; previous target files and launch state restored. Keep the backup.") from None
    print("RUNTIME SETTINGS PRESERVED: saved Python and output/key paths were reused unless explicitly overridden.")
    print("ALL LOCAL CHECKS PASSED")
    print("GitHub, the API key contents, and other RunCat integrations were not modified.")


def uninstall(cfg):
    if not cfg.old:
        print("No FX LaunchAgent is installed; nothing removed.")
        return
    was_loaded, was_disabled = loaded(cfg), disabled(cfg)
    stop(cfg)
    directory = record = None
    try:
        directory, record = backup(cfg, was_loaded, was_disabled)
        print("Backup: " + str(directory))
        cfg.script.unlink(missing_ok=True)
        cfg.plist.unlink(missing_ok=True)
    except Exception:
        if record is not None:
            failures = restore(cfg, directory, record)
        else:
            failures = []
            if was_loaded:
                try:
                    launch("bootstrap", cfg.domain, str(cfg.plist))
                except Exception:
                    failures.append("reload")
        raise SetupError("Uninstall failed; " + ("RESTORE INCOMPLETE." if failures else "previous installation restored.")) from None
    print("UNINSTALLED: only the FX job and producer. API key, metric, logs, lock and backups were kept.")


def rollback(directory):
    directory = local_path(str(directory))
    regular(directory / "RESTORE.json", missing_ok=False)
    if not directory.name.startswith("fx-usdkrw-") or directory.parent.name != "backups":
        raise SetupError("Select an FX backup directory created by this installer.")
    record = json.loads((directory / "RESTORE.json").read_text())
    if record.get("version") != 1 or record.get("label") != LABEL or len(record.get("targets", [])) != 3:
        raise SetupError("Unexpected backup manifest.")
    paths = [local_path(x["path"]) for x in record["targets"]]
    if paths[0].name != "update-fx.py" or paths[1] != Path.home() / "Library/LaunchAgents" / (LABEL + ".plist"):
        raise SetupError("Unexpected backup targets.")
    if len({p.resolve() for p in paths}) != 3:
        raise SetupError("Backup targets collide.")
    for i, item in enumerate(record["targets"]):
        if item["file"] is not None:
            if item["file"] != f"{i}.bak" or type(item["mode"]) is not int or not 0 <= item["mode"] <= 0o777:
                raise SetupError("Unexpected backup entry.")
            regular(directory / item["file"], missing_ok=False)
            if digest((directory / item["file"]).read_bytes()) != item["sha256"]:
                raise SetupError("Backup contents changed; restoration refused.")
    # Current Config validates collisions, paths and unrelated integrations too.
    cfg = Config(require_python=False)
    if cfg.targets() != paths:
        raise SetupError("The current FX paths differ from this backup; automatic rollback refused.")
    failures = restore(cfg, directory, record)
    if failures:
        raise SetupError("RESTORE INCOMPLETE. Keep the backup.")
    print("ROLLBACK COMPLETE. Saved files restored; the quote snapshot may be older until the next refresh.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "verify", "refresh", "diagnose", "show", "uninstall", "set-key", "rollback"))
    parser.add_argument("backup", nargs="?")
    parser.add_argument("--python")
    parser.add_argument("--data-dir")
    parser.add_argument("--output")
    parser.add_argument("--key-file")
    args = parser.parse_args(argv)
    try:
        native()
        with operation_lock() if args.action in ("install", "uninstall", "rollback", "set-key") else nullcontext():
            if args.action == "rollback":
                if not args.backup:
                    raise SetupError("Pass the printed FX backup directory.")
                rollback(args.backup)
                return 0
            if args.backup:
                raise SetupError("A backup argument is only valid for rollback.")
            overrides = {"python": args.python, "data_dir": args.data_dir, "output": args.output, "key_file": args.key_file}
            if args.action not in ("install", "set-key") and any(x is not None for x in overrides.values()):
                raise SetupError("Saved-runtime commands do not accept new runtime preferences.")
            cfg = Config(**overrides, require_python=args.action not in ("uninstall", "show", "set-key"))
            if args.action == "install":
                install(cfg)
            elif args.action == "uninstall":
                uninstall(cfg)
            elif args.action == "verify":
                verify(cfg)
            elif args.action == "refresh":
                for row in live_sample(cfg)["metrics"]:
                    print(row["title"] + ": " + row["formattedValue"])
            elif args.action == "show":
                print(json.dumps(snapshot(cfg.out), ensure_ascii=False, indent=2))
            elif args.action == "diagnose":
                script = cfg.script if cfg.script.exists() and digest(cfg.script.read_bytes()) == digest((ROOT / "update-fx.py").read_bytes()) else ROOT / "update-fx.py"
                result = run_producer(cfg, script, "--diagnose")
                print(result.stdout, end="")
            elif args.action == "set-key":
                if not sys.stdin.isatty():
                    raise SetupError("Set the key interactively, not as a command-line argument or pipeline.")
                spec = importlib.util.spec_from_file_location("fx_key_validator", ROOT / "update-fx.py")
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                key = module.validate_key(getpass.getpass("Twelve Data API key (input hidden): "))
                atomic_bytes(cfg.key, (key + "\n").encode(), 0o600)
                print("API key saved privately. No network request was made; the key was not printed.")
            return 0
    except Exception as exc:
        msg = str(exc) if isinstance(exc, SetupError) else "A local setup operation failed; no raw response/credential was printed."
        print("FX setup: " + msg, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
