"""Run the supplier header-selection tests with the existing native recorder."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit
import urllib.request

import credential_artifacts
from image_execution import execution_records

SOURCE = os.environ["UAPI_QUALIFICATION_SOURCE"]
REPORTER_SOURCE = "462126434f0cdf5e56f37603c82b563b456f1ce8"
REPORTER_HASHES = {
    "qualification_report.py": "3929b631d7d28c9dd97720281d8629af7a6bb611d670dafaf96793a4a8652e83",
    "unit_matrix.py": "209f47696a8c28e0072a6199d40f85edb855e656195189741ba4cb6215246a37",
}


def main() -> int:
    if sys.version_info < (3, 11):
        raise RuntimeError("Qualification requires Python 3.11+")
    if sys.argv[1:]:
        raise ValueError("Expected no arguments")
    work_seconds = 2400
    workspace = Path.cwd()
    if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != SOURCE:
        raise RuntimeError("Unexpected source")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"]):
        raise RuntimeError("Dirty tracked source")
    if (workspace / "user.bazelrc").exists():
        raise RuntimeError("Unexpected per-user workspace RC")
    artifacts = Path(os.environ["BUILDBUDDY_ARTIFACTS_DIRECTORY"])
    artifacts.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + work_seconds
    runner_exit = capture_exit = 0
    with tempfile.TemporaryDirectory(prefix="uapi-qualification-") as temporary:
        private = Path(temporary)
        auth = private / "auth"
        auth.mkdir(mode=0o700)
        credential_artifacts.prepare(auth)
        names = json.loads(os.environ.get("BUILDBUDDY_SECRET_ENV_VAR_NAMES", "[]"))
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError("Invalid runner secret inventory")
        names += ["BUILDBUDDY_API_KEY", "BB_GRPC_CLIENT_IDENTITY", "REPO_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"]
        secrets = {os.environ.pop(name).encode() for name in names if os.environ.get(name)}
        secrets |= {json.dumps(value.decode())[1:-1].encode() for value in secrets}
        # The supported hosted wrapper declares its native Bazel binary here.
        # Its generated RC embeds header credentials, so use the same binary
        # with the normal workspace RC and the private native helper instead.
        wrapper = Path(shutil.which("bazel") or "")
        tokens = shlex.split(wrapper.read_text(), comments=True)
        candidates = [token.removeprefix("BAZEL_BIN=") for token in tokens if token.startswith("BAZEL_BIN=")]
        if len(candidates) != 1 or not Path(candidates[0]).is_file():
            raise ValueError("Hosted wrapper omitted its native Bazel binary")
        bazel = candidates[0]
        # Read only the UUID metadata; never copy the credential-bearing RC.
        runner_rc = Path(os.environ["BUILDBUDDY_CI_RUNNER_ROOT_DIR"]) / "buildbuddy.bazelrc"
        parent_ids = re.findall(
            r"^common --build_metadata=PARENT_INVOCATION_ID=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
            runner_rc.read_text(), re.MULTILINE)
        if len(parent_ids) != 1:
            raise ValueError("Hosted runner omitted its unique parent invocation UUID")
        rc = private / "auth.bazelrc"
        rc.write_text("common --credential_helper=remote.buildbuddy.io=" + str(auth / "helper") + "\n")
        # Preserve the checked-in settings except its public CAS-only headers
        # and optional user overlay; private credentials use the reviewed helper.
        source_rc = private / "source.bazelrc"
        source_rc.write_text("\n".join(line for line in (workspace / ".bazelrc").read_text().splitlines()
                                      if "--remote_header=" not in line and "--bes_header=" not in line
                                      and not line.startswith("try-import ")) + "\n")
        with rc.open("a") as output:
            output.write("common --noannounce_rc\ncommon --lockfile_mode=off\n")
            for key, value in (("PARENT_INVOCATION_ID", parent_ids[0]), ("COMMIT_SHA", SOURCE),
                               ("REPO_URL", "https://github.com/tamird/hermetic-llvm")):
                output.write("common --build_metadata=" + key + "=" + value + "\n")
        startup = ["--nosystem_rc", "--nohome_rc", "--noworkspace_rc", "--bazelrc=" + str(source_rc),
                   "--bazelrc=" + str(rc), "--digest_function=sha256"]
        options = ["--remote_executor=grpcs://remote.buildbuddy.io", "--remote_cache=grpcs://remote.buildbuddy.io",
                   "--experimental_remote_downloader=grpcs://remote.buildbuddy.io", "--bes_backend=grpcs://remote.buildbuddy.io",
                   "--bes_results_url=https://app.buildbuddy.io/invocation/", "--remote_download_outputs=all",
                   "--lockfile_mode=off", "--noannounce_rc", "--spawn_strategy=remote", "--noremote_local_fallback", "--jobs=400",
                   "--extra_execution_platforms=@llvm//tools/uapi_qualification:linux_amd64",
                   "--build_event_json_file_path_conversion=false", "--remote_timeout=15m"]
        os.environ["BAZEL_BIN"] = bazel
        for name, expected in REPORTER_HASHES.items():
            with urllib.request.urlopen(f"https://raw.githubusercontent.com/tamird/gvisor/{REPORTER_SOURCE}/test/rbe/{name}", timeout=30) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("Pinned qualification helper identity differs")
            (private / name).write_bytes(raw)
        sys.path.insert(0, str(private))
        import qualification_report as report
        evidence = private / "evidence"
        evidence.mkdir()
        report.write_json(evidence / "plan.json", {"source": SOURCE, "work_seconds": work_seconds,
                          "native_bazel": bazel, "reporter": REPORTER_HASHES, "options": options,
                          "source_rc": report.file_record(workspace / ".bazelrc"),
                          "scope": "Root/e2e default selection, actual C includes and four forced extension errors. Linux AMD64 only.",
                          "lock_policy": "off: evaluate declared source-bound extensions without modifying tracked locks"})
        output_root = None

        def clean(data: bytes) -> bytes:
            for value in secrets:
                data = data.replace(value, b"[REDACTED]")
            return data

        def invoke(name: str, args: list[str], kind: str, targets: list[str], *, cwd: Path = workspace) -> dict:
            nonlocal runner_exit, capture_exit, output_root
            directory = evidence / ("invocation-" + name)
            directory.mkdir()
            events = directory / "events.jsonl"
            raw_events = private / (name + ".events.jsonl")
            command = [bazel, *startup, args[0], *options, *args[1:]]
            report.write_json(directory / "working-directory.json", str(cwd.relative_to(workspace)))
            if kind != "command":
                command += ["--build_event_json_file=" + str(raw_events)]
            report.write_json(directory / "command.json", command)
            (directory / "arguments.txt").write_text(json.dumps(command[1:]) if kind == "command" else "\n".join(command[1:]) + "\n")
            (directory / "expected-targets.txt").write_text("".join(label + "\n" for label in targets))
            for name, git_args in (("commit.txt", ["cat-file", "commit", SOURCE]), ("before.txt", ["status", "--porcelain", "--untracked-files=no"])):
                (directory / name).write_bytes(subprocess.check_output(["git", *git_args], timeout=30))
            (directory / "lock-before").write_bytes((workspace / "MODULE.bazel.lock").read_bytes())
            native = None
            captured = 0
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Qualification work deadline exhausted")
                with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
                    native = subprocess.run(["timeout", "--signal=TERM", "--kill-after=5s", str(remaining), *command], stdout=stdout, stderr=stderr, cwd=cwd).returncode
            finally:
                finished = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if native is not None:
                    (directory / "native-exit").write_text(str(native) + "\n")
                runner_exit = runner_exit or (native if native is not None else 1)
                for filename in ("stdout.log", "stderr.log"):
                    path = directory / filename
                    if path.exists():
                        path.write_bytes(clean(path.read_bytes()))
                try:
                    for name, git_args in (("after.txt", ["status", "--porcelain", "--untracked-files=no"]), ("head.txt", ["rev-parse", "HEAD"])):
                        (directory / name).write_bytes(subprocess.check_output(["git", *git_args], timeout=30))
                    (directory / "lock-after").write_bytes((workspace / "MODULE.bazel.lock").read_bytes())
                    if kind != "command":
                        result = report.capture(raw_events, events, output_root, directory / "reports")
                        captured = int(bool(result["errors"]))
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    report.write_json(directory / "capture-error.json", {"error_type": type(error).__name__})
                    captured = 1
                capture_exit = capture_exit or captured
                (directory / "capture-exit").write_text(str(captured) + "\n")
                report.write_json(directory / "context.json", {
                    "expected_source": SOURCE, "command_kind": kind, "execution_finished_at": finished,
                    "commit_file": "commit.txt", "source_before": "before.txt", "source_after": "after.txt",
                    "final_head": "head.txt", "executed_arguments": "arguments.txt", "native_exit": "native-exit",
                    "capture_exit": "capture-exit", "lock_before": "lock-before", "lock_after": "lock-after"})
                if kind == "command":
                    verdict = report.command_verdict(directory / "context.json")
                else:
                    verdict = report.verdict(events, report.manifest_path(events), directory / "context.json", directory / "expected-targets.txt")
                report.write_json(directory / "verdict.json", verdict)
                print(json.dumps({"phase": directory.name, "native_exit": native, "status": verdict["status"]}), flush=True)
            if verdict["status"] != "PASS":
                raise RuntimeError("Qualification phase did not pass: " + directory.name)
            return verdict

        completed = False
        try:
            invoke("version", ["info", "release"], "command", [])
            if (evidence / "invocation-version/stdout.log").read_text().strip() != "release " + (workspace / ".bazelversion").read_text().strip():
                raise ValueError("Native Bazel differs from the source's declared release")
            test_options = ["--strategy=TestRunner=local", "--local_test_jobs=1", "--nocache_test_results",
                            "--runs_per_test=1", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled", "--test_timeout=300"]
            invoke("formatter-output-base", ["info", "output_base"], "command", [])
            output_root = Path((evidence / "invocation-formatter-output-base/stdout.log").read_text().strip())
            invoke("buildifier", ["test", *test_options, "//internal_tools:buildifier.check"], "test", ["//internal_tools:buildifier.check"])
            for context, cwd, package in (("root", workspace, "//tests/kernel_headers:"),
                                          ("configured", workspace / "e2e/kernel_headers", "//:")):
                invoke(context + "-output-base", ["info", "output_base"], "command", [], cwd=cwd)
                output_root = Path((evidence / ("invocation-" + context + "-output-base") / "stdout.log").read_text().strip())
                if not output_root.is_absolute() or not output_root.is_dir():
                    raise ValueError("Invalid native output base")
                targets = [package + name + "_" + kind + "_test" for name in
                           ("gnu", "musl", "unconstrained", "explicit_gnu", "explicit_musl") for kind in ("selection", "compile")]
                if context == "root":
                    targets += [package + "linux_kernel_archs_by_version_test"]
                actions_path = private / (context + "-actions.json")
                result = invoke(context + "-selection", ["test", *test_options,
                                "--execution_log_json_file=" + str(actions_path), *targets], "test", targets, cwd=cwd)
                actions = []
                with actions_path.open() as stream:
                    for _, action in execution_records(stream):
                        if action is not None and action.get("targetLabel", "").startswith(package) and action.get("mnemonic") == "CppCompile":
                            actions.append(action)
                report.write_json(evidence / (context + "-compile-actions.json"), actions)
                wanted = {package + name + "_compile" for name in ("gnu", "musl", "unconstrained", "explicit_gnu", "explicit_musl")}
                if {row["targetLabel"] for row in actions} != wanted or len(actions) != 5:
                    raise ValueError("Missing or duplicate native fixture compile action")
                for action in actions:
                    if action["exitCode"] or action["status"] or not action["remotable"]:
                        raise ValueError("Fixture compile failed or is not remotable")
                    if not action["cacheHit"] and action["runner"] != "remote":
                        raise ValueError("Fixture compilation ran outside remote execution")
                    properties = {row["name"]: row["value"] for row in action["platform"]["properties"]}
                    if not {"Arch": "amd64", "OSFamily": "linux", "workload-isolation-type": "oci",
                            "container-image": "docker://gcr.io/flame-public/rbe-ubuntu22-04@sha256:0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3"}.items() <= properties.items():
                        raise ValueError("Fixture compile selected a different executor platform")
                    source_inputs = [row for row in action["inputs"] if row["path"].endswith("tests/kernel_headers/version_test.c")]
                    source_identity = report.file_record(workspace / "tests/kernel_headers/version_test.c")
                    if len(source_inputs) != 1 or source_inputs[0]["digest"]["hash"] != source_identity["sha256"] or int(source_inputs[0]["digest"]["sizeBytes"]) != source_identity["bytes"]:
                        raise ValueError("Fixture compile omitted its declared version assertion source")
                phase = evidence / ("invocation-" + context + "-selection")
                manifest = report.read_json(report.manifest_path(phase / "events.jsonl"))
                xmls = [row for row in manifest["reports"] if row["name"] == "test.xml"]
                if len(xmls) != len(targets) or len(result["executed"]) != len(targets):
                    raise ValueError("Expected one uncached attempt and XML per selection owner")
                for row in xmls:
                    cases = list(ET.parse(phase / row["path"]).getroot().iter("testcase"))
                    if not cases or any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
                        raise ValueError("Missing positive selection or compile case")
            invoke("configuration-errors", ["run", "//tools/uapi_qualification:configuration_errors", "--", *startup], "command", [])
            negative = (evidence / "invocation-configuration-errors/stdout.log").read_text()
            matches = re.findall(r"^(duplicate|empty|unknown|nonroot): exit ([1-9][0-9]*): (.+)$", negative, re.MULTILINE)
            if [row[0] for row in matches] != ["duplicate", "empty", "unknown", "nonroot"]:
                raise ValueError("Expected all four native extension error diagnostics")
            report.write_json(evidence / "configuration-errors.json", matches)
            completed = True
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
            runner_exit = runner_exit or 1
            report.write_json(evidence / "qualification-error.json", {"type": type(error).__name__, "message": clean(str(error).encode()).decode()})
        finally:
            if not completed:
                runner_exit = runner_exit or 1
            # Scan uncompressed evidence before it enters the sole artifact.
            credential_artifacts.verify(auth, evidence)
            def traversal_error(error):
                raise error
            for parent, _, names in os.walk(evidence, onerror=traversal_error):
                for name in names:
                    if any(value in (Path(parent) / name).read_bytes() for value in secrets):
                        raise ValueError("Credential in evidence; publication withheld")
            result = report.bundle(evidence, runner_exit, capture_exit, artifacts / "qualification.tar.gz")
            print(json.dumps({"status": result["status"], "native_exit": result["native_exit"],
                              "artifact": report.file_record(artifacts / "qualification.tar.gz")}), flush=True)
            runner_exit = runner_exit or result["native_exit"] or int(result["status"] != "PASS")
    return runner_exit


if __name__ == "__main__":
    raise SystemExit(main())
