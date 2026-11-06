"""Check the two public static GNU C runtime archives on current upstream LLVM."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

SOURCE = "be935b8288c180ea99f86b5c7f6aa191a854c563"
UPSTREAM = "d01e636dea633dda7a12ceedddd9c64146fb9d54"
C_SERIES = "f9d284160b24126e6fdcc37b715ce832913a6349"
TARGETS = (
    "//tests/linux_linking:static_glibc_pthread_cancel_test",
    "//tests/linux_linking/qualification_static_c:resolver_test",
)
OUT = Path(os.environ["RUNNER_TEMP"]) / "unwind"


def capture(*args):
    return subprocess.check_output(args, text=True).strip()


def main(report):
    for ref, expected in (("HEAD^", SOURCE), ("HEAD^^^^^1", UPSTREAM), ("HEAD^^^^^2", C_SERIES)):
        assert capture("git", "rev-parse", ref) == expected
    assert set(capture("git", "diff", "--name-only", SOURCE, "HEAD").splitlines()) == {
        ".github/workflows/hosted-tools.yml", ".github/workflows/static_native.py",
        "tests/linux_linking/qualification_static_c/BUILD.bazel", "tests/linux_linking/qualification_static_c/resolver.c",
    }
    assert os.uname().machine == "aarch64"
    report["bazel"] = capture("bazel", "--version")
    assert report["bazel"] == "bazel 9.3.0rc2"
    bep = OUT / "events.jsonl"
    argv = ["bazel", "--ignore_all_rc_files", "test", "--platforms=//platforms:linux_aarch64_glibc_static",
            "--extra_toolchains=//toolchain:all",
            "--extra_execution_platforms=@platforms//host:host,//platforms:linux_aarch64_glibc_static",
            "--jobs=4", "--strip=never", "--lockfile_mode=off", "--remote_cache=", "--remote_executor=",
            "--experimental_repository_downloader_retries=3", "--distdir=" + str(OUT / "dist"),
            "--nocache_test_results", "--test_output=errors", "--build_event_publish_all_actions",
            "--build_event_json_file=" + str(bep), "--keep_going", *TARGETS]
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=2700)
    report["exit"] = proc.returncode
    report["output_sha256"] = hashlib.sha256(proc.stdout.encode()).hexdigest()
    errors = []
    for line in proc.stdout.splitlines():
        if any(t in line for t in ("ERROR:", "Error in ", "undefined symbol:", "unable to find library", "No space left")):
            errors.append(re.sub(r"https?://[^\s'\"<>]+", lambda m: "https://" + (urlsplit(m[0]).hostname or ""), line)[:520])
    report["diagnostics"] = errors[:12]
    products, results = report["links"], report["test_results"]
    for line in bep.open() if bep.exists() else ():
        event = json.loads(line)
        owner = event["id"].get("testResult", {}).get("label")
        if owner in TARGETS:
            results.setdefault(owner, []).append(event["testResult"]["status"])
        act = event.get("action", {})
        key = event["id"].get("actionCompleted", {})
        owner = key.get("label")
        if act.get("type") != "CppLink" or owner not in TARGETS:
            continue
        row = products[owner] = {"success": act.get("success", False)}
        if row["success"]:
            binary = Path(key["primaryOutput"])
            assert binary.parts[0] == "bazel-out" and ".." not in binary.parts
            raw = binary.read_bytes()
            assert raw[:6] == b"\x7fELF\x02\x01"
            row["bytes"] = len(raw)
            row["sha256"] = hashlib.sha256(raw).hexdigest()
            row["machine"] = int.from_bytes(raw[18:20], "little")
            headers = capture("readelf", "-Wld", binary)
            row["static"] = "INTERP" not in headers and "(NEEDED)" not in headers
    return (proc.returncode == 0 and set(products) == set(TARGETS)
            and all(p["success"] and p["machine"] == 183 and p["static"] for p in products.values())
            and results == {owner: ["PASSED"] for owner in TARGETS})


report = {"upstream": UPSTREAM, "source": SOURCE, "c_series": C_SERIES, "links": {}, "test_results": {}, "passed": False}
try:
    report["passed"] = main(report)
except (AssertionError, ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
    report["error"] = type(e).__name__
finally:
    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
sys.exit(not report["passed"])
