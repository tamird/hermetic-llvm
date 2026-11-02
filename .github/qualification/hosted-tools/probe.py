"""Exercise the installed execution tools on a native Linux host without a loader."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

# In the same order as the declared tool arguments in BUILD.bazel.
TOOLS = {
    "module-map-generator": (2, "<output> @<params>"),
    "header-parser": (2, "header_parser: required env var PARSE_HEADER is not set"),
    "static-library-validator": (2, "usage: static_library_validator <static_library> <stamp_path>"),
    "link-wrapper-clang++": (127, "link-wrapper: required env var LLVM_CLANGXX is not set"),
    "def_file_generator": (1, "Usage: output_def_file dllname"),
    "case_insensitive_vfs": (2, "-output is required"),
}


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def chroot(root, program, *args, **env):
    invocation = ["sudo", "-n", "env", "-i", "LC_ALL=C"]
    invocation.extend(f"{k}={v}" for k, v in env.items())
    invocation.extend(["/usr/sbin/chroot", "--userspec=+65534:+65534", str(root), "/" + program, *args])
    return subprocess.run(invocation, capture_output=True, text=True, timeout=45)


def main():
    if not __debug__:
        raise RuntimeError("probe requires checked assertions")
    [arm] = sys.argv[1:]
    assert arm in {"stock", "fixed"}
    root = Path(os.environ["TEST_TMPDIR"]) / "installed"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    machine = platform.machine()
    expected_machine = {"aarch64": "AArch64", "x86_64": "Advanced Micro Devices X86-64"}[machine]
    receipt = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "probe.json"
    facts = {"arm": arm, "machine": machine, "tools": {}, "operations": {}}

    def save():
        receipt.write_text(json.dumps(facts, indent=2) + "\n")

    save()
    for i, name in enumerate([*TOOLS, "clang++", "llvm-nm"]):
        paths = os.environ[f"HOSTED_TOOL_{i}"].split()
        assert paths
        # Match the supplier's own exec_test CLI fixtures: the first file is
        # the executable, and the remaining rootpaths may include runfiles.
        path = paths[0]
        source = Path(path)
        if name == "link-wrapper-clang++":
            assert source.name == name, source.name
        installed = root / name
        shutil.copyfile(source, installed)
        installed.chmod(0o555)
        elf = subprocess.check_output(["readelf", "-hlWd", installed], text=True)
        machine_match = re.search(r"^\s*Machine:\s*(.*?)\s*$", elf, re.M)
        assert machine_match and machine_match[1] == expected_machine
        interp = re.search(r"Requesting program interpreter: (.*?)\]", elf)
        libs = re.findall(r"\(NEEDED\).*?\[(.*?)\]", elf)
        facts["tools"][name] = {"runfile": path, "bytes": source.stat().st_size,
                                "sha256": sha(installed), "interpreter": interp[1] if interp else None,
                                "needed": libs}
        if i < len(TOOLS):
            assert len(paths) == 1
            default = Path(os.environ[f"HOSTED_DEFAULT_{i}"])
            default_hash = sha(default)
            facts["tools"][name].update(default_file=str(default), default_sha256=default_hash)
            assert default_hash == facts["tools"][name]["sha256"], name
        save()

    # This root starts with just the eight recorded ELFs. No interpreter, libc,
    # shell or worker /etc files can satisfy a host dependency.
    assert sorted(p.name for p in root.iterdir()) == sorted(facts["tools"])
    for name, (code, signature) in TOOLS.items():
        row = facts["tools"][name]
        proc = chroot(root, name)
        row.update(exit=proc.returncode, diagnostic=proc.stderr.strip()[:400])
        if row["interpreter"]:
            assert not (root / row["interpreter"].lstrip("/")).exists()
            row["starts"] = False
            row["verified"] = (arm == "stock" and proc.returncode == 127 and
                               "No such file or directory" in proc.stderr)
        else:
            row["starts"] = proc.returncode == code and signature in proc.stderr
            row["verified"] = not row["needed"] and row["starts"]
        save()
    assert all(facts["tools"][k]["verified"] for k in TOOLS)
    assert facts["tools"]["module-map-generator"]["starts"]
    if arm == "stock":
        assert not facts["tools"]["link-wrapper-clang++"]["starts"]
        facts["verified"] = True
        save()
        return

    assert all(facts["tools"][k]["starts"] for k in TOOLS)
    out = root / "out"
    out.mkdir()
    out.chmod(0o777)
    (root / "tmp").mkdir()
    (root / "tmp").chmod(0o1777)
    (root / "sdk").mkdir()
    (root / "sdk").chmod(0o755)
    (root / "sdk/Header.h").write_text("int example;\n")
    (root / "params").write_text("/sdk/Header.h\n")
    (root / "input.def").write_text("EXPORTS\nexample\n")
    (root / "empty.a").write_bytes(b"!<arch>\n")
    for path in ("sdk/Header.h", "params", "input.def", "empty.a"):
        (root / path).chmod(0o644)

    cases = [
        ("module-map-generator", ["/out/map", "@/params"], {}, "map", 'textual header "/sdk/Header.h" { size 13 }'),
        ("header-parser", ["-fsyntax-only", "-x", "c", "/sdk/Header.h"], {"PARSE_HEADER": "/out/header", "LLVM_CLANGXX": "/clang++"}, "header", ""),
        ("static-library-validator", ["/empty.a", "/out/archive"], {"LLVM_NM": "/llvm-nm"}, "archive", ""),
        ("link-wrapper-clang++", ["--version"], {"LLVM_CLANGXX": "/clang++"}, None, "clang version"),
        ("def_file_generator", ["/out/exports", "example.dll", "/input.def"], {}, "exports", "\texample\n"),
        ("case_insensitive_vfs", ["-root", "/sdk", "-output", "/out/overlay"], {}, "overlay", '"name": "Header.h"'),
    ]
    for name, args, env, output, marker in cases:
        proc = chroot(root, name, *args, **env)
        actual = (out / output).read_bytes() if output and (out / output).exists() else None
        verified = (proc.returncode == 0 and (actual is not None if output else True) and
                    marker in (actual.decode() if actual is not None else proc.stdout))
        facts["operations"][name] = {"argv": args, "env": env, "exit": proc.returncode,
                                     "stderr": proc.stderr.strip()[:400], "verified": verified,
                                     "sha256": hashlib.sha256(actual if actual is not None else proc.stdout.encode()).hexdigest()}
        save()
    assert all(row["verified"] for row in facts["operations"].values()), facts["operations"]
    facts["verified"] = True
    save()


if __name__ == "__main__":
    main()
