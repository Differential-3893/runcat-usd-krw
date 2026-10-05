#!/usr/bin/env python3
"""First publication via gh, staging only this release's reviewed source files.

No upload of the working directory, HOME, runtime keys, snapshots or logs.
No overwrite of an existing nonempty repository with different content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = "Differential-3893/runcat-usd-krw"
PUBLIC_FILES = (
    ".github/workflows/ci.yml", ".gitignore", "LICENSE", "README.md", "START_HERE_KO.md",
    "apply_local.sh", "install.sh", "uninstall.sh", "update-fx.py", "publish_github.py",
    "scripts/manage_install.py", "docs/DATA_CONTRACT.md", "docs/MAINTENANCE.md",
    "tests/common.py", "tests/test_fx.py", "tests/test_install.py", "tests/test_publish.py",
    "tests/test_release.py", "tests/fixtures/quote_synthetic.json",
)
EXECUTABLES = {"apply_local.sh", "install.sh", "uninstall.sh", "update-fx.py", "publish_github.py"}
CI_NAMES = {f"test-{osname}-py{version}" for osname in ("ubuntu-latest", "macos-latest") for version in ("3.10", "3.13")}


class PublishError(Exception):
    pass


class APIError(PublishError):
    def __init__(self, status=None):
        self.status = status
        super().__init__("GitHub API request failed" + (f" (HTTP {status})" if status else "") +
                         ". Check gh authentication, repository permission and workflow scope; raw output was not printed.")


def clean_environment():
    # Keep gh's authenticated context but suppress debug/token traces and user Git hooks/config.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "RUNCAT_", "TWELVEDATA_", "PYTHON"))
           and k not in ("GH_DEBUG", "DEBUG", "GH_HOST", "GH_REPO", "GH_PAGER", "PAGER")}
    env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1",
               GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_LFS_SKIP_SMUDGE="1")
    return env


def source_bytes(root, relative):
    p = PurePosixPath(relative)
    if p.is_absolute() or ".." in p.parts or str(p) != relative:
        raise PublishError("Invalid release path.")
    path = root / relative
    cur = path
    while cur != root:
        if cur.is_symlink():
            raise PublishError("A release path is a symlink.")
        cur = cur.parent
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
        raise PublishError("Invalid or oversized release file.")
    return path.read_bytes()


def verify_package(root=ROOT):
    manifest_raw = source_bytes(root, "release-manifest.json")
    manifest = json.loads(manifest_raw)
    hashes = manifest.get("sha256")
    if (manifest.get("version") != "1.0.0" or not isinstance(hashes, dict)
            or set(hashes) != set(PUBLIC_FILES)):
        raise PublishError("Unexpected release manifest/file list.")
    files = {}
    for name in PUBLIC_FILES:
        raw = source_bytes(root, name)
        if hashlib.sha256(raw).hexdigest() != hashes[name]:
            raise PublishError("Release file changed: " + name + ". Use an intact package.")
        files[name] = raw
    files["release-manifest.json"] = manifest_raw
    return files


def git_blob(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def api(gh, endpoint, method="GET", body=None):
    command = [gh, "api", "--hostname", "github.com", "--method", method,
               "-H", "Accept: application/vnd.github+json", endpoint]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                            capture_output=True, text=True, timeout=45, env=clean_environment())
    if result.returncode:
        match = re.search(r"\bHTTP (\d{3})\b", result.stderr)
        raise APIError(int(match.group(1)) if match else None)
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError:
        raise PublishError("GitHub returned an unexpected response; raw output was not printed.") from None


def validate_repo(repo):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repo):
        raise PublishError("Use a GitHub owner/repository name, not a URL or filesystem path.")
    return repo.split("/", 1)


def remote_state(gh, repo, files):
    """Return (missing|empty|identical, sha-or-None); refuse unrelated nonempty repos."""
    try:
        metadata = api(gh, f"repos/{repo}")
    except APIError as exc:
        if exc.status == 404:
            return "missing", None
        raise
    if metadata.get("full_name", "").lower() != repo.lower() or metadata.get("archived") or metadata.get("fork"):
        raise PublishError("The destination is renamed, archived, or a fork; publication refused.")
    # An empty repository has no Git references. Do not infer emptiness from size.
    try:
        refs = api(gh, f"repos/{repo}/git/refs?per_page=100")
    except APIError as exc:
        if exc.status == 409:
            return "empty", None
        raise
    if refs == []:
        return "empty", None
    if not isinstance(refs, list):
        raise PublishError("Could not establish the destination references.")
    main = [r for r in refs if r.get("ref") == "refs/heads/main"]
    if len(main) != 1:
        raise PublishError("This is an existing repository without the expected main branch; not overwritten.")
    sha = main[0].get("object", {}).get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PublishError("Invalid remote commit identifier.")
    tree = api(gh, f"repos/{repo}/git/trees/{sha}?recursive=1")
    if tree.get("truncated") is not False:
        raise PublishError("The remote tree is incomplete; not overwritten.")
    actual = {x["path"]: (x["sha"], x["mode"], x["type"]) for x in tree.get("tree", []) if x["type"] != "tree"}
    expected = {name: (git_blob(raw), "100755" if name in EXECUTABLES else "100644", "blob") for name, raw in files.items()}
    if actual != expected:
        raise PublishError("A nonempty repository with different content already exists. No files were overwritten; choose a new --repo name.")
    return "identical", sha


def git_run(git, args, directory, *, gh=None):
    cmd = [git, "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
    if gh:
        cmd += ["-c", "credential.helper=", "-c", "credential.helper=!" + shlex.quote(gh) + " auth git-credential"]
    result = subprocess.run(cmd + list(args), cwd=directory, env=clean_environment(), capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise PublishError("Git " + args[0] + " failed. Nothing was force-pushed. Check permissions and gh authentication; raw output was not printed.")
    return result.stdout.strip()


def stage_commit(files, directory, git, login, user_id):
    directory.mkdir(parents=True, exist_ok=True)
    git_run(git, ["init", "-b", "main", "--template="], directory)
    git_run(git, ["config", "user.name", login], directory)
    git_run(git, ["config", "user.email", f"{user_id}+{login}@users.noreply.github.com"], directory)
    for name, raw in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o755 if name in EXECUTABLES else 0o644)
    git_run(git, ["add", "--", *sorted(files)], directory)
    staged = set(git_run(git, ["ls-files"], directory).splitlines())
    if staged != set(files):
        raise PublishError("Staging did not match the explicit release file list.")
    git_run(git, ["commit", "-m", "feat: add USD/KRW daily-close change metrics for RunCat Neo"], directory)
    return git_run(git, ["rev-parse", "HEAD"], directory)


def wait_ci(gh, repo, sha, timeout=600):
    deadline = time.monotonic() + timeout
    while True:
        result = api(gh, f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest")
        if result.get("total_count", 0) > 100:
            raise PublishError("Too many check runs to safely verify in one page.")
        latest = {}
        for run in result.get("check_runs", []):
            if (run.get("head_sha") == sha and run.get("name") in CI_NAMES
                    and run.get("app", {}).get("slug") == "github-actions"):
                name = run["name"]
                if name not in latest or run["id"] > latest[name]["id"]:
                    latest[name] = run
        for name, run in latest.items():
            if run.get("status") == "completed" and run.get("conclusion") != "success":
                raise PublishError("CI FAILED: " + name + ". The published commit remains; it was not reverted or force-retried.")
        if set(latest) == CI_NAMES and all(x.get("status") == "completed" and x.get("conclusion") == "success" for x in latest.values()):
            print("ALL GITHUB CI CHECKS PASSED: " + sha)
            return
        if time.monotonic() >= deadline:
            raise PublishError("CI is still pending/unavailable. The commit is published; rerun the same command to check again.")
        time.sleep(10)


def publish(gh, git, repo, files, *, push=False, visibility=None, wait=False):
    owner, name = validate_repo(repo)
    identity = api(gh, "user")
    login, uid = identity.get("login"), identity.get("id")
    if not isinstance(login, str) or login.lower() != owner.lower() or type(uid) is not int or uid <= 0:
        raise PublishError("Authenticate gh as the destination owner. This publisher does not create organization repositories.")
    state, sha = remote_state(gh, repo, files)
    print(f"Target: {repo}; state: {state}; allowlisted source files: {len(files)}")
    if not push:
        print("PREVIEW ONLY: no GitHub changes. Add --push and explicit --public or --private for creation.")
        return
    if state == "identical":
        print("ALREADY UP TO DATE: " + sha)
    else:
        if visibility not in ("public", "private"):
            raise PublishError("Explicitly choose --public or --private for a new/empty destination.")
        # Local source staging succeeds before an empty remote repository is created.
        with tempfile.TemporaryDirectory(prefix="runcat-fx-publication-") as temp:
            directory = Path(temp) / "source"
            sha = stage_commit(files, directory, git, login, uid)
            if state == "missing":
                created = api(gh, "user/repos", "POST", {
                    "name": name, "description": "USD/KRW and previous daily-close change for RunCat Neo on macOS",
                    "private": visibility == "private", "auto_init": False,
                })
                if created.get("full_name", "").lower() != repo.lower():
                    raise PublishError("Repository creation did not return the expected destination.")
                print("CREATED: " + repo + " (" + visibility + ")")
            else:
                metadata = api(gh, f"repos/{repo}")
                if metadata.get("private") != (visibility == "private"):
                    raise PublishError("Existing repository visibility differs; it was not changed.")
            # Verify the remote still has no refs immediately before a non-forced push.
            refs = git_run(git, ["ls-remote", f"https://github.com/{repo}.git"], directory, gh=gh)
            if refs:
                raise PublishError("Remote changed during publication; no push was made.")
            git_run(git, ["push", f"https://github.com/{repo}.git", "HEAD:refs/heads/main"], directory, gh=gh)
        final_state, remote_sha = remote_state(gh, repo, files)
        if final_state != "identical" or remote_sha != sha:
            raise PublishError("The pushed revision could not be confirmed as the exact release.")
        # The first pushed branch normally becomes default. Confirm rather than assume.
        metadata = api(gh, f"repos/{repo}")
        if metadata.get("default_branch") != "main":
            raise PublishError("Code is on main, but GitHub has another default branch. No branch settings were changed automatically.")
        print("PUBLISHED: " + repo + " " + sha)
    print("https://github.com/" + repo + "/commit/" + sha)
    if wait:
        print("WAITING FOR GITHUB CI (code is already published)...")
        wait_ci(gh, repo, sha)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-package", action="store_true")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--public", dest="visibility", action="store_const", const="public")
    group.add_argument("--private", dest="visibility", action="store_const", const="private")
    parser.add_argument("--wait-ci", action="store_true")
    args = parser.parse_args(argv)
    try:
        files = verify_package()
        print("PACKAGE CHECK PASSED")
        if args.check_package:
            if args.push:
                raise PublishError("--check-package and --push are mutually exclusive.")
            return 0
        gh, git = shutil.which("gh"), shutil.which("git")
        if not gh or not git:
            raise PublishError("Both gh and git must be installed and gh authenticated.")
        publish(os.path.abspath(gh), os.path.abspath(git), args.repo, files,
                push=args.push, visibility=args.visibility, wait=args.wait_ci)
        return 0
    except Exception as exc:
        print("PUBLICATION STOPPED: " + (str(exc) if isinstance(exc, PublishError) else
              "An operation failed; raw credentials/responses were not printed."), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
