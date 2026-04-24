#!/usr/bin/env python3
"""
Automated CRD updater.

Reads crd_source.yaml, fetches the latest CRD definitions from each upstream
source, converts them to JSON schema using openapi2jsonschema.py, and (when
run in CI) creates a PR if anything changed.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CONVERTER = SCRIPT_DIR / "openapi2jsonschema.py"
SOURCE_FILE = REPO_ROOT / "crd_source.yaml"
INDEX_UPDATER = SCRIPT_DIR / "auto-update-index.py"

GITHUB_API = "https://api.github.com"
RAW_GH = "https://raw.githubusercontent.com"


def gh_headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def gh_get(url: str) -> Any:
    req = urllib.request.Request(url, headers=gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"  WARNING: GitHub API {exc.code} for {url}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"  WARNING: Request failed for {url}: {exc}", file=sys.stderr)
        return None


def download(url: str, dest: Path) -> bool:
    req = urllib.request.Request(url, headers=gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception as exc:
        print(f"  WARNING: Download failed {url}: {exc}", file=sys.stderr)
        return False


def parse_owner_repo(gh_url: str) -> tuple[str, str]:
    parts = gh_url.rstrip("/").split("/")
    return parts[-2], parts[-1]


def get_latest_tag(owner: str, repo: str) -> str | None:
    data = gh_get(f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest")
    if data and "tag_name" in data:
        return data["tag_name"]
    data = gh_get(f"{GITHUB_API}/repos/{owner}/{repo}/tags?per_page=1")
    if data and len(data) > 0:
        return data[0]["name"]
    return None


def list_repo_files(owner: str, repo: str, path: str, ref: str) -> list[dict]:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    data = gh_get(url)
    if isinstance(data, list):
        return data
    return []


def fetch_release_asset(owner: str, repo: str, tag: str, asset_name: str, dest_dir: Path) -> list[Path]:
    release = gh_get(f"{GITHUB_API}/repos/{owner}/{repo}/releases/tags/{tag}")
    if not release:
        release = gh_get(f"{GITHUB_API}/repos/{owner}/{repo}/releases/latest")
    if not release or "assets" not in release:
        print(f"  WARNING: No release found for {owner}/{repo}@{tag}", file=sys.stderr)
        return []

    for asset in release["assets"]:
        if asset["name"] == asset_name:
            dest = dest_dir / asset_name
            dl_url = asset["browser_download_url"]
            if download(dl_url, dest):
                return [dest]
    print(f"  WARNING: Asset '{asset_name}' not found in {owner}/{repo}@{tag}", file=sys.stderr)
    return []


def fetch_repo_path(owner: str, repo: str, tag: str, path: str, dest_dir: Path) -> list[Path]:
    files = list_repo_files(owner, repo, path, tag)
    downloaded = []
    for f in files:
        name = f.get("name", "")
        if not (name.endswith(".yaml") or name.endswith(".yml")):
            continue
        raw_url = f"{RAW_GH}/{owner}/{repo}/{tag}/{path}/{name}"
        dest = dest_dir / name
        if download(raw_url, dest):
            downloaded.append(dest)
    return downloaded


def fetch_release_url(url_template: str, tag: str, dest_dir: Path) -> list[Path]:
    url = url_template.replace("{version}", tag)
    name = url.split("/")[-1]
    dest = dest_dir / name
    if download(url, dest):
        return [dest]
    return []


def convert_crds(yaml_files: list[Path], out_dir: Path) -> list[Path]:
    if not yaml_files:
        return []

    env = os.environ.copy()
    env["FILENAME_FORMAT"] = "{fullgroup}_{kind}_{version}"

    result = subprocess.run(
        [sys.executable, str(CONVERTER)] + [str(f) for f in yaml_files],
        cwd=str(out_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  WARNING: Conversion errors:\n{result.stderr}", file=sys.stderr)

    return list(out_dir.glob("*.json"))


def place_schemas(json_files: list[Path], repo_root: Path) -> dict[str, list[str]]:
    """Move JSON schemas into group directories. Returns {group: [files]}."""
    placed: dict[str, list[str]] = {}
    for jf in json_files:
        name = jf.name
        parts = name.split("_", 1)
        if len(parts) != 2:
            continue
        group, rest = parts
        group_dir = repo_root / group
        group_dir.mkdir(exist_ok=True)
        dest = group_dir / rest
        shutil.copy2(jf, dest)
        placed.setdefault(group, []).append(rest)
    return placed


def process_source(source: dict, repo_root: Path, dry_run: bool = False) -> dict[str, Any]:
    url = source["url"]
    strategy = source.get("fetch_strategy", "repo_path")
    owner, repo = parse_owner_repo(url)
    groups = source.get("groups", [])
    label = f"{owner}/{repo}"

    print(f"\nProcessing {label} ({strategy})...")

    tag = get_latest_tag(owner, repo)
    if not tag:
        print(f"  SKIP: Could not determine latest tag for {label}")
        return {"source": label, "tag": None, "changed_groups": [], "error": "no tag"}

    print(f"  Latest tag: {tag}")

    with tempfile.TemporaryDirectory() as tmpdir:
        dl_dir = Path(tmpdir) / "download"
        conv_dir = Path(tmpdir) / "convert"
        dl_dir.mkdir()
        conv_dir.mkdir()

        yaml_files: list[Path] = []
        if strategy == "release_asset":
            asset_name = source.get("asset_name", "")
            yaml_files = fetch_release_asset(owner, repo, tag, asset_name, dl_dir)
        elif strategy == "release_url":
            template = source.get("url_template", "")
            yaml_files = fetch_release_url(template, tag, dl_dir)
        elif strategy == "repo_path":
            path = source.get("repo_path", "config/crd/bases")
            yaml_files = fetch_repo_path(owner, repo, tag, path, dl_dir)
        else:
            print(f"  SKIP: Unknown strategy '{strategy}'")
            return {"source": label, "tag": tag, "changed_groups": [], "error": f"unknown strategy: {strategy}"}

        if not yaml_files:
            print(f"  SKIP: No CRD files downloaded for {label}")
            return {"source": label, "tag": tag, "changed_groups": [], "error": "no files"}

        print(f"  Downloaded {len(yaml_files)} file(s)")

        json_files = convert_crds(yaml_files, conv_dir)
        print(f"  Converted to {len(json_files)} JSON schema(s)")

        if not dry_run:
            placed = place_schemas(json_files, repo_root)
            changed = list(placed.keys())
        else:
            changed = []

    return {
        "source": label,
        "tag": tag,
        "changed_groups": changed,
        "changelog": source.get("changelog", ""),
        "error": None,
    }


def update_index(repo_root: Path):
    print("\nUpdating index.yaml...")
    subprocess.run(
        [sys.executable, str(INDEX_UPDATER), ".", "index.yaml"],
        cwd=str(repo_root),
        check=True,
    )


def create_pr(repo_root: Path, results: list[dict], dry_run: bool = False):
    os.chdir(repo_root)

    diff = subprocess.run(
        ["git", "diff", "--name-only"],
        capture_output=True, text=True
    )
    changed_files = [f for f in diff.stdout.strip().split("\n") if f]

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True,
    )
    new_files = [f for f in untracked.stdout.strip().split("\n") if f]

    all_changed = changed_files + new_files
    json_changes = [f for f in all_changed if f.endswith(".json")]

    if not json_changes:
        print("\nNo CRD changes detected. Nothing to do.")
        return

    print(f"\n{len(json_changes)} schema file(s) changed/added")

    if dry_run:
        print("DRY RUN: Would create PR with these changes:")
        for f in json_changes[:20]:
            print(f"  {f}")
        if len(json_changes) > 20:
            print(f"  ... and {len(json_changes) - 20} more")
        return

    import datetime
    branch = f"auto-update-crds-{datetime.date.today().isoformat()}"

    subprocess.run(["git", "checkout", "-b", branch], check=True)
    subprocess.run(["git", "add", "-A"], check=True)

    successful = [r for r in results if not r.get("error") and r.get("changed_groups")]
    groups_changed = set()
    for r in successful:
        groups_changed.update(r["changed_groups"])

    title = f"chore: update CRD schemas ({len(json_changes)} files)"

    body_lines = [
        "## Automated CRD Schema Update",
        "",
        f"Updated {len(json_changes)} JSON schema file(s) from upstream sources.",
        "",
        "### Sources Updated",
        "",
    ]
    for r in successful:
        tag_str = f" @ `{r['tag']}`" if r.get("tag") else ""
        changelog_str = ""
        if r.get("changelog"):
            changelog_str = f" ([changelog]({r['changelog']}))"
        body_lines.append(
            f"- **{r['source']}**{tag_str}{changelog_str}"
        )
        for g in r.get("changed_groups", []):
            body_lines.append(f"  - `{g}`")

    errors = [r for r in results if r.get("error")]
    if errors:
        body_lines.extend(["", "### Skipped Sources", ""])
        for r in errors:
            body_lines.append(f"- {r['source']}: {r['error']}")

    body_lines.extend([
        "",
        "### Changed Groups",
        "",
    ])
    for g in sorted(groups_changed):
        body_lines.append(f"- `{g}/`")

    body = "\n".join(body_lines)

    subprocess.run(
        ["git", "commit", "-m", title],
        check=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", branch],
        check=True,
    )

    result = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"\nPR created: {result.stdout.strip()}")
    else:
        print(f"\nFailed to create PR: {result.stderr}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Update CRD schemas from upstream sources")
    parser.add_argument("--dry-run", action="store_true", help="Don't write files or create PRs")
    parser.add_argument("--source", type=str, help="Process only sources matching this substring")
    parser.add_argument("--parallel", type=int, default=4, help="Number of parallel downloads")
    parser.add_argument("--create-pr", action="store_true", help="Create a PR with changes")
    parser.add_argument(
        "--source-file",
        type=str,
        default=str(SOURCE_FILE),
        help="Path to crd_source.yaml",
    )
    args = parser.parse_args()

    source_path = Path(args.source_file)
    if not source_path.exists():
        print(f"Error: Source file not found: {source_path}", file=sys.stderr)
        sys.exit(1)

    with open(source_path) as f:
        config = yaml.safe_load(f)

    sources = config.get("sources", [])
    if args.source:
        sources = [s for s in sources if args.source.lower() in s["url"].lower()]

    print(f"Processing {len(sources)} source(s)...")

    results = []
    with ThreadPoolExecutor(max_workers=args.parallel) as executor:
        futures = {
            executor.submit(process_source, src, REPO_ROOT, args.dry_run): src
            for src in sources
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as exc:
                src = futures[future]
                print(f"  ERROR processing {src['url']}: {exc}", file=sys.stderr)
                results.append({
                    "source": src["url"],
                    "tag": None,
                    "changed_groups": [],
                    "error": str(exc),
                })

    if not args.dry_run:
        update_index(REPO_ROOT)

    successful = [r for r in results if not r.get("error") and r.get("changed_groups")]
    errored = [r for r in results if r.get("error")]

    print(f"\n{'='*60}")
    print(f"Results: {len(successful)} updated, {errored and len(errored) or 0} skipped/errored")
    for r in successful:
        print(f"  ✓ {r['source']} @ {r['tag']} ({len(r['changed_groups'])} groups)")
    for r in errored:
        print(f"  ✗ {r['source']}: {r['error']}")

    if args.create_pr:
        create_pr(REPO_ROOT, results, args.dry_run)


if __name__ == "__main__":
    main()
