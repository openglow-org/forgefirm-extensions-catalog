#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
"""tools/publish.py held to what it promises, on a throwaway catalog with a stand-in for gh.

The stand-in answers the releases API from a state file and records every release it is asked to create. A
stand-in key plays the OpenGlow extension key (keys/forgefirm-ext.pub is its public half), and the built extension
host is the host that must keep the index:

- no release yet: the index is published as vYYYY.MMDD.1 of the day, and the asset is kept by the host;
- the same catalog again: nothing is published;
- a changed catalog the same day: .2; the next day: .1 of that day;
- a day before the latest release's: refused, nothing published;
- a key that is not keys/forgefirm-ext.pub: the host does not keep the index, nothing published;
- a key with the whitespace a secret may carry: published;
- the releases API failing: refused;
- --dry-run: nothing published.

    FORGEEXT=build/forgeext FFX=../forgeext/tools/ffx FWUP=fwup python3 tests/publish_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.path.dirname(HERE)
FORGEEXT = os.environ.get("FORGEEXT")
FFX = os.environ.get("FFX")
FWUP = os.environ.get("FWUP") or shutil.which("fwup")
failures = []

STAND_IN_GH = r'''#!/usr/bin/env python3
import json, os, shutil, sys
state_file = os.environ["GH_STATE"]
st = json.load(open(state_file))
a = sys.argv[1:]
if st.get("fail"):
    print("gh: Server Error (HTTP 502)", file=sys.stderr); sys.exit(1)
if a[:1] == ["api"] and a[-1].endswith("/releases/latest"):
    rel = [r for r in st["releases"] if r.get("latest")]
    if not rel:
        print("gh: Not Found (HTTP 404)", file=sys.stderr); sys.exit(1)
    print(json.dumps(rel[-1])); sys.exit(0)
if a[:1] == ["api"] and "--jq" in a:
    for r in st["releases"]:
        print(r["tag_name"])
    sys.exit(0)
if a[:2] == ["release", "create"]:
    tag, asset = a[2], a[3]
    notes = a[a.index("--notes") + 1]
    keep = os.path.join(os.path.dirname(state_file), tag + "-" + os.path.basename(asset))
    shutil.copyfile(asset, keep)
    for r in st["releases"]:
        r["latest"] = False
    st["releases"].append({"tag_name": tag, "body": notes, "latest": "--latest" in a, "asset": keep, "args": a})
    json.dump(st, open(state_file, "w")); sys.exit(0)
print("unexpected gh %s" % a, file=sys.stderr); sys.exit(9)
'''


def check(cond, what):
    print("  %s  %s" % ("ok  " if cond else "FAIL", what), flush=True)
    if not cond:
        failures.append(what)


def main():
    if not (FORGEEXT and FFX and FWUP and os.path.isfile(FORGEEXT) and os.path.isfile(FFX)):
        print("skipped: needs FORGEEXT (a built forgeext), FFX (its tools/ffx), and fwup")
        return 77
    top = tempfile.mkdtemp(prefix="pubtest.")
    try:
        bindir = os.path.join(top, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "gh"), "w") as f:
            f.write(STAND_IN_GH)
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        state = os.path.join(top, "gh-state.json")
        env = dict(os.environ, FWUP=FWUP, GH_STATE=state, PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        for k in ("standin", "other"):
            assert subprocess.run([sys.executable, "-B", FFX, "keygen", os.path.join(top, k)], env=env,
                                  capture_output=True).returncode == 0
        cat = os.path.join(top, "catalog")
        shutil.copytree(TREE, cat, ignore=shutil.ignore_patterns(".git", ".archives", "__pycache__"))
        shutil.copyfile(os.path.join(top, "standin.pub"), os.path.join(cat, "keys", "forgefirm-ext.pub"))
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t"] + list(a), cwd=cat,  # noqa: E731
                                      capture_output=True, text=True)
        g("init", "-q")
        g("add", "-A")
        g("commit", "-q", "-m", "base")

        def put_state(releases, fail=False):
            json.dump({"releases": releases, "fail": fail}, open(state, "w"))

        def releases():
            return json.load(open(state))["releases"]

        def publish(*more, key="standin", keyfile=None):
            p = subprocess.run([sys.executable, "-B", os.path.join(cat, "tools", "publish.py"), "--ffx", FFX, "--host",
                                FORGEEXT, "--key", keyfile or os.path.join(top, key + ".priv"), "--fwup", FWUP] + list(more),
                               cwd=cat, env=env, capture_output=True, text=True)
            return p.returncode, (p.stdout + p.stderr).strip()

        def change(name):
            d = os.path.join(cat, "packages", "org.openglow." + name)
            os.makedirs(d)
            with open(os.path.join(d, "withdrawn.json"), "w") as f:
                json.dump({"reason": "a change to the catalog"}, f)
            g("add", "-A")
            g("commit", "-q", "-m", name)

        put_state([])
        rc, said = publish("--today", "2026-09-25")
        rel = releases()
        check(rc == 0 and [r["tag_name"] for r in rel] == ["v2026.925.1"] and rel[0]["latest"]
              and "index.json sha256: " in rel[0]["body"] and rel[0]["args"][3].endswith("index-1.ffi"),
              "no release yet: v2026.925.1, index-1.ffi, marked latest: %s" % said.splitlines()[-1:])
        root = os.path.join(top, "machine")
        p = subprocess.run([FORGEEXT, "--root", root, "--fwup", FWUP, "--official-key", os.path.join(top, "standin.pub"),
                            "--no-reserve", "index-verify", rel[0]["asset"]], capture_output=True, text=True)
        check(json.loads(p.stdout).get("version") == "2026.925.1", "a machine keeps the published asset: %s" % p.stdout.strip())

        rc, said = publish("--today", "2026-09-25")
        check(rc == 0 and "unchanged" in said and len(releases()) == 1, "the same catalog again: nothing published")

        change("gone1")
        rc, said = publish("--today", "2026-09-25")
        check(rc == 0 and [r["tag_name"] for r in releases()][-1] == "v2026.925.2" and releases()[0]["latest"] is False,
              "a changed catalog the same day: v2026.925.2, now the latest: %s" % said.splitlines()[-1:])
        p = subprocess.run([FORGEEXT, "--root", root, "--fwup", FWUP, "--official-key", os.path.join(top, "standin.pub"),
                            "--no-reserve", "index-verify", releases()[-1]["asset"]], capture_output=True, text=True)
        check(json.loads(p.stdout).get("ok") is True, "and the machine keeps it over the first")

        change("gone2")
        rc, said = publish("--today", "2026-09-24")
        check(rc == 1 and "not above the latest release's, v2026.925.2" in said and len(releases()) == 2,
              "a day before the latest release's: refused, nothing published")
        rc, said = publish("--today", "2026-09-26", key="other")
        check(rc == 1 and "does not keep it" in said and "not signed with the OpenGlow extension key" in said
              and len(releases()) == 2, "a key that is not keys/forgefirm-ext.pub: refused, nothing published")
        rc, said = publish("--today", "2026-09-26", "--dry-run")
        check(rc == 0 and "dry run" in said and len(releases()) == 2, "--dry-run: nothing published")
        spaced = os.path.join(top, "spaced.priv")
        with open(spaced, "w") as f:
            f.write("\n  " + open(os.path.join(top, "standin.priv")).read().strip() + "  \n\n")
        rc, said = publish("--today", "2026-09-26", keyfile=spaced)
        check(rc == 0 and releases()[-1]["tag_name"] == "v2026.926.1",
              "the next day, with a key carrying a secret's whitespace: v2026.926.1: %s" % said.splitlines()[-1:])
        change("gone3")
        put_state(releases(), fail=True)
        rc, said = publish("--today", "2026-09-26")
        check(rc == 1 and "the releases API" in said, "the releases API failing: refused")
        check(not [n for n in os.listdir(tempfile.gettempdir()) if n.startswith("catalog-publish-")],
              "every run removed its work directory, and the key in it")
    finally:
        shutil.rmtree(top, ignore_errors=True)
    print("%s: publish_test, %d failure%s" % ("FAIL" if failures else "PASS", len(failures), "" if len(failures) == 1 else "s"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
