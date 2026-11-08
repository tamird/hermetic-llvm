"""Qualify the current Linux C++ toolchain's actual module map and generator."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

SOURCE = "f9abea4c99d6d4a779488463419b2b0486497cbc"
PR899 = "eb198127bdc4f7b737d08a7ea009869bc7a72e75"
ORIGINAL = "2c43e79381c6d8a68b9d5246081075af5fe8e8aa"
UNWRAPPED = "e94223b2e54629401f246215a17ade340a8a79b4"
OWNER = "tools/internal/BUILD.bazel"
ROOT = Path.cwd()
CWD = ROOT / "e2e/rules_cc"
FIXTURE = ROOT / ".github/qualification/module-map-exec"
TMP = Path(os.environ["RUNNER_TEMP"]) / "module-map-exec"
BAZEL = [str(TMP / "bazel"), "--ignore_all_rc_files", "--host_jvm_args=-Xmx3500m"]
GNU = "@llvm//platforms:linux_x86_64_gnu.2.28"
PKG = "//module_map_exec:"
MAP = PKG + "real_map"
VIEW = PKG + "generator"
MUSL_TARGETS = 'filter("(/runtimes/musl:|musl_libc//)", deps(' + VIEW + '))'
REPORT = {"source": SOURCE, "carrier": os.environ["GITHUB_SHA"], "stock": ORIGINAL,
          "unwrapped": UNWRAPPED, "target_platform": GNU, "host_platform": GNU, "cells": {}}


def save():
    (TMP / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bazel(row, command, args):
    argv = [*BAZEL, command, *args]
    run = subprocess.run(argv, cwd=CWD, capture_output=True, text=True)
    if run.returncode:
        lines = run.stderr.splitlines()
        starts = [i for i,s in enumerate(lines) if re.search(r"ERROR:|error:|FATAL:", s)][:3]
        indexes = sorted({j for i in starts for j in range(i, min(i+7, len(lines)))} | set(range(max(0,len(lines)-5),len(lines))))
        row.setdefault("diagnostics", []).append({"command": command, "exit": run.returncode,
            "lines": [re.sub(r"https?://\S+", "<url>", lines[i])[:500] for i in indexes]})
        save()
        raise RuntimeError(f"{command} exit {run.returncode}")
    return run.stdout


def counts_graph(row, flags):
    labels = sorted(set(bazel(row, "cquery", [*flags, "--output=label", MUSL_TARGETS]).splitlines()))
    row["configured_musl_rows"] = len(labels)
    row["configured_musl_sha256"] = sha("\n".join(labels).encode())
    row["configured_musl_examples"] = [s for s in labels if "//runtimes/musl:" in s][:8]
    save()
    query = 'mnemonic("CppCompile|CppArchive|CppLink", ' + MUSL_TARGETS + ')'
    graph = json.loads(bazel(row, "aquery", [*flags, "--output=jsonproto", "--noinclude_artifacts",
                       "--noinclude_commandline", query]) or "{}")
    owners = {str(t["id"]): t["label"] for t in graph.get("targets", [])}
    rows = [(a["mnemonic"], owners[str(a["targetId"])]) for a in graph.get("actions", [])]
    row["musl_actions"] = dict(Counter(k for k,_ in rows))
    row["musl_compile_owners"] = sorted({v for k,v in rows if k == "CppCompile"})
    save()


def inspect_map(row, flags, worker):
    bep = TMP / (worker + ".bep")
    bazel(row, "build", [*flags, "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), MAP])
    completed = []
    emitted = Counter()
    for line in bep.open():
        event = json.loads(line)
        if "started" in event:
            row["invocation"] = event["started"]["uuid"]
        key = event["id"].get("actionCompleted", {})
        kind = event.get("action", {}).get("type")
        if kind in ("CppCompile", "CppArchive", "CppLink") and re.search(r"/runtimes/musl:|musl_libc//", key.get("label","")):
            emitted[kind] += 1
        label = key.get("label", "")
        if kind == "CppModuleMap" and "llvm-toolchain-minimal-linux-amd64" in label and label.endswith("//:module_map"):
            done = event["action"]
            if done.get("commandLine"):
                completed.append((key, done))
    row["whole_map_bep_musl_actions"] = dict(emitted)
    row["matching_map_actions"] = len(completed)
    save()
    (key, done), = completed
    assert done.get("success") is True
    row["original_action"] = {"owner": key["label"], "output": key["primaryOutput"],
                              "command_head": done["commandLine"][:2], "configuration": key["configuration"]["id"]}
    save()
    query = 'mnemonic("CppModuleMap", deps(' + MAP + ', 1))'
    graph = json.loads(bazel(row, "aquery", [*flags, "--output=jsonproto", "--noinclude_artifacts",
                                             "--include_commandline", query]))
    owners = {str(t["id"]): t["label"] for t in graph.get("targets", [])}
    action, = [a for a in graph["actions"] if a.get("arguments", []) and
               owners[str(a["targetId"])].endswith("//:module_map")]
    argv = action["arguments"]
    assert len(argv) >= 2 and argv[1].endswith(".modulemap"), argv[:3]
    owner = owners[str(action["targetId"])]
    platform = action["executionPlatform"]
    row["configured_action"] = {"owner": owner, "platform": platform, "program": argv[0], "output": argv[1],
                                "argument_count": len(argv), "argv_sha256": sha(json.dumps(argv).encode())}
    save()
    assert "llvm-toolchain-minimal-linux-amd64" in owner and platform.lstrip("@") == PKG + worker
    declared, = bazel(row, "cquery", [*flags, "--output=files", VIEW]).splitlines()
    assert argv[0] == declared, (argv[0], declared)
    assert key["primaryOutput"] == argv[1] and key["label"].lstrip("@") == owner.lstrip("@")
    orig_argv = done["commandLine"]
    assert orig_argv[:2] == argv[:2], (orig_argv[:2], argv[:2])
    row["action_command_sha256"] = sha(json.dumps(orig_argv).encode())
    output = done["primaryOutput"]
    url = urlsplit(output["uri"])
    assert url.scheme == "file" and not url.netloc
    location = Path(unquote(url.path))
    contents = location.read_bytes()
    assert contents and output.get("digest", sha(contents)) == sha(contents)
    assert int(output.get("length", len(contents))) == len(contents)
    root = Path(bazel(row, "info", ["execution_root"]).strip())
    assert location.samefile(root / argv[1])
    text = contents.decode()
    entries = re.findall(r'^    textual header "(.*)" \{ size (\d+) \}$', text, re.M)
    assert text.startswith('module "crosstool" [system] {\n') and text.endswith("}\n")
    assert len(entries) >= 2 and len(entries) == text.count("    textual header ")
    assert all((root / name).stat().st_size == int(size) for name, size in entries)
    by_name = {Path(name).name for name, _ in entries}
    assert {"stdio.h", "stddef.h"} <= by_name
    row["built_map"] = {"owner": key["label"], "configuration": key["configuration"]["id"], "file": argv[1],
                        "bytes": len(contents), "sha256": sha(contents), "header_sizes_verified": len(entries),
                        "examples": [(n,int(s)) for n,s in entries if Path(n).name in {"stdio.h","stddef.h"}][:8]}
    generator = root / declared
    elf = subprocess.check_output(["readelf", "-hlWd", generator], text=True)
    interpreter = re.search(r"Requesting program interpreter: (.*?)\]", elf)
    needed = re.findall(r"\(NEEDED\).*?\[(.*?)\]", elf)
    row["generator"] = {"path": declared, "bytes": generator.stat().st_size, "sha256": sha(generator.read_bytes()),
                        "interpreter": interpreter[1] if interpreter else None, "needed": needed}
    save()
    assert "Advanced Micro Devices X86-64" in elf
    assert (interpreter is None and not needed) if worker == "musl" else (
        interpreter and interpreter[1] == "/lib64/ld-linux-x86-64.so.2" and "libc.so.6" in needed)
    # Re-use this exact, action-bound ELF and one measured header in a
    # scratch root. The GNU action above already ran on the real hosted OS.
    chroot = TMP / ("root-" + worker)
    chroot.mkdir(mode=0o755)
    chroot.chmod(0o755)
    shutil.copyfile(generator, chroot / "generator")
    (chroot / "generator").chmod(0o555)
    path, size = next((n,s) for n,s in entries if Path(n).name == "stddef.h")
    shutil.copyfile(root / path, chroot / "header.h")
    (chroot / "params").write_text("/header.h\n")
    (chroot / "out").mkdir()
    (chroot / "out").chmod(0o777)
    assert not (chroot / "lib64").exists()
    proc = subprocess.run(["sudo", "-n", "/usr/sbin/chroot", "--userspec=+65534:+65534", str(chroot),
                           "/generator","/out/map","@/params"], capture_output=True, text=True)
    row["empty_root"] = {"exit": proc.returncode, "diagnostic": proc.stderr.strip()[:300],
                         "header_bytes": int(size)}
    save()
    if worker == "gnu":
        assert proc.returncode == 127 and "No such file or directory" in proc.stderr
    else:
        assert proc.returncode == 0 and f'textual header "/header.h" {{ size {size} }}' in (chroot / "out/map").read_text()


def cell(name, blob, worker, physical):
    row = REPORT["cells"][name] = {"worker": PKG + worker, "blob": blob, "verified": False}
    save()
    try:
        assert git("hash-object", OWNER) == blob
        flags = ["--lockfile_mode=off", "--remote_cache=", "--remote_executor=", "--bes_backend=", "--jobs=3",
                 "-c", "opt", "--repo_env=BAZEL_DO_NOT_DETECT_CPP_TOOLCHAIN=1", "--experimental_cc_static_library",
                 "--platforms=" + GNU, "--host_platform=" + GNU, "--extra_execution_platforms=" + PKG + worker]
        try:
            counts_graph(row, flags)
        except Exception as error:
            row["closure_error"] = f"{type(error).__name__}: {error}"[:350]
            save()
        if physical:
            inspect_map(row, flags, worker)
        assert "closure_error" not in row, row.get("closure_error")
        row["verified"] = True
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {error}"[:400]
    save()


def main():
    assert git("rev-parse", "HEAD^") == SOURCE and git("rev-parse", "HEAD") == REPORT["carrier"]
    changed = git("diff", "--name-only", SOURCE, "HEAD").splitlines()
    assert set(changed) == {".github/workflows/module-map-exec.yml"} | {
        ".github/qualification/module-map-exec/" + p for p in ["check.py","fixture.BUILD","view.bzl"]}
    assert git("rev-parse", PR899 + ":" + OWNER) == ORIGINAL == git("rev-parse", SOURCE + ":" + OWNER)
    assert git("rev-parse", PR899 + "^:" + OWNER) == UNWRAPPED
    assert git("diff", "--name-only", PR899 + "^", PR899) == OWNER
    REPORT["bazel"] = subprocess.check_output([BAZEL[0], "--version"], text=True).strip()
    assert REPORT["bazel"] == "bazel 9.3.1"
    save()
    package = CWD / "module_map_exec"
    package.mkdir()
    shutil.copyfile(FIXTURE / "fixture.BUILD", package / "BUILD.bazel")
    shutil.copyfile(FIXTURE / "view.bzl", package / "view.bzl")
    cell("stock-gnu-closure", ORIGINAL, "gnu", False)
    (ROOT / OWNER).write_bytes(subprocess.check_output(["git", "show", PR899 + "^:" + OWNER]))
    assert git("diff", "--name-only") == OWNER
    for worker in ["gnu","musl"]:
        cell("unwrapped-" + worker, UNWRAPPED, worker, True)
    cells = list(REPORT["cells"].values())
    if all(c["verified"] for c in cells):
        # The question about the GNU closure is observational. Positive
        # controls ensure zero counts can really distinguish the supplier.
        assert all(c["musl_actions"].get("CppCompile", 0) > 0 for c in [cells[0],cells[2]])
        REPORT["gnu_requires_musl_compiles"] = cells[1]["musl_actions"].get("CppCompile", 0) > 0
        REPORT["verified"] = True
    save()
    assert REPORT.get("verified"), list(REPORT["cells"])


if __name__ == "__main__":
    main()
