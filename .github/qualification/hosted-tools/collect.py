"""Run the same Bazel test against the two exact supplier sources."""

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


BASE = "413655560cc6269a1bf4eaac5100fd96830948bd"
FIXED = "cf9d2e26369d531d159c54c90ed6ac82f5d7d8c1"
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
                           for p in ("BUILD.bazel", "probe.py")}}

    def save():
        public.write_text(json.dumps(receipt, indent=2) + "\n")

    save()
    for arm, sha in (("stock", BASE), ("fixed", FIXED)):
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
                               row["bazel_version"] == "bazel 9.3.0rc2")
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
