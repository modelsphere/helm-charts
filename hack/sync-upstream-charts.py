#!/usr/bin/env python3
"""Sync vendored charts from each upstream repo's latest stable GitHub Release.

This repository publishes on every push to main (release.yml), so a chart
copied from upstream has to be reviewed first. The script only writes the
working tree; the workflow opens a pull request and never pushes to main.

A release is stable when it is not a draft, not marked prerelease, and its
tag is plain semver (an optional leading v, no pre-release suffix). The
highest version wins. The GitHub "latest" flag is ignored: it follows
publication time, and a release candidate published after the current stable
tag would otherwise win.

The chart here is replaced only when that version is strictly newer than
charts/<name>/Chart.yaml, and only when the tag matches upstream's Chart.yaml
version. An equal or older upstream is left untouched, so a helm-charts-only
edit is never overwritten by a re-run.

Image repositories are rewritten to the Docker Hub mirrors this repository
publishes, except in README*.md (those document the OCI chart) and except
oci:// chart references. hack/sync-upstream-charts.yaml holds the per-chart
rewrites, the text patches that keep our comments, and the files we keep.

    hack/sync-upstream-charts.py --check
    hack/sync-upstream-charts.py --only swiss
    hack/sync-upstream-charts.py --pr-body /tmp/pr.md

Exit 0 when nothing is behind, or every chart that was behind was synced.
Exit 1 when a chart needs a human (a patch no longer matches, a kept
Chart.yaml's dependencies diverged, the release tag and Chart.yaml version
disagree, lint or template failed). A missing Docker Hub image is a warning
and a skip, not a failure: the image is often simply not published yet, and
the chart already in this repo still installs.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Strict semver, optional leading v. A pre-release identifier (rc, beta,
# alpha, preview, snapshot, ...) is captured and is not stable. Tags that do
# not match at all (0.7.4rc1, nightly, swiss-chart-1.0) are ignored rather
# than guessed at.
_SEMVER = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)

# The same line match as hack/check-chart-images.sh. Kept here so the sync
# gates the images that script will later refuse to publish.
_IMAGE_LINE = re.compile(r'^[\s-]*image:\s*"?([^"\s]+)"?')

_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

_USER_AGENT = "modelsphere-helm-charts-sync"


class ChartError(Exception):
    """A chart cannot be synced without a human looking at it."""


class UnsafePath(ChartError):
    """A tarball member would write outside the chart directory."""


class PatchError(ChartError):
    """A configured patch does not match the upstream text."""


class ImageMissing(Exception):
    """A Docker Hub mirror the chart renders is not published yet.

    Not a ChartError: the chart is left as it was and the run still succeeds.
    The image usually shows up on a later daily run.
    """

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("; ".join(missing))


@dataclass
class Result:
    name: str
    current: str
    upstream: str
    result: str
    repo: str = ""
    images: list[str] = field(default_factory=list)
    detail: str = ""


def parse_semver(tag: str | None):
    """Return (major, minor, patch, prerelease or None), or None if unparseable.

    Build metadata is accepted and dropped. Semver precedence ignores it, and
    a `+build` suffix must not hide a stable release.
    """
    if tag is None:
        return None
    match = _SEMVER.fullmatch(tag.strip())
    if not match:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        match.group(4),
    )


def normalize_version(value: str):
    """Parse a Chart.yaml version: strip quotes, then the semver above.

    A leading v is part of the semver pattern (`v0.5.1` and `0.5.1` are the
    same version). Quotes are not, so they come off first.
    """
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return parse_semver(text)


def _order(parsed) -> tuple:
    """(major, minor, patch, release beats a pre-release of the same numbers).

    Pre-release identifiers are not ordered against each other.
    select_stable_release already drops them. A local Chart.yaml can still be
    a pre-release, and the release of those numbers has to count as newer.
    """
    major, minor, patch, pre = parsed
    return (major, minor, patch, pre is None)


def is_strictly_newer(upstream_tag: str, current_version: str) -> bool:
    """True when upstream_tag is a newer version than the chart's version.

    Equal versions are not newer: re-running must not overwrite edits that
    exist only in this repository.
    """
    upstream = normalize_version(upstream_tag)
    current = normalize_version(current_version)
    if upstream is None or current is None:
        raise ChartError(
            f"cannot compare versions {upstream_tag!r} and {current_version!r}"
        )
    return _order(upstream) > _order(current)


def require_tag_matches_chart_version(repo: str, tag: str, chart_version: str) -> None:
    """The release tag and Chart.yaml `version` must be the same semver.

    Leading v and quotes are ignored. An unparseable chart version is a
    mismatch: the version that would be written has to be the tag, or a later
    run cannot tell the chart was updated. appVersion is not compared; it can
    differ from the tag.
    """
    parsed_tag = normalize_version(tag)
    parsed_chart = normalize_version(chart_version)
    if parsed_chart is not None and parsed_tag == parsed_chart:
        return
    raise ChartError(
        f"{repo} release {tag} has Chart.yaml version {chart_version}; "
        "the tag and chart version must match"
    )


def select_stable_release(releases: list[dict]) -> dict | None:
    """The highest stable release, or None.

    Drafts and anything GitHub marked prerelease are skipped. So is a tag
    GitHub did *not* mark prerelease when the tag itself has a pre-release
    suffix (0.7.4-rc10, 1.0.0-preview, 1.0.0-SNAPSHOT, ...): the flag is not
    reliable, the tag is. Publication order is not used. 0.10.0 beats 0.9.9
    even when 0.9.9 was published later.
    """
    best = None
    best_key = None
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        parsed = parse_semver(str(release.get("tag_name") or ""))
        if parsed is None or parsed[3] is not None:
            continue
        key = parsed[:3]
        if best_key is None or key > best_key:
            best = release
            best_key = key
    return best


def next_link(header: str | None) -> str | None:
    """The `rel="next"` URL from a GitHub Link header, if there is one."""
    if not header:
        return None
    for part in header.split(","):
        match = _LINK_NEXT.search(part.strip())
        if match:
            return match.group(1)
    return None


def rewrite_images(text: str, rewrites: list[tuple[str, str]]) -> str:
    """Replace image repository references and nothing adjacent to them.

    The next character must be the end of the text, ":", "@", whitespace or
    a quote. That is what stops ghcr.io/modelsphere/swissd from also matching
    ghcr.io/modelsphere/swissd-foo, and what leaves a repository named in a
    sentence (`...swissd).`) alone so the Docker Hub comment survives.

    An oci:// reference is a Helm chart repository, not an image. It is never
    rewritten, even when the repository string is the one being replaced.
    README files are not passed here; the caller skips them.
    """
    for src, dst in rewrites:
        if not src or src == dst:
            continue
        pattern = re.compile(
            r"(?<!oci://)(?<![\w./-])" + re.escape(src) + r"(?=$|[:@\s\"'])"
        )
        text = pattern.sub(lambda _match, dest=dst: dest, text)
    return text


def apply_patch(text: str, find: str, replace: str, *, regex: bool = False) -> str:
    """Apply one patch. Already-applied text is left as it is.

    A find that matches is replaced. A find that does not match, while the
    replacement is already present, is the patch having been applied already
    (upstream caught up, or a kept file). Anything else means the upstream
    text moved and the patch would be guessing: raise PatchError.
    `regex` uses Python `re` with MULTILINE. The replacement is literal text,
    not a `re.sub` template, so backslashes in a comment stay backslashes.
    """
    if regex:
        try:
            pattern = re.compile(find, re.MULTILINE)
        except re.error as exc:
            raise PatchError(f"invalid regex: {exc}") from exc
        if pattern.search(text):
            return pattern.sub(lambda _match: replace, text)
    elif find in text:
        return text.replace(find, replace)
    if replace in text:
        return text
    raise PatchError("find did not match and the replacement is not already present")


def _split_scalar(rest: str) -> tuple[str, str]:
    """Split `version: <rest>` into (raw value, inline-comment tail).

    The tail includes the whitespace before a `#` comment, so rewriting the
    value does not move the comment.
    """
    if rest[:1] in "\"'":
        quote = rest[0]
        end = rest.find(quote, 1)
        if end == -1:
            raise ChartError(f"unterminated quote in Chart.yaml: {rest!r}")
        return rest[: end + 1], rest[end + 1 :]
    for index, char in enumerate(rest):
        if char == "#" and (index == 0 or rest[index - 1] in " \t"):
            start = index
            while start > 0 and rest[start - 1] in " \t":
                start -= 1
            return rest[:start], rest[start:]
    return rest, ""


def read_top_level_scalar(text: str, key: str) -> str:
    """The inner text of a top-level `key:` scalar, quotes removed.

    Only column 0 counts. A dependency's indented `version:` is a different
    field and must not be read as the chart version.
    """
    for line in text.splitlines():
        match = re.match(rf"^{re.escape(key)}:[ \t]*(.*)$", line)
        if not match:
            continue
        raw, _comment = _split_scalar(match.group(1))
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            return raw[1:-1]
        return raw.strip()
    raise ChartError(f"Chart.yaml has no top-level {key}:")


def set_chart_versions(text: str, version: str, app_version: str) -> str:
    """Set top-level version and appVersion without reformatting the file.

    Quoting stays as it was (`version: 0.5.0` stays unquoted,
    `appVersion: "0.5.0"` stays double-quoted) and every comment survives.
    Dumping the YAML back out would drop both, which is how a kept Chart.yaml
    loses the comment that says why appVersion is not simply "latest".
    """
    return _set_scalar(_set_scalar(text, "version", version), "appVersion", app_version)


def _set_scalar(text: str, key: str, new_inner: str) -> str:
    lines = text.splitlines(keepends=True)
    out = []
    done = False
    for line in lines:
        if done:
            out.append(line)
            continue
        newline = ""
        body = line
        if body.endswith("\r\n"):
            body, newline = body[:-2], "\r\n"
        elif body.endswith("\n"):
            body, newline = body[:-1], "\n"
        match = re.match(rf"^{re.escape(key)}:([ \t]*)(.*)$", body)
        if not match:
            out.append(line)
            continue
        space, rest = match.group(1), match.group(2)
        if space == "":
            space = " "
        raw, comment = _split_scalar(rest)
        quote = ""
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            quote = raw[0]
        written = f"{quote}{new_inner}{quote}" if quote else new_inner
        out.append(f"{key}:{space}{written}{comment}{newline}")
        done = True
    if not done:
        raise ChartError(f"Chart.yaml has no top-level {key}:")
    return "".join(out)


def chart_dependencies(text: str):
    """The parsed `dependencies` list, or None when the chart has none."""
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ChartError("Chart.yaml is not a mapping")
    return data.get("dependencies")


def dependencies_differ(upstream_text: str, kept_text: str) -> bool:
    """True when a kept Chart.yaml does not declare upstream's dependencies.

    version and appVersion are allowed to differ; they are copied separately.
    A dependency change (a new chart, a bumped subchart version, a different
    repository) is a decision about what this repo installs, not a mechanical
    copy, so the sync stops and asks for a human.
    """
    return chart_dependencies(upstream_text) != chart_dependencies(kept_text)


def rendered_images(render: str) -> list[str]:
    """Unique `image:` values from a `helm template` render, in sorted order."""
    found = []
    for line in render.splitlines():
        match = _IMAGE_LINE.match(line)
        if match:
            found.append(match.group(1))
    return sorted(set(found))


def image_repository(ref: str) -> str:
    """Repository portion of an image reference, without tag or digest.

    A colon before the last slash is a registry port (`localhost:5000/foo`)
    and stays. A colon after the last slash is a tag and is dropped. A digest
    (`repo@sha256:...`) is dropped.
    """
    without_digest = ref.split("@", 1)[0]
    slash = without_digest.rfind("/")
    colon = without_digest.rfind(":")
    if colon > slash:
        return without_digest[:colon]
    return without_digest


def _assert_safe_rel(rel: str) -> None:
    if rel.startswith("/") or rel.startswith("\\"):
        raise UnsafePath(rel)
    parts = rel.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise UnsafePath(rel)


def _locate_prefix(names: list[str], rel_path: str) -> str:
    """The tarball directory that corresponds to `rel_path`.

    GitHub tag tarballs wrap the repo in one top-level directory
    (`swiss-0.7.3/helm/swiss/...`, and a leading `v` on the tag is not always
    in that directory name). Find the path rather than guessing the wrapper.
    """
    rel_parts = [part for part in rel_path.split("/") if part and part != "."]
    found = set()
    for name in names:
        parts = [part for part in name.split("/") if part and part != "."]
        if any(part == ".." for part in parts):
            continue
        for index in range(len(parts) - len(rel_parts) + 1):
            if parts[index : index + len(rel_parts)] == rel_parts:
                found.add("/".join(parts[: index + len(rel_parts)]))
                break
    if not found:
        raise ChartError(f"{rel_path} not found in the release tarball")
    ordered = sorted(found, key=len)
    root = ordered[0]
    for other in ordered[1:]:
        if other != root and not other.startswith(root + "/"):
            raise ChartError(
                f"ambiguous path {rel_path} in the tarball ({root}, {other})"
            )
    return root


def extract_chart(tar_bytes: bytes, rel_path: str, dest: Path) -> None:
    """Extract only `rel_path` from a GitHub tag tarball into `dest`.

    Members are written by hand. tarfile.extract honours absolute paths,
    `..` and links, which a release tarball must not be able to use to write
    outside `dest`. Links are refused outright: a chart does not need them,
    and a link pointing at a file outside the chart would copy it in.
    """
    rel_path = rel_path.strip("/")
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        members = tar.getmembers()
        prefix = _locate_prefix([member.name for member in members], rel_path)
        dest.mkdir(parents=True, exist_ok=True)
        dest_root = dest.resolve()
        prefix_slash = prefix.rstrip("/") + "/"
        for member in members:
            name = member.name
            if name.rstrip("/") == prefix.rstrip("/"):
                continue
            if not name.startswith(prefix_slash):
                continue
            rel = name[len(prefix_slash) :]
            if not rel:
                continue
            _assert_safe_rel(rel)
            target = dest / rel
            resolved = target.resolve()
            if resolved != dest_root and dest_root not in resolved.parents:
                raise UnsafePath(name)
            if member.issym() or member.islnk():
                raise UnsafePath(f"refusing link {name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            source = tar.extractfile(member)
            if source is None:
                raise ChartError(f"cannot read {name} from the tarball")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source:
                target.write_bytes(source.read())
            mode = member.mode & 0o777
            if mode:
                target.chmod(mode)


def _is_excluded(rel: str, exclude: list[str]) -> bool:
    rel = rel.strip("/")
    for item in exclude:
        item = item.strip("/")
        if item and (rel == item or rel.startswith(item + "/")):
            return True
    return False


def copy_chart(src: Path, dest: Path, exclude: list[str]) -> None:
    """Copy an extracted chart, dropping `exclude` paths (Chart.lock, ...)."""

    def ignore(directory: str, names: list[str]):
        rel_dir = Path(directory).resolve().relative_to(src.resolve())
        ignored = []
        for name in names:
            rel = name if rel_dir == Path(".") else (rel_dir / name).as_posix()
            if _is_excluded(rel, exclude):
                ignored.append(name)
        return ignored

    shutil.copytree(src, dest, ignore=ignore)


def restore_keep(old: Path, new: Path, keep: list[str]) -> None:
    """Put the files this repo owns back over the upstream copy."""
    for rel in keep:
        rel = rel.strip("/")
        if not rel:
            continue
        source = old / rel
        if not source.exists():
            continue
        target = new / rel
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, symlinks=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _github_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _token() -> str | None:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


def list_releases(repo: str, token: str | None = None) -> list[dict]:
    """Every release from the GitHub REST API, following Link rel=next.

    Unauthenticated works for a public repo; a token (GH_TOKEN or
    GITHUB_TOKEN) raises the rate limit and is what the workflow passes.
    """
    url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    headers = _github_headers(token)
    releases: list[dict] = []
    pages = 0
    while url:
        pages += 1
        if pages > 20:
            raise ChartError(f"too many release pages for {repo}")
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                link = response.headers.get("Link")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ChartError(
                f"listing releases for {repo} failed: HTTP {exc.code} {detail}"
            ) from exc
        data = json.loads(body)
        if not isinstance(data, list):
            message = data.get("message", data) if isinstance(data, dict) else data
            raise ChartError(f"listing releases for {repo}: {message}")
        releases.extend(data)
        url = next_link(link)
    return releases


def download_tarball(repo: str, tag: str, token: str | None = None) -> bytes:
    """The tag tarball. codeload first, the API tarball if that is refused.

    codeload serves public tags without a token. The API endpoint is the
    fallback (it honours the token) so a private upstream still works once a
    token is configured.
    """
    quoted = urllib.parse.quote(tag, safe="")
    codeload = f"https://codeload.github.com/{repo}/tar.gz/refs/tags/{quoted}"
    request = urllib.request.Request(codeload, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as first:
        api = f"https://api.github.com/repos/{repo}/tarball/{quoted}"
        fallback = urllib.request.Request(api, headers=_github_headers(token))
        try:
            with urllib.request.urlopen(fallback, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as second:
            raise ChartError(
                f"downloading {repo}@{tag} failed: "
                f"codeload HTTP {first.code}, api HTTP {second.code}"
            ) from second


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ChartError(f"timed out: {' '.join(cmd)}") from exc


def _tail(text: str, limit: int = 40) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _materialize_deps(dest: Path) -> dict | None:
    """Build chart dependencies the way ci.yml does, and remember what we added.

    A downloaded OCI dependency (console's swiss chart) lands in
    charts/<name>/charts/*.tgz with a Chart.lock. Both are gitignored, and
    neither belongs in the tree after the gate: Chart.lock is excluded from
    the copy on purpose. They are removed once lint and template have run.
    """
    chart = (dest / "Chart.yaml").read_text(encoding="utf-8")
    if not re.search(r"(?m)^dependencies:", chart):
        return None
    charts_dir = dest / "charts"
    lock = dest / "Chart.lock"
    state = {"charts": charts_dir.exists(), "lock": lock.exists()}
    cmd = ["helm", "dependency", "build" if lock.exists() else "update", str(dest)]
    completed = _run(cmd)
    if completed.returncode != 0 and cmd[2] == "build":
        completed = _run(["helm", "dependency", "update", str(dest)])
    if completed.returncode != 0:
        _cleanup_deps(dest, state)
        raise ChartError(
            "helm dependency failed:\n" + _tail(completed.stderr or completed.stdout)
        )
    return state


def _cleanup_deps(dest: Path, state: dict | None) -> None:
    if not state:
        return
    charts_dir = dest / "charts"
    if charts_dir.exists():
        if not state["charts"]:
            shutil.rmtree(charts_dir)
        else:
            for tarball in charts_dir.glob("*.tgz"):
                tarball.unlink()
    if not state["lock"]:
        lock = dest / "Chart.lock"
        if lock.exists():
            lock.unlink()


def gate_chart(
    dest: Path,
    ci_args: str,
    to_repos: set[str],
    *,
    skip_image_check: bool,
) -> list[str]:
    """Lint, render, and confirm the Docker Hub mirrors this chart publishes.

    crane is asked only for images whose repository is one of the target's
    rewrite `to` values. Those are the mirrors this repository is responsible
    for. Other rendered images (ghcr.io/modelsphere/catalog-server, which this
    repo deliberately does not rewrite, for example) are not this gate.
    hack/check-chart-images.sh still checks every image of a chart whose
    version moved, in the workflow, before the pull request is pushed.

    Returns the image references that were digested.
    """
    state = _materialize_deps(dest)
    checked: list[str] = []
    try:
        args = shlex.split(ci_args or "")
        lint = _run(["helm", "lint", "--strict", str(dest), *args])
        if lint.returncode != 0:
            raise ChartError(
                "helm lint --strict failed:\n" + _tail(lint.stdout + "\n" + lint.stderr)
            )
        rendered = _run(["helm", "template", "ci", str(dest), *args])
        if rendered.returncode != 0:
            raise ChartError(
                "helm template failed:\n" + _tail(rendered.stderr or rendered.stdout)
            )
        images = rendered_images(rendered.stdout)
        if not images:
            raise ChartError(
                "helm template rendered no image: line; the gate would pass a chart that installs nothing"
            )
        if skip_image_check:
            return images
        missing = []
        for image in images:
            if image_repository(image) not in to_repos:
                continue
            digest = _run(["crane", "digest", image], timeout=60)
            if digest.returncode != 0:
                err = (digest.stderr or digest.stdout).strip().splitlines()
                missing.append(f"{image} ({err[-1] if err else 'crane digest failed'})")
            else:
                checked.append(image)
                _say(f"   ok  {image}  ({digest.stdout.strip()})")
        if missing:
            raise ImageMissing(missing)
        if to_repos and not checked:
            raise ChartError(
                "rendered no image whose repository is "
                + ", ".join(sorted(to_repos))
                + "; the image rewrite did not apply"
            )
        return checked
    finally:
        _cleanup_deps(dest, state)


def _restore(dest: Path, backup: Path | None, had_dest: bool) -> None:
    """Put dest back to the tree from before this chart was replaced."""
    if dest.exists():
        shutil.rmtree(dest)
    if had_dest and backup is not None and backup.exists():
        shutil.copytree(backup, dest, symlinks=True)


def _kept_chart(keep: list[str]) -> bool:
    return any(path.strip("/") == "Chart.yaml" for path in keep)


def _apply_kept_chart_versions(dest: Path, extracted: Path, keep: list[str]) -> None:
    if not _kept_chart(keep):
        return
    upstream_text = (extracted / "Chart.yaml").read_text(encoding="utf-8")
    kept_path = dest / "Chart.yaml"
    kept_text = kept_path.read_text(encoding="utf-8")
    if dependencies_differ(upstream_text, kept_text):
        raise ChartError(
            "kept Chart.yaml dependencies differ from upstream; "
            "reconcile them by hand before the next sync"
        )
    updated = set_chart_versions(
        kept_text,
        read_top_level_scalar(upstream_text, "version"),
        read_top_level_scalar(upstream_text, "appVersion"),
    )
    kept_path.write_text(updated, encoding="utf-8")


def _apply_patches(dest: Path, patches: list[dict]) -> None:
    for patch in patches:
        path = dest / patch["file"]
        if not path.is_file():
            raise ChartError(f"patch file {patch['file']} is not in the chart")
        text = path.read_text(encoding="utf-8")
        try:
            updated = apply_patch(
                text, patch["find"], patch["replace"], regex=bool(patch.get("regex"))
            )
        except PatchError as exc:
            raise ChartError(f"patch {patch['file']}: {exc}") from exc
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def _rewrite_tree(dest: Path, pairs: list[tuple[str, str]]) -> None:
    if not pairs:
        return
    for path in sorted(dest.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        # README*.md documents the OCI chart (image and oci:// URL included)
        # and is what this repo already ships. Rewriting it would make the
        # README describe a different image than the one it is about.
        if fnmatch.fnmatch(path.name, "README*.md"):
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        updated = rewrite_images(text, pairs)
        if updated != text:
            path.write_bytes(updated.encode("utf-8"))


def sync_target(target: dict, *, check: bool, skip_image_check: bool) -> Result:
    """Sync one chart, or in check mode only report whether it is behind."""
    name = target["name"]
    dest = Path(target["dest"])
    chart_yaml = dest / "Chart.yaml"
    if not chart_yaml.is_file():
        _annotate("error", name, f"{dest}/Chart.yaml does not exist")
        return Result(name, "", "", "needs attention", target["repo"], detail="dest Chart.yaml missing")

    current = read_top_level_scalar(chart_yaml.read_text(encoding="utf-8"), "version")
    releases = list_releases(target["repo"], _token())
    stable = select_stable_release(releases)
    if stable is None:
        _annotate("error", name, "no stable GitHub Release")
        return Result(name, current, "", "needs attention", target["repo"], detail="no stable release")

    tag = str(stable["tag_name"])
    if not is_strictly_newer(tag, current):
        upstream = normalize_version(tag)
        have = normalize_version(current)
        if upstream is not None and have is not None and _order(upstream) < _order(have):
            _say(f"{name}: current ({current}; upstream stable {tag} is older)")
        else:
            _say(f"{name}: current ({current})")
        return Result(name, current, tag, "current", target["repo"])

    if check:
        _say(f"{name}: behind ({current} -> {tag})")
        return Result(name, current, tag, "behind", target["repo"])

    _say(f"{name}: syncing {current} -> {tag}")
    blob = download_tarball(target["repo"], tag, _token())
    work = Path(tempfile.mkdtemp(prefix=f"upstream-{name}-"))
    backup_root = Path(tempfile.mkdtemp(prefix=f"backup-{name}-"))
    changed = False
    had_dest = dest.exists()
    backup = backup_root / "tree" if had_dest else None
    try:
        if had_dest:
            shutil.copytree(dest, backup, symlinks=True)
        extracted = work / "chart"
        extract_chart(blob, target["path"], extracted)
        if not (extracted / "Chart.yaml").is_file():
            raise ChartError(f"upstream {target['path']} has no Chart.yaml")
        require_tag_matches_chart_version(
            target["repo"],
            tag,
            read_top_level_scalar(
                (extracted / "Chart.yaml").read_text(encoding="utf-8"), "version"
            ),
        )
        changed = True
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_chart(extracted, dest, list(target.get("exclude") or []))
        if had_dest:
            restore_keep(backup, dest, list(target.get("keep") or []))
        _apply_kept_chart_versions(dest, extracted, list(target.get("keep") or []))
        _apply_patches(dest, list(target.get("patches") or []))
        pairs = [(item["from"], item["to"]) for item in target.get("images") or []]
        _rewrite_tree(dest, pairs)
        images = gate_chart(
            dest,
            target.get("ci_args") or "",
            {item["to"] for item in target.get("images") or []},
            skip_image_check=skip_image_check,
        )
    except ImageMissing as exc:
        if changed:
            _restore(dest, backup, had_dest)
        _annotate(
            "warning",
            name,
            f"image missing on Docker Hub, chart left unchanged: {exc}",
        )
        _say(f"{name}: skipped (image missing) ({current} -> {tag})")
        return Result(
            name, current, tag, "skipped (image missing)", target["repo"], detail=str(exc)
        )
    except (ChartError, UnsafePath, OSError) as exc:
        if changed:
            try:
                _restore(dest, backup, had_dest)
            except OSError as restore_error:
                _annotate("error", name, f"{exc} (and restoring {dest} failed: {restore_error})")
                return Result(
                    name,
                    current,
                    tag,
                    "needs attention",
                    target["repo"],
                    detail=f"{exc}; restore failed: {restore_error}",
                )
        _annotate("error", name, str(exc))
        _say(f"{name}: needs attention ({current} -> {tag})")
        return Result(name, current, tag, "needs attention", target["repo"], detail=str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)

    _say(f"{name}: synced ({current} -> {tag})")
    return Result(name, current, tag, "synced", target["repo"], images=images)


def load_config(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("charts"), list):
        raise ChartError(f"{path} must be a mapping with a charts: list")
    targets = []
    seen = set()
    for index, raw in enumerate(data["charts"]):
        if not isinstance(raw, dict):
            raise ChartError(f"charts[{index}] is not a mapping")
        for key in ("name", "repo", "path", "dest"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ChartError(f"charts[{index}] missing {key}")
        name = raw["name"]
        if name in seen:
            raise ChartError(f"duplicate chart name {name}")
        seen.add(name)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", raw["repo"]):
            raise ChartError(f"{name}: repo must be owner/name, got {raw['repo']!r}")
        images = []
        for image in raw.get("images") or []:
            if not isinstance(image, dict) or not image.get("from") or not image.get("to"):
                raise ChartError(f"{name}: an image rewrite needs from and to")
            images.append({"from": str(image["from"]), "to": str(image["to"])})
        patches = []
        for patch in raw.get("patches") or []:
            if not isinstance(patch, dict):
                raise ChartError(f"{name}: patch is not a mapping")
            for key in ("file", "find", "replace"):
                if key not in patch:
                    raise ChartError(f"{name}: patch missing {key}")
            patches.append(
                {
                    "file": str(patch["file"]),
                    "find": str(patch["find"]),
                    "replace": str(patch["replace"]),
                    "regex": bool(patch.get("regex", False)),
                }
            )
        targets.append(
            {
                "name": name,
                "repo": raw["repo"],
                "path": raw["path"].strip("/"),
                "dest": raw["dest"].strip("/"),
                "images": images,
                "patches": patches,
                "keep": [str(item) for item in (raw.get("keep") or [])],
                "exclude": [str(item) for item in (raw.get("exclude") or [])],
                "ci_args": str(raw.get("ci_args") or ""),
            }
        )
    return targets


def render_pr_body(results: list[Result]) -> str:
    """Markdown for the pull request: each synced chart, old -> new, with the release."""
    synced = [result for result in results if result.result == "synced"]
    lines = [
        "## Upstream chart sync",
        "",
        "Copied from each upstream repository's latest stable GitHub Release.",
        "Drafts, releases marked prerelease, and tags with a pre-release suffix",
        "are not stable and were ignored. An equal or older upstream was left",
        "untouched.",
        "",
    ]
    if synced:
        lines.append("| Chart | Version | Upstream release | Images checked |")
        lines.append("| --- | --- | --- | --- |")
        for result in synced:
            quoted = urllib.parse.quote(result.upstream, safe="")
            url = f"https://github.com/{result.repo}/releases/tag/{quoted}"
            images = ", ".join(f"`{image}`" for image in result.images) or "—"
            lines.append(
                f"| {result.name} | {result.current} -> {result.upstream} "
                f"| [{result.upstream}]({url}) | {images} |"
            )
        lines.append("")
        lines.append(
            "Each image above was checked with `crane digest`. Only the Docker Hub"
            " mirrors this repository publishes (the configured `to` repositories)"
            " are checked by the sync; `hack/check-chart-images.sh` checks every"
            " rendered image of a chart whose version moved before this pull"
            " request is pushed."
        )
        lines.append("")
    else:
        behind = [result for result in results if result.result == "behind"]
        if behind:
            lines.append("Nothing was copied (`--check`). Charts behind a stable release:")
            lines.append("")
            for result in behind:
                lines.append(f"- {result.name}: {result.current} -> {result.upstream}")
            lines.append("")
        else:
            lines.append("No chart was behind a stable upstream release.")
            lines.append("")
    skipped = [result for result in results if result.result == "skipped (image missing)"]
    if skipped:
        lines.append("Left unchanged because a Docker Hub image is not published yet:")
        lines.append("")
        for result in skipped:
            lines.append(f"- {result.name} ({result.current} -> {result.upstream}): {result.detail}")
        lines.append("")
    attention = [result for result in results if result.result == "needs attention"]
    if attention:
        lines.append("Needs a human before it can be copied:")
        lines.append("")
        for result in attention:
            detail = " ".join(result.detail.split()) if result.detail else "see the log"
            lines.append(f"- {result.name}: {detail}")
        lines.append("")
    return "\n".join(lines)


def _write_github_output(results: list[Result]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    synced = " ".join(result.name for result in results if result.result == "synced")
    attention = " ".join(result.name for result in results if result.result == "needs attention")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"synced={synced}\n")
        handle.write(f"attention={attention}\n")


def _write_step_summary(results: list[Result]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "### Upstream chart sync",
        "",
        "| Chart | Current | Upstream stable | Result |",
        "| --- | --- | --- | --- |",
    ]
    for result in results:
        lines.append(
            f"| {result.name} | {result.current or '—'} | {result.upstream or '—'} | {result.result} |"
        )
    lines.append("")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def _emit(results: list[Result], pr_body: Path | None) -> None:
    _write_github_output(results)
    _write_step_summary(results)
    if pr_body is not None:
        pr_body.parent.mkdir(parents=True, exist_ok=True)
        pr_body.write_text(render_pr_body(results), encoding="utf-8")
    if not results:
        return
    synced = " ".join(result.name for result in results if result.result == "synced") or "(none)"
    attention = " ".join(result.name for result in results if result.result == "needs attention") or "(none)"
    skipped = " ".join(
        result.name for result in results if result.result == "skipped (image missing)"
    ) or "(none)"
    _say("")
    _say(f"synced: {synced}")
    _say(f"attention: {attention}")
    _say(f"skipped: {skipped}")


def _say(text: str) -> None:
    print(text, flush=True)


def _annotate(level: str, name: str, message: str) -> None:
    """A GitHub Actions annotation. The workflow log keeps the full text too."""
    one_line = " ".join(message.split())
    if len(one_line) > 500:
        one_line = one_line[:500] + "..."
    _say(f"::{level}::{name}: {one_line}")
    if "\n" in message.strip():
        _say(message.rstrip())


def git_toplevel() -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr.strip() or "not inside a git repository")
    return Path(completed.stdout.strip())


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync vendored charts from upstream stable GitHub Releases."
    )
    parser.add_argument(
        "--config",
        default="hack/sync-upstream-charts.yaml",
        help="path relative to the repo root (default: hack/sync-upstream-charts.yaml)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="only this chart (repeatable)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report which charts are behind; do not download or write the tree",
    )
    parser.add_argument(
        "--pr-body",
        metavar="FILE",
        help="write the pull request body markdown to FILE",
    )
    parser.add_argument(
        "--skip-image-check",
        action="store_true",
        help="do not run crane digest (local use, when the registry is unreachable)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass
    args = parse_args(argv)
    # Resolve the body path before chdir so a relative --pr-body follows the
    # caller's cwd. Chart paths are relative to the repo root, like the shell
    # scripts, which cd to the toplevel before they touch charts/.
    pr_body = Path(args.pr_body).resolve() if args.pr_body else None
    os.chdir(git_toplevel())
    try:
        targets = load_config(Path(args.config))
    except (OSError, ChartError, yaml.YAMLError) as exc:
        _say(f"::error::config: {exc}")
        return 1
    if args.only:
        known = {target["name"] for target in targets}
        unknown = [name for name in args.only if name not in known]
        if unknown:
            _say(f"::error::unknown chart(s): {', '.join(unknown)}")
            return 1
        wanted = set(args.only)
        targets = [target for target in targets if target["name"] in wanted]

    results: list[Result] = []
    try:
        for target in targets:
            try:
                results.append(
                    sync_target(
                        target,
                        check=args.check,
                        skip_image_check=args.skip_image_check,
                    )
                )
            except Exception as exc:
                # One chart's failure must not skip the others: the workflow
                # opens a pull request for whatever did sync.
                _annotate("error", target["name"], str(exc))
                results.append(
                    Result(
                        target["name"],
                        "",
                        "",
                        "needs attention",
                        target.get("repo", ""),
                        detail=str(exc),
                    )
                )
    finally:
        _emit(results, pr_body)
    if any(result.result == "needs attention" for result in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
