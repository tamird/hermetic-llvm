"""Compare the same C cancellation test and inspect libunwind's actual compiler."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SOURCE = "c889b0597c7d1af8d31caedca2d896e40a5ce879"
BASE = "d779c289633baa6ab566be8af75539bd646d9b86"
TEST = "//tests/linux_linking:static_glibc_pthread_cancel_test"
FILES = ["tests/linux_linking/BUILD.bazel", "tests/linux_linking/pthread_cancel.c"]
BAZEL = ["bazel", "--ignore_all_rc_files"]
FLAGS = ["--platforms=//platforms:linux_x86_64_glibc_static", "--extra_toolchains=//toolchain:all",
         "--extra_execution_platforms=@platforms//host:host,//platforms:linux_x86_64_glibc_static",
         "--jobs=4", "--lockfile_mode=off", "--remote_cache=", "--remote_executor="]
OUT = Path(os.environ["RUNNER_TEMP"]) / "unwind"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def measure(label: str) -> dict:
    bep = OUT / (label + ".bep")
    p = subprocess.run([*BAZEL, "test", *FLAGS, "--nocache_test_results",
                        "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep),
                        TEST], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=2700)
    row = {"exit": p.returncode, "output_sha256": hashlib.sha256(p.stdout.encode()).hexdigest(),
           "undefined": sorted(set(re.findall(r"undefined symbol: ([\w]+)", p.stdout))),
           "missing_libraries": sorted(set(re.findall(r"unable to find library (-l[\w+.-]+)", p.stdout))),
           "failure_kinds": [s for s in ("No space left on device", "no such package", "cycle in dependency graph",
                                         "not compatible", "OutOfMemoryError", "No matching toolchains")
                             if s in p.stdout],
           "links": [], "test_results": []}
    if bep.exists():
        with bep.open() as events:
            for line in events:
                event = json.loads(line)
                id_ = event.get("id", {})
                action = event.get("action", {})
                if id_.get("actionCompleted", {}).get("label") == TEST and action.get("type") == "CppLink":
                    row["links"].append({"exit": action.get("exitCode", 0), "success": action.get("success", False)})
                if id_.get("testResult", {}).get("label") == TEST:
                    row["test_results"].append(event["testResult"]["status"])
    return row


def stages(node) -> set[str]:
    matches = set()
    if isinstance(node, dict):
        for key, val in node.items():
            if key.endswith("//toolchain:runtime_stage") and isinstance(val, str):
                matches.add(val)
            else:
                matches.update(stages(val))
    elif isinstance(node, list):
        for item in node:
            matches.update(stages(item))
    return matches


def inspect_compiler() -> dict:
    query = 'mnemonic("CppCompile", filter("//libunwind:libunwind$", deps(//runtimes/libunwind:libunwind.static)))'
    doc = json.loads(subprocess.check_output([*BAZEL, "aquery", *FLAGS, "--noinclude_artifacts",
                                               "--output=jsonproto", query], timeout=300))
    configs = {str(c["id"]): c["checksum"] for c in doc.get("configuration", [])}
    selected = {}
    for action in doc.get("actions", []):
        if any(s.endswith("/libunwind/src/UnwindLevel1.c") for s in action.get("arguments", [])):
            h = configs[str(action["configurationId"])]
            config = json.loads(subprocess.check_output([*BAZEL, "config", "--output=json", h], timeout=60))
            selected[h] = sorted(stages(config))
    transition = subprocess.run([*BAZEL, "cquery", *FLAGS, "--output=transitions", "--transitions=full",
                                 "deps(//runtimes/libunwind:libunwind.static, 4)"],
                                text=True, stdout=subprocess.PIPE, timeout=300, check=True)
    return {"UnwindLevel1.c": selected, "stage_changes": [
        line.strip()[:320] for line in transition.stdout.splitlines()
        if "toolchain:runtime_stage" in line or ("->" in line and "libunwind.static" in line)
    ][:16]}


def main() -> None:
    r = {"source": SOURCE, "parent": BASE}
    receipt = OUT / "report.json"
    try:
        assert git("rev-parse", "HEAD^") == SOURCE and git("rev-parse", "HEAD^^") == BASE
        assert subprocess.check_output([*BAZEL, "--version"], text=True).strip() == "bazel 9.2.0"
        git("checkout", "--detach", BASE)
        git("restore", "--source=" + SOURCE, "--staged", "--worktree", "--", *FILES)
        assert git("diff", "--name-only", "HEAD").splitlines() == FILES
        r["parent_test"] = measure("parent")
        receipt.write_text(json.dumps(r, indent=2) + "\n")
        git("restore", "--source=" + BASE, "--staged", "--worktree", "--", *FILES)
        git("checkout", "--detach", SOURCE)
        assert git("rev-parse", "HEAD") == SOURCE and git("status", "--porcelain", "-uno") == ""
        r["candidate_test"] = measure("candidate")
        receipt.write_text(json.dumps(r, indent=2) + "\n")
        r["bootstrap"] = inspect_compiler()
        parent, candidate = r["parent_test"], r["candidate_test"]
        r["passed"] = (parent["exit"] != 0 and any(s.startswith("_Unwind_") for s in parent["undefined"])
                       and any(not a["success"] for a in parent["links"])
                       and candidate["exit"] == 0 and candidate["test_results"] == ["PASSED"]
                       and bool(candidate["links"]) and all(a["success"] for a in candidate["links"])
                       and any("stage0" in stages_ for stages_ in r["bootstrap"]["UnwindLevel1.c"].values()))
    finally:
        receipt.write_text(json.dumps(r, indent=2) + "\n")
    sys.exit(not r.get("passed", False))


if __name__ == "__main__":
    main()
