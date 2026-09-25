#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT
"""tools/check.py held to what it promises, on a throwaway catalog.

A git repository with the catalog's tools and a stand-in for the OpenGlow
extension key; real archives packed and signed with ffx; and the built
extension host. Each case changes the catalog as a pull request would and
runs the check against the commit before it:

- a new package's first version, its record as `ffx index record` wrote it: passes;
- the same with the record altered, with a range wider than the manifest's, with a record signed by
  another key: fails;
- a changed record: a narrower range passes, a wider one and any other change fail;
- a changed or removed key.pub fails;
- a version withdrawn, and a package withdrawn whole: pass, and a withdrawal still listing a version fails;
- OpenGlow's own package, verified against keys/forgefirm-ext.pub: passes;
- --no-fetch with no archive at hand checks no new record, and says so;
- a host that does not keep the index fails the check.

    FORGEEXT=build/forgeext FFX=../forgeext/tools/ffx FWUP=fwup python3 tests/check_test.py
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


def check(cond, what):
    print("  %s  %s" % ("ok  " if cond else "FAIL", what), flush=True)
    if not cond:
        failures.append(what)


def run(*args, cwd=None, env=None):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, env=env)


def main():
    if not (FORGEEXT and FFX and FWUP and os.path.isfile(FORGEEXT) and os.path.isfile(FFX)):
        print("skipped: needs FORGEEXT (a built forgeext), FFX (its tools/ffx), and fwup")
        return 77
    top = tempfile.mkdtemp(prefix="catalog-check-test.")
    env = dict(os.environ, FWUP=FWUP)
    try:
        cat = os.path.join(top, "catalog")
        shutil.copytree(TREE, cat, ignore=shutil.ignore_patterns(".git", ".archives", "__pycache__", "tests"))
        for k in ("standin", "author", "stranger"):
            p = run(sys.executable, "-B", FFX, "keygen", os.path.join(top, k), env=env)
            assert p.returncode == 0, p.stderr
        shutil.copyfile(os.path.join(top, "standin.pub"), os.path.join(cat, "keys", "forgefirm-ext.pub"))
        g = lambda *a: run("git", "-c", "user.name=t", "-c", "user.email=t@t", *a, cwd=cat)  # noqa: E731
        g("init", "-q")
        g("add", "-A")
        g("commit", "-q", "-m", "base")
        archives = os.path.join(top, "archives")
        os.makedirs(archives)

        def package(id_, version, key, core=None, caps=("machine.read",)):
            d = os.path.join(top, "src-%s-%s-%s" % (id_, version, key))
            m = {"manifest": 1, "id": id_, "name": id_.split(".")[-1], "version": version, "author": "Test",
                 "license": "MIT", "api": "0.1", "runtime": "shell", "service": {"exec": "bin/run.sh"},
                 "capabilities": list(caps)}
            if core:
                m["core"] = core
            os.makedirs(os.path.join(d, "bin"))
            with open(os.path.join(d, "manifest.json"), "w") as f:
                f.write(json.dumps(m, indent=1) + "\n")
            with open(os.path.join(d, "bin", "run.sh"), "w") as f:
                f.write("#!/bin/sh\nexec sleep 3600\n")
            os.chmod(os.path.join(d, "bin", "run.sh"), 0o755)
            out = os.path.join(top, "%s-%s-%s.ffx" % (id_, version, key))
            p = run(sys.executable, "-B", FFX, "pack", d, "--key", os.path.join(top, key + ".priv"), "--out", out, env=env)
            assert p.returncode == 0, p.stdout + p.stderr
            return out

        def record(ffx_file, id_, version, key=None, official=False):
            """Write the record as ffx index record writes it, and put the archive where the check finds it."""
            url = "https://example.org/%s-%s.ffx" % (id_, version)
            signer = ["--official-key", os.path.join(cat, "keys", "forgefirm-ext.pub")] if official else \
                ["--key", os.path.join(top, key + ".pub")]
            pdir = os.path.join(cat, "packages", id_)
            os.makedirs(pdir, exist_ok=True)
            out = os.path.join(pdir, version + ".json")
            p = run(sys.executable, "-B", FFX, "index", "record", ffx_file, "--url", url, "--out", out, *signer, env=env)
            assert p.returncode == 0, p.stdout + p.stderr
            rec = json.load(open(out))
            shutil.copyfile(ffx_file, os.path.join(archives, rec["sha256"] + ".ffx"))
            return out

        def checked(*more, host=FORGEEXT):
            base = g("rev-parse", "HEAD").stdout.strip()
            p = run(sys.executable, "-B", os.path.join(cat, "tools", "check.py"), "--base", base, "--ffx", FFX,
                    "--host", host, "--archives", archives, "--fwup", FWUP, *more, cwd=cat, env=env)
            return p.returncode, p.stdout + p.stderr

        def commit(what):
            g("add", "-A")
            g("commit", "-q", "-m", what)

        def put(path, obj):
            with open(path, "w") as f:
                json.dump(obj, f)

        A = "io.example.alpha"
        pdir = os.path.join(cat, "packages", A)
        rc, said = checked()
        check(rc == 0 and "the catalog's form" in said, "an empty catalog: %s" % said.strip().splitlines()[-1])

        # A new package's first version.
        a1 = package(A, "1.0.0", "author", core={"min": "0.0.7"})
        r1 = record(a1, A, "1.0.0", key="author")
        shutil.copyfile(os.path.join(top, "author.pub"), os.path.join(pdir, "key.pub"))
        rc, said = checked()
        check(rc == 0 and "the record of its archive" in said and "kept by" in said, "a new package's first version passes: %s"
              % said.strip().splitlines()[-1])
        rec = json.load(open(r1))
        for what, bad, words in (("an altered record", dict(rec, capabilities=["machine.read", "events"]), "not the record"),
                                 ("a wider range", dict(rec, core={"min": "0.0.6"}), "wider than its manifest"),
                                 ("no range at all, wider than the manifest's", {k: v for k, v in rec.items() if k != "core"},
                                  "wider than its manifest")):
            put(r1, bad)
            rc, said = checked()
            check(rc == 1 and words in said, "%s fails: %s" % (what, [ln for ln in said.splitlines() if "FAIL" in ln][:1]))
        put(r1, dict(rec, core={"min": "0.0.8"}))
        rc, said = checked()
        check(rc == 0, "a new record may narrow its range: %s" % said.strip().splitlines()[-1])
        put(r1, rec)
        # signed by another key than the package's
        s1 = package(A, "1.0.0", "stranger", core={"min": "0.0.7"})
        shutil.copyfile(s1, os.path.join(archives, rec["sha256"] + ".ffx"))
        p = run(sys.executable, "-B", FFX, "index", "record", s1, "--url", rec["url"], "--key",
                os.path.join(top, "stranger.pub"), env=env)
        srec = json.loads(p.stdout)
        put(r1, srec)
        shutil.copyfile(s1, os.path.join(archives, srec["sha256"] + ".ffx"))
        rc, said = checked()
        check(rc == 1 and "is not signed with" in said, "an archive signed by another key than key.pub fails")
        put(r1, rec)
        shutil.copyfile(a1, os.path.join(archives, rec["sha256"] + ".ffx"))
        commit("alpha 1.0.0")

        # Changed records and keys.
        put(r1, dict(rec, core={"min": "0.0.8"}))
        rc, said = checked()
        check(rc == 0 and "narrowed" in said, "a listed record's range narrowed passes")
        put(r1, dict(rec, core={"min": "0.0.6"}))
        rc, said = checked()
        check(rc == 1 and "wider than the listed" in said, "a listed record's range widened fails")
        put(r1, dict(rec, url="https://example.org/elsewhere.ffx"))
        rc, said = checked()
        check(rc == 1 and "changes its core range alone" in said, "a listed record's address changed fails")
        put(r1, rec)
        shutil.copyfile(os.path.join(top, "stranger.pub"), os.path.join(pdir, "key.pub"))
        rc, said = checked()
        check(rc == 1 and "key never changes" in said, "a changed key.pub fails")
        os.remove(os.path.join(pdir, "key.pub"))
        rc, said = checked()
        check(rc == 1 and "key never changes" in said, "a removed key.pub fails")
        shutil.copyfile(os.path.join(top, "author.pub"), os.path.join(pdir, "key.pub"))

        # A second version, then the first withdrawn, then the package withdrawn whole.
        a2 = package(A, "1.1.0", "author")
        record(a2, A, "1.1.0", key="author")
        commit("alpha 1.1.0")
        os.remove(r1)
        put(os.path.join(pdir, "withdrawn.json"), {"versions": {"1.0.0": "a flaw"}})
        rc, said = checked()
        check(rc == 0, "a version withdrawn passes: %s" % said.strip().splitlines()[-1])
        commit("alpha 1.0.0 withdrawn")
        put(os.path.join(pdir, "withdrawn.json"), {"reason": "its author asked"})
        rc, said = checked()
        check(rc == 1 and "withdrawn whole and still lists 1.1.0" in said, "a package withdrawn whole that still lists a version fails")
        os.remove(os.path.join(pdir, "1.1.0.json"))
        rc, said = checked()
        check(rc == 0, "a package withdrawn whole passes: %s" % said.strip().splitlines()[-1])
        commit("alpha withdrawn")

        # OpenGlow's own, against keys/forgefirm-ext.pub.
        o1 = package("org.openglow.probe", "1.0.0", "standin")
        record(o1, "org.openglow.probe", "1.0.0", official=True)
        rc, said = checked()
        check(rc == 0 and "org.openglow.probe/1.0.0.json: the record of its archive" in said,
              "OpenGlow's own package passes against the extension key")

        # --no-fetch with no archive at hand, and a host that does not keep the index.
        rec_o = json.load(open(os.path.join(cat, "packages", "org.openglow.probe", "1.0.0.json")))
        os.remove(os.path.join(archives, rec_o["sha256"] + ".ffx"))
        rc, said = checked("--no-fetch")
        check(rc == 0 and "skip" in said and "not fetched" in said, "--no-fetch checks no new record, and says so")
        liar = os.path.join(top, "old-host")
        with open(liar, "w") as f:
            f.write('#!/bin/sh\necho \'{"ok": false, "error": "an index is {\\"index\\": 1}"}\'\nexit 1\n')
        os.chmod(liar, 0o755)
        rc, said = checked("--no-fetch", host=liar)
        check(rc == 1 and "not kept by" in said, "a host that does not keep the index fails the check")
    finally:
        shutil.rmtree(top, ignore_errors=True)
    print("%s: check_test, %d failure%s" % ("FAIL" if failures else "PASS", len(failures), "" if len(failures) == 1 else "s"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
