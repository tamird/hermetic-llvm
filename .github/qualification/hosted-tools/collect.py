"""Compare stock tools with only the registered native execution platform changed."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit
import zipfile

SOURCE = "f9abea4c99d6d4a779488463419b2b0486497cbc"
ROOT = Path.cwd()
PKG = "hosted_tools_qualification"
TARGET = f"//{PKG}:hosted_tools"
GNU = "@llvm//platforms:linux_x86_64_gnu.2.28"
WORK = ROOT / "e2e/rules_cc"
FIXTURE = ROOT / ".github/qualification/hosted-tools"
TMP = Path(os.environ["RUNNER_TEMP"]) / "stock-llvm-platform"
BAZEL = TMP / "bazel"
REPORT = {"source": SOURCE, "carrier": os.environ["GITHUB_SHA"], "target": GNU, "host": GNU, "arms": {}}


def save():
    (TMP / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def collect(arm):
    worker = f"//{PKG}:{arm}"
    row = REPORT["arms"][arm] = {"exec_platform_request": worker, "verified": False}
    save()
    log, bep = (TMP / f"{arm}.{suffix}" for suffix in ["log", "bep"])
    startup = [str(BAZEL), "--ignore_all_rc_files", "--host_jvm_args=-Xmx3500m"]
    flags = [
        "--jobs=3", "-c", "opt", "--lockfile_mode=off", "--remote_executor=", "--bes_backend=",
        "--remote_cache=", "--repo_env=BAZEL_DO_NOT_DETECT_CPP_TOOLCHAIN=1", "--experimental_cc_static_library",
        "--platforms=" + GNU, "--host_platform=" + GNU, "--extra_execution_platforms=" + worker,
    ]
    args = [*startup, "test", *flags,
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
            if key.get("label", "").removeprefix("@@") == TARGET:
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
    assert row["test"]["strategy"] in ["", "local"] and row["test"]["attempt"] == 1
    assert observed["arm"] == arm and observed["machine"] == "x86_64" and observed["verified"]
    query = 'kind("platform_facts rule", deps(' + TARGET + ', 1))'
    formatter = 'json.encode([str(target.label), [str(p) for p in build_options(target)["//command_line_option:platforms"]]])'
    query_run = subprocess.run([*startup, "cquery", *flags, "--output=starlark", "--starlark:expr=" + formatter, query],
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
    assert exec_label.lstrip("@") == worker and target_label.endswith("//platforms:linux_x86_64_gnu.2.28"), rows
    row["verified"] = True
    save()


def main():
    assert platform.machine() == "x86_64" and platform.system() == "Linux"
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == SOURCE
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == REPORT["carrier"]
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
    if all(row["verified"] for row in REPORT["arms"].values()):
        observations = [r["probe"] for r in REPORT["arms"].values()]
        REPORT["same_gnu_target_digest"] = len({p["gnu_target"]["sha256"] for p in observations}) == 1
        REPORT["verified"] = True
    save()
    assert REPORT.get("verified")


if __name__ == "__main__":
    main()
