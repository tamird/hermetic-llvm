"""Build the four reported MSVC targets and exercise the six public exec tools."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import urllib.parse
import zipfile


BASE = "cf9d2e26369d531d159c54c90ed6ac82f5d7d8c1"
FIXED = "ed1ed5105e6cd8b638b1e55d4c32e28b7511541d"
TARGET = "//hosted_tools_qualification:hosted_tools"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main():
    repo = Path(os.environ["GITHUB_WORKSPACE"])
    fixture = Path(__file__).parent
    work = repo / "e2e/rules_cc"
    tmp = Path(os.environ["RUNNER_TEMP"])
    public = tmp / "hosted-tools.json"
    cpu = platform.machine()
    assert cpu == os.environ["EXPECTED_CPU"]
    assert git("rev-parse", "HEAD") == os.environ["GITHUB_SHA"]
    assert git("rev-parse", "HEAD^") == FIXED
    assert git("rev-parse", "HEAD^^") == BASE
    receipt = {"carrier": os.environ["GITHUB_SHA"], "machine": cpu, "phases": {},
               "fixture": {p: hashlib.sha256((fixture / p).read_bytes()).hexdigest()
                           for p in ("BUILD.bazel", "probe.py", "contract.bzl")}}

    def save():
        public.write_text(json.dumps(receipt, indent=2) + "\n")

    save()
    for arm, sha in (("fixed", FIXED),):
        row = receipt["phases"][arm] = {"source": sha, "started": now(), "verified": False}
        save()
        try:
            subprocess.run(["git", "checkout", "--detach", sha], check=True)
            assert git("rev-parse", "HEAD") == sha and not git("diff", "--name-only")
            package = work / "hosted_tools_qualification"
            package.mkdir(exist_ok=True)
            for path, digest in receipt["fixture"].items():
                shutil.copyfile(fixture / path, package / path)
                assert hashlib.sha256((package / path).read_bytes()).hexdigest() == digest
            label = "@llvm//platforms:linux_" + cpu + "_gnu.2.28"
            bep = tmp / (arm + ".bep.json")
            log = tmp / (arm + ".log")
            command = ["bazel", "test", "-c", "opt", "--jobs=4", "--nocache_test_results",
                       "--strategy=TestRunner=local", "--zip_undeclared_test_outputs",
                       "--nobuild_event_json_file_path_conversion", "--lockfile_mode=off",
                       "--platforms=" + label, "--host_platform=" + label,
                       "--build_event_json_file=" + str(bep), "--test_arg=" + arm, TARGET]
            row["platform"] = label
            row["bazel_version"] = subprocess.check_output(["bazel", "--version"], cwd=work, text=True).strip()
            with log.open("w") as out:
                row["exit"] = subprocess.run(command, cwd=work, stdout=out, stderr=subprocess.STDOUT).returncode
            row["execution_finished"] = now()
            save()
            if row["exit"]:
                lines = log.read_text(errors="replace").splitlines()
                first = next((i for i, line in enumerate(lines) if re.search(r"\b(ERROR|FATAL):", line)), None)
                if first is not None:
                    row["first_error"] = [re.sub(r"https?://\S+", "[URL]", line)[:600]
                                          for line in lines[first:first + 5]]
            events = [json.loads(line) for line in bep.read_text().splitlines()] if bep.exists() else []
            started = [e["started"] for e in events if "started" in e]
            row["invocation"] = started[0]["uuid"] if started else None
            cases = [e["testResult"] for e in events
                     if e.get("id", {}).get("testResult", {}).get("label", "").removeprefix("@@") == TARGET]
            row["tests"] = [{"status": case.get("status"), "cached": case.get("cachedLocally", False),
                             "strategy": case.get("executionInfo", {}).get("strategy"),
                             "remote_cache": case.get("executionInfo", {}).get("remoteCacheHit", False)}
                            for case in cases]
            save()
            assert len(cases) == 1, "expected one original TestRunner"
            paths = []
            for item in cases[0].get("testActionOutput", []):
                uri = urllib.parse.urlparse(item.get("uri", ""))
                if uri.scheme != "file":
                    continue
                path = Path(urllib.parse.unquote(uri.path))
                if path.is_file() and path.name == "test.log":
                    original = path.read_bytes()
                    row["test_log_sha256"] = hashlib.sha256(original).hexdigest()
                    if row["exit"]:
                        lines = original.decode(errors="replace").splitlines()
                        row["test_failure_tail"] = [re.sub(r"https?://\S+", "[URL]", line)[:600]
                                                    for line in lines[-10:]]
                        save()
                if path.is_file() and (path.suffix == ".zip" or path.name == "probe.json"):
                    paths.append(path)
            # The arm, machine and hashes in probe.json must also match. This
            # fallback handles Bazel versions that only list test.log/test.xml.
            if not paths:
                testout = work / "bazel-testlogs/hosted_tools_qualification/hosted_tools/test.outputs"
                paths = [p for p in (testout / "outputs.zip", testout / "probe.json") if p.is_file()]
            probes = []
            for path in paths:
                if path.suffix == ".zip":
                    with zipfile.ZipFile(path) as z:
                        for name in z.namelist():
                            if name == "probe.json":
                                probes.append(z.read(name))
                else:
                    probes.append(path.read_bytes())
            assert len(probes) == 1, "expected one original probe receipt"
            row["probe_sha256"] = hashlib.sha256(probes[0]).hexdigest()
            probe = row["probe"] = json.loads(probes[0])
            save()
            result = row["tests"][0]
            row["verified"] = (row["exit"] == 0 and result["status"] == "PASSED" and
                               not result["cached"] and not result["remote_cache"] and
                               result["strategy"] in (None, "", "local") and
                               probe.get("verified") is True and probe["arm"] == arm and probe["machine"] == cpu and
                               row["bazel_version"] == "bazel 10.0.0rc2")
        except Exception as e:
            row["collection"] = f"{type(e).__name__}: {str(e)[:450]}"
        row["verified_at"] = now()
        save()
    # Both phases run at the exact product checkout, independently of their verdicts.
    # Each consumer has its own Bazel server; release the first before the next
    # workspace opens the host and Windows SDKs on a standard hosted runner.
    cleanup = subprocess.run(["bazel", "clean", "--expunge"], cwd=work,
                             stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    receipt["scratch_cleanup_exit"] = cleanup.returncode
    row = receipt["phases"]["msvc"] = {"source": FIXED, "started": now(), "verified": False}
    save()
    try:
        assert git("rev-parse", "HEAD") == FIXED and not git("diff", "--name-only")
        work = repo / "e2e/cross_compilation"
        labels = ["//:main_windows_" + arch + "_msvc" + options
                  for arch in ("x86_64", "aarch64") for options in ("", "_external_include_paths")]
        row["expected"] = labels
        row["bazel_version"] = subprocess.check_output(["bazel", "--version"], cwd=work, text=True).strip()
        assert row["bazel_version"] == "bazel 10.0.0rc2"
        bep = tmp / "msvc.bep.json"
        log = tmp / "msvc.log"
        command = ["bazel", "--bazelrc=" + str(repo / ".github/workflows/ci.bazelrc"), "--bazelrc=.bazelrc",
                   "build", "--repository_cache=", "--repo_contents_cache=", "--jobs=100",
                   "--repo_env=BAZEL_MSVC_RUNTIME_VISUAL_STUDIO_EULA=1",
                   "--repo_env=BAZEL_WINDOWS_SDK_EULA=1", "--color=no", "--build_event_publish_all_actions",
                   "--build_event_json_file=" + str(bep), *labels]
        with log.open("w") as out:
            row["exit"] = subprocess.run(command, cwd=work, stdout=out, stderr=subprocess.STDOUT).returncode
        row["execution_finished"] = now()
        save()
        row["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
        if row["exit"]:
            lines = log.read_text(errors="replace").splitlines()
            first = next((i for i, line in enumerate(lines) if re.search(r"\b(ERROR|FATAL):", line)), None)
            if first is not None:
                row["first_error"] = [re.sub(r"https?://\S+", "[URL]", line)[:600]
                                      for line in lines[first:first + 8]
                                      if not re.search(r"(?i)(api.key|authorization|bearer)", line)]
        row["completed"] = {}
        row["overlays"] = []
        if bep.is_file():
            with bep.open() as events:
                for line in events:
                    e = json.loads(line)
                    if "started" in e:
                        row["invocation"] = e["started"]["uuid"]
                    label = e.get("id", {}).get("targetCompleted", {}).get("label", "").removeprefix("@@")
                    if label in labels and "completed" in e:
                        row["completed"][label] = e["completed"].get("success", False)
                    if e.get("action", {}).get("type") == "CaseInsensitiveVFS":
                        action = e["id"]["actionCompleted"]
                        row["overlays"].append({"label": action.get("label"),
                                                "output": action.get("primaryOutput"),
                                                "success": e["action"].get("success", False)})
        row["verified"] = (row["exit"] == 0 and row.get("invocation") is not None
                           and row["completed"] == dict.fromkeys(labels, True))
    except Exception as e:
        row["collection"] = f"{type(e).__name__}: {str(e)[:450]}"
    row["verified_at"] = now()
    save()
    receipt["verified"] = all(r["verified"] for r in receipt["phases"].values())
    receipt["verified_at"] = now()
    save()
    return 0 if receipt["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
