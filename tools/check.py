#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
"""The catalog's check: what every pull request is held to, and what anyone can run.

    tools/check.py --base REV --ffx FORGEEXT/tools/ffx --host FORGEEXT_BIN [--host ...]
                   [--archives DIR] [--no-fetch] [--fwup FWUP]

Against the base revision (the branch a pull request asks to change):

- the catalog's form, as every machine keeps it: `ffx index build` of the tree;
- the keys: a package's key.pub never changes and is never removed;
- each new record: its archive, fetched once from its address and kept in --archives by its SHA-256, gives the
  same record under `ffx index record` (verified against the package's key, and judged as `ffx lint` judges it),
  except for a core range the record narrows;
- each changed record: only its core range changes, and only to a narrower one;
- the index built from the tree, signed with a throwaway key, kept by every --host (the extension host of each
  firmware release still supported).

--no-fetch fetches no archive and checks no new record: a push to main, whose records were checked in their pull
request. Exits 0 when everything holds, 1 when anything does not.
"""
import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

# The catalog is the git tree the check runs in, wherever the check itself is.
ROOT = OFFICIAL_KEY = None
OFFICIAL_NS = ("org.openglow.", "org.forgefirm.")
ARCHIVE_MAX = 32 << 20
failures = []


def fail(what):
    print("FAIL  " + what, flush=True)
    failures.append(what)


def ok(what):
    print("ok    " + what, flush=True)


def load_ffx(path):
    loader = importlib.machinery.SourceFileLoader("ffx_tool", path)
    spec = importlib.util.spec_from_loader("ffx_tool", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def git(*args):
    return subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, text=True)


def changes(base):
    """(status, path) of every change under packages/ from the base to the files as they stand, committed or
    not: A added, M changed, D removed."""
    p = git("ls-tree", "-r", "--name-only", base, "--", "packages/")
    if p.returncode != 0:
        raise SystemExit("git ls-tree %s failed: %s" % (base, p.stderr.strip()))
    was = set(p.stdout.splitlines())
    now = set()
    for d, _dirs, files in os.walk(os.path.join(ROOT, "packages")):
        for f in files:
            now.add(os.path.relpath(os.path.join(d, f), ROOT).replace(os.sep, "/"))
    out = [("A", x) for x in sorted(now - was)] + [("D", x) for x in sorted(was - now)]
    for x in sorted(now & was):
        with open(os.path.join(ROOT, x), "rb") as f:
            if f.read() != git_bytes(base, x):
                out.append(("M", x))
    return out


def git_bytes(base, path):
    return subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (base, path)], capture_output=True).stdout


def at_base(base, path):
    p = git("show", "%s:%s" % (base, path))
    return p.stdout if p.returncode == 0 else None


def within(ffx, narrow, wide):
    """Is the range narrow inside wide: a minimum no lower, a maximum no higher."""
    narrow, wide = narrow or {}, wide or {}
    if "min" in wide and ("min" not in narrow or ffx.version_cmp(narrow["min"], wide["min"]) < 0):
        return False
    if "max" in wide and ("max" not in narrow or ffx.version_cmp(narrow["max"], wide["max"]) > 0):
        return False
    return True


def archive(rec, archives, fetch):
    """The archive a record names, from --archives by its SHA-256, or fetched once from its address."""
    path = os.path.join(archives, rec["sha256"] + ".ffx")
    if os.path.isfile(path):
        return path, None
    if not fetch:
        return None, "not fetched (--no-fetch)"
    url = rec.get("url", "")
    if not url.startswith("https://") or not isinstance(rec.get("size"), int) or not 0 < rec["size"] <= ARCHIVE_MAX:
        return None, "its address or its size is out of form"
    os.makedirs(archives, exist_ok=True)
    part = path + ".part"
    try:
        with urllib.request.urlopen(url, timeout=240) as r, open(part, "wb") as f:
            got = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                got += len(chunk)
                if got > rec["size"]:
                    raise ValueError("more than the %d bytes the record names" % rec["size"])
                f.write(chunk)
        data = open(part, "rb").read()
        if len(data) != rec["size"] or hashlib.sha256(data).hexdigest() != rec["sha256"]:
            raise ValueError("its size or its SHA-256 is not the record's")
        os.replace(part, path)
        return path, None
    except (OSError, ValueError) as e:
        if os.path.exists(part):
            os.remove(part)
        return None, "the archive at %s: %s" % (url, e)


def check_new(ffx_path, ffx, path, archives, fetch, env):
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    rec = json.load(open(path, encoding="utf-8"))
    pid = os.path.basename(os.path.dirname(path))
    got, why = archive(rec, archives, fetch)
    if not got:
        if fetch:
            fail("%s: %s" % (rel, why))
        else:
            print("skip  %s: %s" % (rel, why))
        return
    signer = ["--official-key", OFFICIAL_KEY] if pid.startswith(OFFICIAL_NS) else \
        ["--key", os.path.join(os.path.dirname(path), "key.pub")]
    p = subprocess.run([sys.executable, "-B", ffx_path, "index", "record", got, "--url", rec.get("url", "")] + signer,
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        fail("%s: %s" % (rel, (p.stderr or p.stdout).strip()))
        return
    mine = json.loads(p.stdout)
    theirs = {k: v for k, v in rec.items() if k != "core"}
    ours = {k: v for k, v in mine.items() if k != "core"}
    if ours != theirs:
        fail("%s is not the record of its archive: %s differ" % (rel, ", ".join(sorted(
            k for k in set(ours) | set(theirs) if ours.get(k) != theirs.get(k)))))
    elif not within(ffx, rec.get("core"), mine.get("core")):
        fail("%s: its core range %s is wider than its manifest's %s" % (rel, rec.get("core"), mine.get("core")))
    else:
        ok("%s: the record of its archive, signed with its key" % rel)


def check_changed(ffx, base, path):
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    was = json.loads(at_base(base, rel) or "{}")
    now = json.load(open(path, encoding="utf-8"))
    if {k: v for k, v in was.items() if k != "core"} != {k: v for k, v in now.items() if k != "core"}:
        fail("%s: a listed record changes its core range alone" % rel)
    elif not within(ffx, now.get("core"), was.get("core")):
        fail("%s: its core range %s is wider than the listed %s" % (rel, now.get("core"), was.get("core")))
    else:
        ok("%s: its core range narrowed" % rel)


def main(argv=None):
    ap = argparse.ArgumentParser(description="the catalog's check")
    ap.add_argument("--base", required=True, help="the revision the tree is judged against")
    ap.add_argument("--ffx", required=True, help="forgeext's tools/ffx")
    ap.add_argument("--host", action="append", default=[], help="a built forgeext that must keep the index (repeat)")
    ap.add_argument("--archives", help="archives by SHA-256, fetched once (default: .archives in the tree)")
    ap.add_argument("--no-fetch", action="store_true", help="fetch no archive, and check no new record")
    ap.add_argument("--fwup", default=os.environ.get("FWUP") or shutil.which("fwup"))
    a = ap.parse_args(argv)
    global ROOT, OFFICIAL_KEY
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        raise SystemExit("run it inside the catalog's git tree")
    ROOT = top.stdout.strip()
    OFFICIAL_KEY = os.path.join(ROOT, "keys", "forgefirm-ext.pub")
    if a.archives is None:
        a.archives = os.path.join(ROOT, ".archives")
    if not a.fwup:
        raise SystemExit("fwup is not found: install it, or give --fwup")
    if not a.host:
        raise SystemExit("no --host: every index is held to being kept by the extension host of each supported release")
    env = dict(os.environ, FWUP=a.fwup)
    ffx = load_ffx(a.ffx)
    work = tempfile.mkdtemp(prefix="catalog-check-")
    try:
        # The keys, and what changed.
        todo = changes(a.base)
        for status, rel in todo:
            if os.path.basename(rel) == "key.pub" and status in ("M", "D"):
                fail("%s: a package's key never changes, and is never removed" % rel)
        for status, rel in todo:
            name, full = os.path.basename(rel), os.path.join(ROOT, rel)
            if name in ("key.pub", "withdrawn.json") or not name.endswith(".json") or status == "D":
                continue
            if status == "A":
                check_new(a.ffx, ffx, full, a.archives, not a.no_fetch, env)
            elif status == "M":
                check_changed(ffx, a.base, full)

        # The form, and every supported host keeping the index built from the tree.
        key = os.path.join(work, "throwaway")
        p = subprocess.run([sys.executable, "-B", a.ffx, "keygen", key], capture_output=True, text=True, env=env)
        if p.returncode != 0:
            raise SystemExit("ffx keygen: %s" % p.stderr.strip())
        idx = os.path.join(work, "index-1.ffi")
        p = subprocess.run([sys.executable, "-B", a.ffx, "index", "build", ROOT, "--version", "1.0.0", "--key",
                            key + ".priv", "--out", idx], capture_output=True, text=True, env=env)
        if p.returncode != 0:
            fail("the catalog's form: %s" % (p.stderr or p.stdout).strip())
        else:
            ok("the catalog's form: %s" % p.stdout.strip().split(": ", 1)[-1])
            for n, host in enumerate(a.host):
                root = os.path.join(work, "root-%d" % n)
                os.makedirs(root)
                q = subprocess.run([host, "--root", root, "--fwup", a.fwup, "--official-key", key + ".pub",
                                    "--no-reserve", "index-verify", idx], capture_output=True, text=True)
                try:
                    r = json.loads(q.stdout)
                except ValueError:
                    r = {"ok": False, "error": (q.stdout + q.stderr).strip()[:300]}
                if r.get("ok") is True:
                    ok("kept by %s" % host)
                else:
                    fail("not kept by %s: %s" % (host, r.get("error")))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%s: %d failure%s" % ("FAIL" if failures else "PASS", len(failures), "" if len(failures) == 1 else "s"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
