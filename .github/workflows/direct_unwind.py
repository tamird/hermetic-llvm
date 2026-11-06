"""Verify that each compiler search directory contains its real target archive."""

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

import static_unwind as native

SOURCE = "1273f916be8c7109542be8fef009258a56369462"
ROOTS = ["//runtimes/libunwind:libunwind.static", "//runtimes/libunwind:libunwind_library_search_directory"]


def archive(arch: str, machine: int, row: dict) -> None:
    platform = "//platforms:linux_" + arch + "_glibc_static"
    flags = ["--platforms=" + platform, "--extra_toolchains=//toolchain:all", "--jobs=4", "--lockfile_mode=off"]
    subprocess.run([*native.BAZEL, "build", *flags, *ROOTS], check=True, timeout=1500)
    outputs = subprocess.check_output([*native.BAZEL, "cquery", *flags, "--output=files",
                                      "set(" + " ".join(ROOTS) + ")"], text=True, timeout=300)
    paths = [Path(s) for s in outputs.splitlines()]
    assert all(not p.is_absolute() and p.parts[0] == "bazel-out" and ".." not in p.parts for p in paths), paths
    original, = [p for p in paths if p.is_file()]
    directory, = [p for p in paths if p.is_dir()]
    installed = directory / "libunwind.a"
    row["original"] = str(original)
    row["directory_contents"] = sorted(str(f.relative_to(directory)) for f in directory.rglob("*"))
    row["original_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
    row["installed_sha256"] = hashlib.sha256(installed.read_bytes()).hexdigest()
    assert row["original_sha256"] == row["installed_sha256"]
    members = subprocess.check_output(["ar", "t", installed], text=True).splitlines()
    assert members, "empty runtime archive"
    machines = set()
    for member in members:
        data = subprocess.check_output(["ar", "p", installed, member])
        assert data[:6] == b"\x7fELF\x02\x01", (arch, member)
        machines.add(struct.unpack_from("<H", data, 18)[0])
    row["member_count"] = len(members)
    row["elf_machines"] = sorted(machines)
    assert machines == {machine}, machines

    missing = {"_Unwind_ForcedUnwind", "_Unwind_GetCFA", "_Unwind_GetIP", "_Unwind_GetLanguageSpecificData",
               "_Unwind_GetRegionStart", "_Unwind_Resume", "_Unwind_SetGR", "_Unwind_SetIP"}
    symbols = subprocess.check_output(["readelf", "--wide", "--symbols", installed], text=True).splitlines()
    defined = {parts[7] for line in symbols if len(parts := line.split()) >= 8
               and parts[4] in ("GLOBAL", "WEAK") and parts[6] != "UND"}
    row["resolved_symbols"] = sorted(missing & defined)
    assert missing <= defined, missing - defined


def main() -> None:
    r = {"source": SOURCE, "bazel_version": subprocess.check_output(["bazel", "--version"], text=True).strip()}
    out = native.OUT / "report.json"
    try:
        assert r["bazel_version"] == "bazel 9.2.0" and native.git("rev-parse", "HEAD") == SOURCE
        for arch, machine in (("x86_64", 62), ("aarch64", 183)):
            row = r[arch] = {}
            try:
                archive(arch, machine, row)
                row["passed"] = True
            except (AssertionError, OSError, ValueError, subprocess.SubprocessError) as err:
                row["error"] = type(err).__name__
            out.write_text(json.dumps(r, indent=2) + "\n")
            if arch == "x86_64":
                r["native_test"] = native.measure("native-cancellation")
                out.write_text(json.dumps(r, indent=2) + "\n")
        test = r["native_test"]
        r["passed"] = (r["x86_64"].get("passed", False) and r["aarch64"].get("passed", False)
                       and test["exit"] == 0 and test["test_results"] == ["PASSED"]
                       and bool(test["links"]) and all(a["success"] for a in test["links"]))
    finally:
        out.write_text(json.dumps(r, indent=2) + "\n")
    sys.exit(not r.get("passed", False))


if __name__ == "__main__":
    main()
