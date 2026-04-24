#!/usr/bin/env python3
"""
Automated CRD updater.

Reads crd_source.yaml, fetches the latest CRD definitions from each upstream
source, converts them to JSON schema using openapi2jsonschema.py, and (when
run in CI) opens one PR per updated source.

Sources that have no releases or produce no CRD files are collected and removed
from crd_source.yaml in a separate cleanup PR.
"""

import argparse
import datetime
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
PR_TEMPLATE = REPO_ROOT / ".github" / "pull_request_template.md"

GITHUB_API = "https://api.github.com"
RAW_GH = "https://raw.githubusercontent.com"

DEFAULT_BRANCH = "main"


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Fetch strategies
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Conversion / placement
# ---------------------------------------------------------------------------

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


def discover_groups(json_files: list[Path]) -> dict[str, list[str]]:
    """Derive API groups from converted JSON filenames (group_kind_version.json)."""
    groups: dict[str, list[str]] = {}
    for jf in json_files:
        parts = jf.name.split("_", 1)
        if len(parts) == 2:
            groups.setdefault(parts[0], []).append(parts[1])
    return groups


# ---------------------------------------------------------------------------
# Source processing (fetch + convert, no git side-effects)
# ---------------------------------------------------------------------------

def process_source(source: dict, staging_root: Path, force: bool = False) -> dict[str, Any]:
    """Fetch and convert CRDs into a staging directory. No repo writes.

    Compares the upstream tag against ``source["tag"]`` (the last processed
    tag stored in crd_source.yaml).  If they match and *force* is False the
    source is skipped.

    Converted JSON schemas are left in ``staging_root/<slug>/`` so the caller
    can place them on a per-source branch later.
    """
    url = source["url"]
    strategy = source.get("fetch_strategy", "repo_path")
    known_tag = source.get("tag", "") or ""
    owner, repo = parse_owner_repo(url)
    label = f"{owner}/{repo}"
    slug = _branch_slug(label)

    print(f"\nProcessing {label} ({strategy})...")

    latest_tag = get_latest_tag(owner, repo)
    if not latest_tag:
        print(f"  SKIP: Could not determine latest tag for {label}")
        return {"source": label, "url": url, "tag": None, "previous_tag": known_tag,
                "changed_groups": [], "staging": None,
                "changelog": source.get("changelog", ""), "error": "no tag"}

    if latest_tag == known_tag and not force:
        print(f"  UP-TO-DATE: {label} already at {known_tag}")
        return {"source": label, "url": url, "tag": latest_tag,
                "previous_tag": known_tag, "changed_groups": [],
                "staging": None, "changelog": source.get("changelog", ""),
                "error": None, "skipped": True}

    if known_tag:
        print(f"  New tag: {known_tag} → {latest_tag}")
    else:
        print(f"  Latest tag: {latest_tag} (first run)")

    stage_dir = staging_root / slug
    dl_dir = stage_dir / "download"
    conv_dir = stage_dir / "convert"
    dl_dir.mkdir(parents=True, exist_ok=True)
    conv_dir.mkdir(parents=True, exist_ok=True)

    yaml_files: list[Path] = []
    if strategy == "release_asset":
        asset_name = source.get("asset_name", "")
        yaml_files = fetch_release_asset(owner, repo, tag=latest_tag, asset_name=asset_name, dest_dir=dl_dir)
    elif strategy == "release_url":
        template = source.get("url_template", "")
        yaml_files = fetch_release_url(template, tag=latest_tag, dest_dir=dl_dir)
    elif strategy == "repo_path":
        path = source.get("repo_path", "config/crd/bases")
        yaml_files = fetch_repo_path(owner, repo, tag=latest_tag, path=path, dest_dir=dl_dir)
    else:
        print(f"  SKIP: Unknown strategy '{strategy}'")
        return {"source": label, "url": url, "tag": latest_tag,
                "previous_tag": known_tag, "changed_groups": [],
                "staging": None, "changelog": source.get("changelog", ""),
                "error": f"unknown strategy: {strategy}"}

    if not yaml_files:
        print(f"  SKIP: No CRD files downloaded for {label}")
        return {"source": label, "url": url, "tag": latest_tag,
                "previous_tag": known_tag, "changed_groups": [],
                "staging": None, "changelog": source.get("changelog", ""),
                "error": "no files"}

    print(f"  Downloaded {len(yaml_files)} file(s)")

    json_files = convert_crds(yaml_files, conv_dir)
    print(f"  Converted to {len(json_files)} JSON schema(s)")

    discovered = discover_groups(json_files)
    print(f"  Discovered groups: {', '.join(sorted(discovered)) or '(none)'}")

    return {
        "source": label,
        "url": url,
        "tag": latest_tag,
        "previous_tag": known_tag,
        "changed_groups": list(discovered.keys()),
        "staging": str(conv_dir),
        "changelog": source.get("changelog", ""),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Index updater
# ---------------------------------------------------------------------------

def update_index(repo_root: Path):
    print("\nUpdating index.yaml...")
    subprocess.run(
        [sys.executable, str(INDEX_UPDATER), ".", "index.yaml"],
        cwd=str(repo_root),
        check=True,
    )


# ---------------------------------------------------------------------------
# Git / PR helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=str(repo_root))


def _branch_slug(label: str) -> str:
    """Turn 'owner/repo' into a safe branch segment like 'owner-repo'."""
    return re.sub(r"[^a-zA-Z0-9._-]", "-", label).strip("-").lower()


def _load_pr_template() -> str:
    if PR_TEMPLATE.exists():
        return PR_TEMPLATE.read_text()
    return ""


def _find_open_pr(repo_root: Path, branch: str) -> str | None:
    """Return the PR number for an existing open PR on *branch*, or None."""
    result = _run(
        ["gh", "pr", "list", "--head", branch, "--state", "open",
         "--json", "number", "--limit", "1"],
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        return None
    prs = json.loads(result.stdout or "[]")
    if prs:
        return str(prs[0]["number"])
    return None


def _get_changed_files(repo_root: Path) -> list[str]:
    diff = _git(repo_root, "diff", "--name-only")
    changed = [f for f in diff.stdout.strip().split("\n") if f]
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard")
    new = [f for f in untracked.stdout.strip().split("\n") if f]
    return changed + new


def _build_source_pr_body(result: dict) -> str:
    """Build a PR body for a single-source update, incorporating the PR template."""
    label = result["source"]
    tag = result.get("tag", "unknown")
    previous_tag = result.get("previous_tag", "")
    url = result.get("url", "")
    changelog = result.get("changelog", "")
    groups = result.get("changed_groups", [])

    if previous_tag and previous_tag != tag:
        version_line = f"`{previous_tag}` → `{tag}`"
    else:
        version_line = f"`{tag}` (initial import)"

    lines = [
        f"## Automated CRD Schema Update — `{label}`",
        "",
        f"Updated CRD schemas pulled from **[{label}]({url})** — {version_line}",
        "",
    ]

    if previous_tag and previous_tag != tag:
        compare_url = f"{url}/compare/{previous_tag}...{tag}"
        lines.append(f"**Release diff:** {compare_url}")
        lines.append("")

    if changelog:
        lines.append(f"**Changelog:** {changelog}")
        lines.append("")

    lines.append(f"**Source URL:** {url}")
    lines.append("")

    if groups:
        lines.append("### Updated Groups")
        lines.append("")
        for g in sorted(groups):
            lines.append(f"- `{g}/`")
        lines.append("")

    template = _load_pr_template()
    if template:
        lines.append("---")
        lines.append("")
        checked = template
        checked = checked.replace(
            "- [ ] I generated these CRs using the [CRD Extractor tool]"
            "(https://github.com/datreeio/CRDs-catalog?tab=readme-ov-file#crd-extractor). "
            "If I used a different method, I have described the method in this PR.",
            "- [x] I generated these CRs using the [CRD Extractor tool]"
            "(https://github.com/datreeio/CRDs-catalog?tab=readme-ov-file#crd-extractor). "
            "If I used a different method, I have described the method in this PR.\n"
            "  > Schemas were fetched from upstream and converted via "
            "`openapi2jsonschema.py` (automated by `update_crds.py`).",
        )
        checked = checked.replace(
            "- [ ] I am updating existing schemas and have specified the updated schema version.",
            f"- [x] I am updating existing schemas and have specified the updated schema version.\n"
            f"  > {version_line}",
        )
        checked = checked.replace(
            "- [ ] I am adding new schemas and included a link to the GitHub repository "
            "that contains the source of these schemas.",
            f"- [x] I am adding new schemas and included a link to the GitHub repository "
            f"that contains the source of these schemas.\n"
            f"  > Source: {url}",
        )
        lines.append(checked)

    return "\n".join(lines)


def _create_or_update_pr(
    repo_root: Path,
    branch: str,
    title: str,
    body: str,
    existing_pr: str | None = None,
) -> str | None:
    """Push *branch* and create or update the PR. Returns the PR URL or None.

    When *existing_pr* is provided (a PR number string), the branch is
    force-pushed (rebased content) and the PR title/body are updated.
    Otherwise a new PR is created.
    """
    push = _run(["git", "push", "-u", "origin", branch, "--force"], cwd=str(repo_root))
    if push.returncode != 0:
        print(f"  ERROR pushing {branch}: {push.stderr}", file=sys.stderr)
        return None

    if existing_pr is None:
        existing_pr = _find_open_pr(repo_root, branch)

    if existing_pr:
        edit = _run(
            ["gh", "pr", "edit", existing_pr, "--title", title, "--body", body],
            cwd=str(repo_root),
        )
        if edit.returncode == 0:
            view = _run(["gh", "pr", "view", existing_pr, "--json", "url"], cwd=str(repo_root))
            pr_url = json.loads(view.stdout or "{}").get("url", f"PR #{existing_pr}")
            print(f"  PR updated (rebased): {pr_url}")
            return pr_url
        print(f"  WARNING: failed to update PR #{existing_pr}: {edit.stderr}", file=sys.stderr)

    create = _run(
        ["gh", "pr", "create", "--title", title, "--body", body, "--head", branch],
        cwd=str(repo_root),
    )
    if create.returncode == 0:
        pr_url = create.stdout.strip()
        print(f"  PR created: {pr_url}")
        return pr_url

    print(f"  ERROR creating PR: {create.stderr}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Per-source PR workflow
# ---------------------------------------------------------------------------

def _bump_tag_in_source_file(source_path: Path, source_url: str, new_tag: str):
    """Update the tag field for a specific source URL in crd_source.yaml.

    Uses line-level matching to preserve comments and formatting.
    """
    lines = source_path.read_text().splitlines(True)
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == f'- url: "{source_url}"' or stripped == f"- url: '{source_url}'":
            # Found the source block — scan the next few lines for the tag field
            for j in range(i + 1, min(i + 10, len(lines))):
                if re.match(r'^    tag:\s', lines[j]):
                    lines[j] = f'    tag: "{new_tag}"\n'
                    source_path.write_text("".join(lines))
                    return
                if lines[j].strip().startswith("- url:"):
                    break
        i += 1


def create_source_pr(
    repo_root: Path,
    result: dict,
    source_path: Path,
    dry_run: bool = False,
) -> str | None:
    """Place staged schemas, bump tag, update index, commit on a dedicated branch, and open/update a PR.

    When an open PR already exists for the branch, the branch is rebased onto
    the latest main so the PR stays conflict-free, and the single commit
    message is updated to reflect the new version.
    """
    label = result["source"]
    tag = result.get("tag", "unknown")
    previous_tag = result.get("previous_tag", "")
    staging = result.get("staging")
    slug = _branch_slug(label)
    branch = f"auto-update/{slug}"

    if not staging:
        print(f"  No staged files for {label}, skipping PR.")
        return None

    groups = result.get("changed_groups", [])
    if dry_run:
        extra = f" ({previous_tag} → {tag})" if previous_tag else f" (→ {tag})"
        print(f"  DRY RUN: would create PR on branch '{branch}' for {label} "
              f"({len(groups)} groups){extra}")
        return None

    existing_pr = _find_open_pr(repo_root, branch)
    if existing_pr:
        print(f"  Existing PR #{existing_pr} found — will rebase onto {DEFAULT_BRANCH}")

    # Always start from a clean branch off latest main.  This guarantees the
    # branch contains exactly one commit on top of main (an implicit rebase).
    _git(repo_root, "fetch", "origin", DEFAULT_BRANCH)
    _git(repo_root, "checkout", "-B", branch, f"origin/{DEFAULT_BRANCH}")

    conv_dir = Path(staging)
    json_files = list(conv_dir.glob("*.json"))
    if not json_files:
        print(f"  No converted JSON for {label}, skipping PR.")
        _git(repo_root, "checkout", DEFAULT_BRANCH)
        return None

    placed = place_schemas(json_files, repo_root)
    result["changed_groups"] = list(placed.keys())

    _bump_tag_in_source_file(source_path, result["url"], tag)

    update_index(repo_root)

    _git(repo_root, "add", "-A")

    title = f"chore: update {label} CRD schemas to {tag}"
    commit = _git(repo_root, "commit", "-m", title)
    if commit.returncode != 0:
        if "nothing to commit" in (commit.stdout + commit.stderr):
            print(f"  Nothing to commit for {label} (schemas unchanged).")
        else:
            print(f"  WARNING: commit failed for {label}: {commit.stderr}", file=sys.stderr)
        _git(repo_root, "checkout", DEFAULT_BRANCH)
        return None

    body = _build_source_pr_body(result)
    pr_url = _create_or_update_pr(repo_root, branch, title, body, existing_pr)

    _git(repo_root, "checkout", DEFAULT_BRANCH)
    _git(repo_root, "reset", "--hard", "HEAD")
    return pr_url


# ---------------------------------------------------------------------------
# Dead-source cleanup PR
# ---------------------------------------------------------------------------

def create_cleanup_pr(
    repo_root: Path,
    failed_results: list[dict],
    source_path: Path,
    dry_run: bool = False,
) -> str | None:
    """Remove sources that have no tag or no files from crd_source.yaml and open a PR."""
    if not failed_results:
        return None

    failed_urls = {r["url"] for r in failed_results if r.get("url")}
    if not failed_urls:
        return None

    with open(source_path) as f:
        config = yaml.safe_load(f)

    original_count = len(config.get("sources", []))
    config["sources"] = [
        s for s in config.get("sources", [])
        if s.get("url") not in failed_urls
    ]
    removed_count = original_count - len(config["sources"])

    if removed_count == 0:
        return None

    print(f"\nPreparing cleanup PR: removing {removed_count} dead source(s)")

    if dry_run:
        for r in failed_results:
            print(f"  DRY RUN: would remove {r['source']} ({r.get('error', 'unknown')})")
        return None

    branch = f"auto-update/cleanup-dead-sources-{datetime.date.today().isoformat()}"
    _git(repo_root, "checkout", "-B", branch)

    header_lines = []
    with open(source_path) as f:
        for line in f:
            if line.startswith("#") or line.strip() == "":
                header_lines.append(line)
            else:
                break

    with open(source_path, "w") as f:
        for line in header_lines:
            f.write(line)
        yaml.dump(
            config,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    _git(repo_root, "add", str(source_path))

    title = f"chore: remove {removed_count} unreachable CRD source(s)"
    commit = _git(repo_root, "commit", "-m", title)
    if commit.returncode != 0:
        print(f"  WARNING: cleanup commit failed: {commit.stderr}", file=sys.stderr)
        _git(repo_root, "checkout", DEFAULT_BRANCH)
        return None

    body_lines = [
        "## Remove Unreachable CRD Sources",
        "",
        "The following sources could not be fetched (no releases, missing assets, "
        "or no CRD files) and have been removed from `crd_source.yaml`.",
        "",
        "| Source | Error |",
        "|--------|-------|",
    ]
    for r in failed_results:
        body_lines.append(f"| `{r['source']}` | {r.get('error', 'unknown')} |")

    template = _load_pr_template()
    if template:
        body_lines.extend(["", "---", "", template])

    body = "\n".join(body_lines)
    pr_url = _create_or_update_pr(repo_root, branch, title, body)

    _git(repo_root, "checkout", DEFAULT_BRANCH)
    return pr_url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Update CRD schemas from upstream sources")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write files or create PRs")
    parser.add_argument("--source", type=str,
                        help="Process only sources matching this substring")
    parser.add_argument("--parallel", type=int, default=4,
                        help="Number of parallel downloads")
    parser.add_argument("--create-pr", action="store_true",
                        help="Create per-source PRs with changes")
    parser.add_argument("--force", action="store_true",
                        help="Process sources even if the tag has not changed")
    parser.add_argument("--source-file", type=str, default=str(SOURCE_FILE),
                        help="Path to crd_source.yaml")
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

    with tempfile.TemporaryDirectory(prefix="crd-staging-") as staging_root:
        staging_path = Path(staging_root)

        # Phase 1: fetch and convert all sources in parallel (no repo writes)
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {
                executor.submit(process_source, src, staging_path, args.force): src
                for src in sources
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    src = futures[future]
                    print(f"  ERROR processing {src['url']}: {exc}", file=sys.stderr)
                    results.append({
                        "source": src["url"], "url": src["url"],
                        "tag": None, "previous_tag": src.get("tag", ""),
                        "changed_groups": [], "staging": None,
                        "changelog": src.get("changelog", ""),
                        "error": str(exc),
                    })

        updated = [r for r in results if not r.get("error") and r.get("staging")]
        up_to_date = [r for r in results if r.get("skipped") and not r.get("error")]
        errored = [r for r in results if r.get("error")]

        print(f"\n{'='*60}")
        print(f"Results: {len(updated)} to update, "
              f"{len(up_to_date)} up-to-date, "
              f"{len(errored)} errored")
        for r in updated:
            prev = r.get("previous_tag", "")
            arrow = f" ({prev} → {r['tag']})" if prev else ""
            print(f"  ✓ {r['source']} @ {r['tag']}{arrow} ({len(r['changed_groups'])} groups)")
        for r in up_to_date:
            print(f"  ≡ {r['source']} @ {r['tag']} (unchanged)")
        for r in errored:
            print(f"  ✗ {r['source']}: {r['error']}")

        if not args.create_pr:
            return

        # Phase 2: sequentially create a branch + PR per updated source
        pr_urls: list[str] = []
        for r in updated:
            url = create_source_pr(REPO_ROOT, r, source_path, args.dry_run)
            if url:
                pr_urls.append(url)

        # Phase 3: cleanup PR removing dead sources from crd_source.yaml
        cleanup_url = create_cleanup_pr(REPO_ROOT, errored, source_path, args.dry_run)
        if cleanup_url:
            pr_urls.append(cleanup_url)

    print(f"\n{'='*60}")
    print(f"PRs created/updated: {len(pr_urls)}")
    for url in pr_urls:
        print(f"  {url}")


if __name__ == "__main__":
    main()
