#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
"""The catalog's publish step. The workflow runs it on main, after the check, with the OpenGlow extension key
from the signing environment's secret; it runs on no pull request.

    tools/publish.py --ffx FORGEEXT/tools/ffx --host FORGEEXT_BIN [--host ...] --key KEY.priv
                     [--repo OWNER/NAME] [--today YYYY-MM-DD] [--dry-run] [--fwup FWUP]

1. Builds the index of the tree. When its index.json is the latest release's - by the SHA-256 that release's notes
   carry, read through the releases API, which counts no download - it publishes nothing.
2. Otherwise it takes the next version, YYYY.MMDD.N of the day in UTC, which must be above the latest release's: a
   machine refuses an index older than the one it keeps.
3. Signs the index with KEY, and has every host keep it under keys/forgefirm-ext.pub, which proves KEY is the key
   every machine trusts.
4. Publishes it as the index-1.ffi asset of release vVERSION, marked latest (gh, with the workflow's token).

Exits 0 when it published or had nothing to publish, 1 when it refused.
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

SHA_NOTE = re.compile(r"index\.json sha256: ([0-9a-f]{64})")


def gh(*args):
    return subprocess.run(["gh"] + list(args), capture_output=True, text=True)


def refuse(why):
    print("refused: " + why, file=sys.stderr)
    return 1


def build(ffx, root, version, out, env, key=None):
    p = subprocess.run([sys.executable, "-B", ffx, "index", "build", root, "--version", version, "--out", out]
                       + (["--key", key] if key else []), capture_output=True, text=True, env=env)
    m = re.search(r"index\.json sha256 ([0-9a-f]{64})", p.stdout)
    if p.returncode != 0 or not m:
        raise SystemExit("ffx index build: %s" % (p.stderr or p.stdout).strip())
    return m.group(1)


def vnum(tag):
    m = re.fullmatch(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag or "")
    return [int(x) for x in m.groups()] if m else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="publish the catalog's signed index")
    ap.add_argument("--ffx", required=True)
    ap.add_argument("--host", action="append", default=[])
    ap.add_argument("--key", required=True, help="the OpenGlow extension key's private half, as fwup -g writes it")
    ap.add_argument("--repo", default="openglow-org/forgefirm-extensions-catalog")
    ap.add_argument("--today", help="YYYY-MM-DD, for a test; the day in UTC otherwise")
    ap.add_argument("--dry-run", action="store_true", help="everything but the release")
    ap.add_argument("--fwup", default=os.environ.get("FWUP") or shutil.which("fwup"))
    a = ap.parse_args(argv)
    if not a.fwup:
        return refuse("fwup is not found: install it, or give --fwup")
    if not a.host:
        return refuse("no --host: the index is kept by the extension host of each supported release before it is published")
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        return refuse("run it inside the catalog's git tree")
    root = top.stdout.strip()
    official = os.path.join(root, "keys", "forgefirm-ext.pub")
    env = dict(os.environ, FWUP=a.fwup)
    work = tempfile.mkdtemp(prefix="catalog-publish-")
    try:
        os.chmod(work, 0o700)

        # What is published now: the latest release, and every release's tag.
        p = gh("api", "repos/%s/releases/latest" % a.repo)
        if p.returncode == 0:
            latest = json.loads(p.stdout)
        elif "404" in p.stderr or "Not Found" in p.stderr:
            latest = None
        else:
            return refuse("the releases API: %s" % p.stderr.strip())
        p = gh("api", "--paginate", "repos/%s/releases?per_page=100" % a.repo, "--jq", ".[].tag_name")
        if p.returncode != 0:
            return refuse("the releases API: %s" % p.stderr.strip())
        tags = p.stdout.split()

        # Nothing new: the index.json the latest release carries.
        sha = build(a.ffx, root, "0.0.1", os.path.join(work, "probe.ffi"), env)
        m = SHA_NOTE.search((latest or {}).get("body") or "")
        if latest and m and m.group(1) == sha:
            print("unchanged: %s already carries index.json %s; nothing published" % (latest.get("tag_name"), sha))
            return 0

        # The next version of the day, above the latest release's.
        day = datetime.date.fromisoformat(a.today) if a.today else datetime.datetime.now(datetime.timezone.utc).date()
        prefix = "%d.%d" % (day.year, day.month * 100 + day.day)
        n = 1 + max([vnum(t)[2] for t in tags if vnum(t) and "%d.%d" % tuple(vnum(t)[:2]) == prefix] or [0])
        version = "%s.%d" % (prefix, n)
        if latest and vnum(latest.get("tag_name")) and vnum(version) <= vnum(latest["tag_name"]):
            return refuse("the version %s is not above the latest release's, %s" % (version, latest["tag_name"]))

        # Signed with the key as fwup wrote it, whatever whitespace the secret carried.
        key = os.path.join(work, "key.priv")
        with open(a.key, encoding="ascii") as f:
            text = "".join(f.read().split())
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text + "\n")
        idx = os.path.join(work, "index-1.ffi")
        if build(a.ffx, root, version, idx, env, key) != sha:
            return refuse("the signed index is not the one just built")
        os.remove(key)

        # Kept by every supported host under the key every machine trusts.
        for i, host in enumerate(a.host):
            hroot = os.path.join(work, "root-%d" % i)
            q = subprocess.run([host, "--root", hroot, "--fwup", a.fwup, "--official-key", official, "--no-reserve",
                                "index-verify", idx], capture_output=True, text=True)
            try:
                r = json.loads(q.stdout)
            except ValueError:
                r = {"ok": False, "error": (q.stdout + q.stderr).strip()[:300]}
            if r.get("ok") is not True:
                return refuse("%s does not keep it: %s" % (host, r.get("error")))
            print("kept by %s" % host)

        rev = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        notes = "The signed index of the catalog at %s.\n\nindex.json sha256: %s\n" % (rev[:12], sha)
        print("index-1.ffi, version %s, index.json sha256 %s" % (version, sha))
        if a.dry_run:
            print("dry run: not published")
            return 0
        p = gh("release", "create", "v" + version, idx, "--repo", a.repo, "--target", rev, "--title",
               "Catalog " + version, "--notes", notes, "--latest")
        if p.returncode != 0:
            return refuse("gh release create: %s" % p.stderr.strip())
        print("published: v%s" % version)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
