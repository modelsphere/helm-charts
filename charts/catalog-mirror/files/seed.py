#!/usr/bin/env python3
"""Put a packed copy of the mirror on the volume, before nginx serves it.

For a cluster that cannot reach upstream at all: the copy is made where upstream
is reachable (an air-gapped install bundle, say), packed as a gzip'd tar holding
the two trees nginx serves -- model-catalog/ and helm-charts/ -- and handed over
in a ConfigMap. Nothing else may be in the archive.

Same rules as sync.py, so a pod that restarts halfway never serves a torn copy:
  - every file goes in by atomic rename, and the indexes (index.json,
    index-v2.json, index.yaml) only after everything they name is there;
  - files are never deleted, so a seed older than what sync already fetched
    only rewinds the indexes, and the next sync round moves them forward again.

status.json says the copy came from a seed, in the shape verify.py reads.

  python3 seed.py <archive>
"""
import json, os, sys, tarfile
from datetime import datetime, timezone

DATA = os.environ.get("DATA_DIR", "/data")
TREES = ("model-catalog", "helm-charts")
INDEXES = ("index.json", "index-v2.json", "index.yaml")


def write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def safe_rel(rel):
    p = os.path.normpath(rel)
    if os.path.isabs(rel) or p.startswith("..") or "/../" in f"/{p}/":
        raise ValueError(f"path {rel!r} escapes the mirror")
    if p.split("/")[0] not in TREES:
        raise ValueError(f"path {rel!r} is outside {' and '.join(TREES)}")
    return p


def main():
    archive = sys.argv[1]
    files, indexes = [], []
    with tarfile.open(archive, "r:gz") as t:
        for m in t.getmembers():
            if m.isdir():
                continue
            if not m.isfile():
                raise ValueError(f"{m.name}: only regular files may be in a seed")
            rel = safe_rel(m.name)
            entry = (rel, t.extractfile(m).read())
            (indexes if os.path.basename(rel) in INDEXES else files).append(entry)
    have = {rel.split("/")[0] for rel, _ in indexes}
    if have != set(TREES):
        raise ValueError(f"the seed must carry an index for both {' and '.join(TREES)}; it has {sorted(have) or 'none'}")
    for rel, data in files + indexes:
        write_atomic(os.path.join(DATA, rel), data)
    n = {tree: sum(rel.startswith(tree + "/") for rel, _ in files + indexes) for tree in TREES}
    status = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "seed",
        "catalog": {"ok": True, "detail": f"seeded: {n['model-catalog']} files"},
        "charts": {"ok": True, "detail": f"seeded: {n['helm-charts']} files"},
    }
    write_atomic(os.path.join(DATA, "status.json"), json.dumps(status, indent=1).encode())
    print(f"seed: {n['model-catalog']} catalog files, {n['helm-charts']} chart files -> {DATA}", flush=True)


if __name__ == "__main__":
    main()
