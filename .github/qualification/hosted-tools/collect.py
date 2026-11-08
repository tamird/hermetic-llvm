"""Compare stock tools with only the registered native execution platform changed."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
from urllib.parse import unquote, urlsplit
import zipfile

SOURCE = "39e71f9391895169b05ccd62cbb1956ae5322ff3"
ROOT = Path.cwd()
PKG = "hosted_tools_qualification"
TARGET = f"//{PKG}:hosted_tools"
GNU = "@llvm//platforms:linux_aarch64_gnu.2.28"
WORK = ROOT / "e2e/rules_cc"
FIXTURE = ROOT / ".github/qualification/hosted-tools"
TMP = Path(os.environ["RUNNER_TEMP"]) / "stock-llvm-platform"
BAZEL = TMP / "bazel"
REPORT = {"source": SOURCE, "carrier": os.environ["GITHUB_SHA"], "target": GNU, "host": GNU, "arms": {}}
STARTUP = [str(BAZEL), "--ignore_all_rc_files", "--host_jvm_args=-Xmx3500m"]


def save():
    (TMP / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def verify_release():
    archive = TMP / "release.tar.gz"
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert sha == "64d421959bba30cb3bcdd1e57a01b8ef2f12b93db3d1ab4a34fe5f4d2ca78aa4"
    source = json.loads((TMP / "source.json").read_text())
    assert source["integrity"] == "sha256-ZNQhlZu6MMs7zdHlegG47y8SuT2z0atKNP5fTSyniqQ="
    assert source["patches"] == {"module_dot_bazel_version.patch": "sha256-tuCvydBVhclhoTy+ZjmoWaa5a4Rh9gRNiLYQRdjU0dY="}
    assert not source.get("overlay")
    blobs = {}
    for record in subprocess.check_output(["git", "ls-tree", "-rz", SOURCE]).split(b"\0"):
        if record:
            header, name = record.split(b"\t", 1)
            blobs[name.decode()] = header.split()[2].decode()
    matched = 0
    with tarfile.open(archive) as files:
        for entry in files:
            if not (entry.isfile() or entry.issym()):
                continue
            path = entry.name.removeprefix(source["strip_prefix"] + "/")
            data = files.extractfile(entry).read() if entry.isfile() else entry.linkname.encode()
            git_header = f"blob {len(data)}\0".encode()
            assert hashlib.sha1(git_header + data).hexdigest() == blobs[path], path
            matched += 1
    assert matched == 370
    REPORT["registry"] = {"archive_sha256": sha, "tag_files_matched": matched, "patches": list(source["patches"])}
    save()


def build_flags(worker, target):
    return [
        "--jobs=3", "-c", "opt", "--lockfile_mode=off", "--remote_executor=", "--bes_backend=",
        "--remote_cache=", "--repo_env=BAZEL_DO_NOT_DETECT_CPP_TOOLCHAIN=1", "--experimental_cc_static_library",
        "--platforms=" + target, "--host_platform=" + GNU, "--extra_execution_platforms=" + worker,
    ]


def collect(arm):
    worker = f"//{PKG}:{arm}"
    row = REPORT["arms"][arm] = {"exec_platform_request": worker, "verified": False}
    save()
    log, bep = (TMP / f"{arm}.{suffix}" for suffix in ["log", "bep"])
    flags = build_flags(worker, GNU)
    args = [*STARTUP, "test", *flags,
        "--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1",
        "--zip_undeclared_test_outputs", "--build_event_json_file=" + str(bep),
        TARGET,
    ]
    with log.open("wb") as out:
        row["exit"] = subprocess.run(args, cwd=WORK, stdout=out, stderr=subprocess.STDOUT).returncode
    if row["exit"]:
        lines = log.read_text(errors="replace").splitlines()
        starts = [i for i, s in enumerate(lines) if re.search(r"ERROR:|error:|FATAL:", s)][:3]
        indexes = sorted({j for i in starts for j in range(i, min(i + 6, len(lines)))} | set(range(max(0, len(lines)-5), len(lines))))
        row["diagnostics"] = [re.sub(r"https?://\S+", "<url>", lines[i])[:400] for i in indexes]
    save()
    cases = []
    with bep.open() as stream:
        for line in stream:
            event = json.loads(line)
            if "started" in event:
                row["invocation"] = event["started"]["uuid"]
            key = event["id"].get("testResult", {})
            if "testResult" in event and key.get("label", "").removeprefix("@@") == TARGET:
                cases.append((key, event["testResult"]))
    assert len(cases) == 1, f"observed {len(cases)} original results"
    (key, result), = cases
    run = result.get("executionInfo", {})
    row["test"] = {"status": result["status"], "attempt": key.get("attempt", 1),
                   "configuration": key["configuration"]["id"], "strategy": run.get("strategy", ""),
                   "cached": bool(result.get("cachedLocally") or run.get("cachedRemotely") or run.get("remoteCacheHit"))}
    save()
    payload = []
    for item in result.get("testActionOutput", []):
        url = urlsplit(item.get("uri", ""))
        if url.scheme != "file" or url.netloc:
            continue
        path = Path(unquote(url.path))
        if path.name not in ("test.log", "outputs.zip", "probe.json"):
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert item.get("digest", digest) == digest and int(item.get("length", len(data))) == len(data)
        if path.name == "test.log":
            assert path.as_posix().endswith("/testlogs/" + PKG + "/hosted_tools/test.log")
            row["test_log_sha256"] = digest
            if row["exit"]:
                row["test_failure_tail"] = [s[:400] for s in data.decode(errors="replace").splitlines()[-10:]]
        elif path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                if "probe.json" in archive.namelist():
                    assert archive.getinfo("probe.json").file_size < 65536
                    payload.append(archive.read("probe.json"))
        else:
            payload.append(data)
    save()
    assert row.get("test_log_sha256") and len(payload) == 1, "original log/undeclared outputs incomplete"
    raw, = payload
    row["probe_sha256"] = hashlib.sha256(raw).hexdigest()
    observed = row["probe"] = json.loads(raw)
    save()
    assert row["exit"] == 0 and row["test"]["status"] == "PASSED" and not row["test"]["cached"]
    assert row["test"]["strategy"] in ["local", "standalone"] and row["test"]["attempt"] == 1
    assert observed["arm"] == arm and observed["machine"] == "aarch64" and observed["verified"]
    query = 'kind("platform_facts rule", deps(' + TARGET + ', 1))'
    formatter = 'json.encode([str(target.label), [str(p) for p in build_options(target)["//command_line_option:platforms"]]])'
    query_run = subprocess.run([*STARTUP, "cquery", *flags, "--output=starlark", "--starlark:expr=" + formatter, query],
                               cwd=WORK, capture_output=True, text=True)
    row["platform_query_exit"] = query_run.returncode
    rows = [json.loads(line) for line in query_run.stdout.splitlines()]
    row["configured_platforms"] = rows
    save()
    assert query_run.returncode == 0, query_run.stderr[-350:]
    platforms = {label.split(":")[-1]: platforms for label, platforms in rows}
    assert len(rows) == 2 and set(platforms) == {"target_platform", "exec_platform"}, rows
    exec_label, = platforms["exec_platform"]
    target_label, = platforms["target_platform"]
    assert exec_label.lstrip("@") == worker and target_label.endswith("//platforms:linux_aarch64_gnu.2.28"), rows
    row["verified"] = True
    save()


def cross_build():
    """Inspect a GNU x86-64 program compiled and linked on this AArch64 worker."""
    worker = f"//{PKG}:musl"
    program = f"//{PKG}:gnu_program"
    target = "@llvm//platforms:linux_x86_64_gnu.2.28"
    row = REPORT["cross"] = {"target": target, "host": GNU, "requested_exec": worker, "actions": [], "verified": False}
    flags = build_flags(worker, target) + ["--strategy=CppCompile=local", "--strategy=CppLink=local"]
    bep = TMP / "cross.bep"
    with (TMP / "cross.log").open("wb") as log:
        result = subprocess.run([*STARTUP, "build", *flags, "--build_event_publish_all_actions",
                                 "--build_event_json_file=" + str(bep), program], cwd=WORK, stdout=log, stderr=subprocess.STDOUT)
    row["exit"] = result.returncode
    save()
    if result.returncode:
        lines = (TMP / "cross.log").read_text(errors="replace").splitlines()
        row["diagnostics"] = [re.sub(r"https?://\S+", "<url>", s)[:400] for s in lines if "ERROR:" in s or "error:" in s][:5]
        raise RuntimeError("GNU cross-target build failed")
    actual = {}
    for line in bep.open():
        event = json.loads(line)
        if "started" in event:
            row["invocation"] = event["started"]["uuid"]
        key = event["id"].get("actionCompleted", {})
        owner = key.get("label", "").lstrip("@")
        action = event.get("action", {})
        kind = action.get("type")
        if owner.startswith(f"//{PKG}:") and kind in ("CppCompile", "CppLink") and action.get("success"):
            actual[owner, kind, key["configuration"]["id"]] = key["primaryOutput"]
    query = f'mnemonic("Cpp(Compile|Link)", filter("//{PKG}:", deps({program})))'
    graph = json.loads(subprocess.check_output([*STARTUP, "aquery", *flags, "--output=jsonproto",
                                               "--noinclude_artifacts", "--include_commandline", query], cwd=WORK))
    owners = {str(t["id"]): t["label"].lstrip("@") for t in graph["targets"]}
    configs = {str(c["id"]): c["checksum"] for c in graph["configuration"]}
    for action in graph["actions"]:
        owner = owners[str(action["targetId"])]
        kind = action["mnemonic"]
        cfg = configs[str(action["configurationId"])]
        output = actual.get((owner, kind, cfg))
        if output is None:
            continue
        assert action["executionPlatform"].lstrip("@") == worker
        args = action["arguments"]
        if kind == "CppCompile":
            assert any(arg.endswith("/gnu.c") for arg in args)
        row["actions"].append({"owner": owner, "mnemonic": kind, "config": cfg, "output": output,
                               "exec": action["executionPlatform"], "tool": args[0]})
    save()
    assert sorted(a["mnemonic"] for a in row["actions"]) == ["CppCompile", "CppLink"], row["actions"]
    [name] = subprocess.check_output([*STARTUP, "cquery", *flags, "--output=files", program], cwd=WORK, text=True).splitlines()
    binary = WORK / name
    link = next(a for a in row["actions"] if a["mnemonic"] == "CppLink")
    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert hashlib.sha256((WORK / link["output"]).read_bytes()).hexdigest() == sha
    elf = subprocess.check_output(["readelf", "-hlWd", binary], text=True)
    row["binary"] = {"file": name, "bytes": binary.stat().st_size, "sha256": sha,
                     "machine": re.search(r"^\s*Machine:\s*(.*?)\s*$", elf, re.M)[1],
                     "interpreter": re.search(r"Requesting program interpreter: (.*?)\]", elf)[1],
                     "needed": re.findall(r"\(NEEDED\).*?\[(.*?)\]", elf)}
    save()
    assert row["binary"]["machine"] == "Advanced Micro Devices X86-64"
    assert row["binary"]["interpreter"] == "/lib64/ld-linux-x86-64.so.2" and "libc.so.6" in row["binary"]["needed"]
    row["verified"] = True
    save()


def main():
    assert platform.machine() == "aarch64" and platform.system() == "Linux"
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == SOURCE
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == REPORT["carrier"]
    verify_release()
    files = subprocess.check_output(["git", "diff", "--name-only", SOURCE, "HEAD"], text=True).splitlines()
    assert set(files) == {".github/workflows/hosted-tools.yml"} | {
        ".github/qualification/hosted-tools/" + name
        for name in ["BUILD.bazel", "collect.py", "contract.bzl", "gnu.c", "probe.py"]}
    REPORT["fixture_files"] = {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files}
    REPORT["bazel"] = subprocess.check_output([BAZEL, "--version"], text=True).strip()
    assert REPORT["bazel"] == "bazel 9.3.1"
    save()
    package = WORK / PKG
    package.mkdir()
    for name in ["BUILD.bazel", "contract.bzl", "gnu.c", "probe.py"]:
        shutil.copyfile(FIXTURE / name, package / name)
    for arm in ["unspecified", "musl"]:
        try:
            collect(arm)
        except Exception as error:
            REPORT["arms"][arm]["error"] = f"{type(error).__name__}: {error}"[:450]
            save()
    try:
        cross_build()
    except Exception as error:
        REPORT["cross"]["error"] = f"{type(error).__name__}: {error}"[:450]
        save()
    if all(row["verified"] for row in REPORT["arms"].values()):
        observations = [r["probe"] for r in REPORT["arms"].values()]
        REPORT["same_gnu_target_digest"] = len({p["gnu_target"]["sha256"] for p in observations}) == 1
        REPORT["native_pair_verified"] = True
        REPORT["verified"] = REPORT["native_pair_verified"] and REPORT["cross"]["verified"]
    save()
    assert REPORT.get("verified")


if __name__ == "__main__":
    main()
