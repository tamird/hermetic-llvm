"""Run the native cancellation test and retain the reason for repository failures."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

SOURCE = "1273f916be8c7109542be8fef009258a56369462"
TARGET = "//tests/linux_linking:static_glibc_pthread_cancel_test"
OUT = Path(os.environ["RUNNER_TEMP"]) / "unwind"


def main() -> None:
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    version = subprocess.check_output(["bazel", "--version"], text=True).strip()
    assert version == "bazel 9.2.0"
    host = os.uname().machine
    assert host == "aarch64", host
    bep = OUT / "events.jsonl"
    argv = ["bazel", "--ignore_all_rc_files", "test", "--platforms=//platforms:linux_aarch64_glibc_static",
            "--extra_toolchains=//toolchain:all",
            "--extra_execution_platforms=@platforms//host:host,//platforms:linux_aarch64_glibc_static",
            "--jobs=4", "--lockfile_mode=off", "--remote_cache=", "--remote_executor=",
            "--experimental_repository_downloader_retries=3", "--distdir=" + str(OUT / "dist"),
            "--nocache_test_results", "--test_output=errors", "--build_event_publish_all_actions",
            "--build_event_json_file=" + str(bep), TARGET]
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=2700)
    products, results = [], []
    if bep.exists():
        with bep.open() as events:
            for line in events:
                event = json.loads(line)
                id_ = event.get("id", {})
                action = event.get("action", {})
                if id_.get("actionCompleted", {}).get("label") == TARGET and action.get("type") == "CppLink":
                    product = {"exit": action.get("exitCode", 0), "success": action.get("success", False)}
                    if product["success"]:
                        path = Path(id_["actionCompleted"]["primaryOutput"])
                        assert not path.is_absolute() and path.parts[0] == "bazel-out" and ".." not in path.parts
                        with path.open("rb") as binary:
                            header = binary.read(20)
                        assert header[:6] == b"\x7fELF\x02\x01"
                        product["elf_machine"] = int.from_bytes(header[18:20], "little")
                    products.append(product)
                if id_.get("testResult", {}).get("label") == TARGET:
                    results.append(event["testResult"]["status"])
    errors = []
    for line in proc.stdout.splitlines():
        if any(term in line for term in ("ERROR:", "Error in ", "no such package", "attempt to download",
                                        "unsupported option", "No matching toolchains", "undefined symbol:")):
            # Signed download URLs may include query parameters.
            public = re.sub(r"https?://[^\s'\"<>]+", lambda m: "https://" + (urlsplit(m[0]).hostname or ""),
                            line)
            errors.append(public[:600])
    passed = proc.returncode == 0 and bool(products) and all(p["success"] and p["elf_machine"] == 183 for p in products) and results == ["PASSED"]
    report = {"source": SOURCE, "host": host, "bazel_version": version, "target": TARGET, "exit": proc.returncode,
              "output_sha256": hashlib.sha256(proc.stdout.encode()).hexdigest(), "cpp_links": products,
              "test_results": results, "diagnostics": errors[:9], "passed": passed}
    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    sys.exit(not passed)


if __name__ == "__main__":
    main()
