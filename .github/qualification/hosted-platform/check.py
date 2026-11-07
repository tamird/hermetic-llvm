"""Check input platform, default files and executable views on two Bazel releases."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
from urllib.parse import unquote, urlparse

SRC = Path.cwd() / ".github/qualification/hosted-platform"
WORK = Path(os.environ["RUNNER_TEMP"]) / "hosted-platform"
FIX = "51d740df115b817c17df9bfb3af3e61e820788eb"
LOCAL = ["--lockfile_mode=off","--remote_cache=","--remote_executor=","--bes_backend=","--jobs=3"]
OVERRIDE = 'single_version_override(module_name="with_cfg.bzl", version="0.12.0", patches=["//{pkg}:with_cfg_keep.patch"], patch_strip=1)\n'
REPORT = {"stock_original":{"run":42421069505,"artifact":13593630898,"sha256":"e293d245c48f981051c0aaaa55b638d243a6a13582810281a4ef4211e1576c33"}, "candidate":FIX, "supplier":"with_cfg.bzl@0.12.0", "cells":{}, "errors":[]}


def save():
    (WORK / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def bazel(version, wd, row, args):
    argv = [WORK / version, "--ignore_all_rc_files", "--host_jvm_args=-Xmx3500m", *args]
    result = subprocess.run(argv, cwd=wd, capture_output=True, text=True)
    if result.returncode:
        lines = result.stderr.splitlines()
        starts = [i for i,s in enumerate(lines) if re.search(r"ERROR:|fatal:|error:",s)][:5]
        indexes = sorted({n for i in starts for n in range(i,min(i+7,len(lines)))} | set(range(max(0,len(lines)-4),len(lines))))
        row["errors"] = [re.sub(r"https?://\S+", "<public-url>", lines[i]).replace(str(Path.home()),"<home>").replace(str(wd),"<workspace>")[:620] for i in indexes]
        save()
        raise RuntimeError(f"{args[0]} exit {result.returncode}")
    return result.stdout


def files(bep, labels):
    sets, targets = {}, {}
    for line in bep.open():
        e = json.loads(line)
        if "namedSetOfFiles" in e:
            sets[e["id"]["namedSet"]["id"]] = e["namedSetOfFiles"]
        target = e["id"].get("targetCompleted", {})
        if target.get("label") in labels and not target.get("aspect") and e.get("completed", {}).get("success"):
            group, = [g for g in e["completed"]["outputGroup"] if g["name"] == "default"]
            targets[target["label"]] = [f["id"] for f in group["fileSets"]]
    assert set(targets) == set(labels), f"missing configured default groups: {sorted(set(labels) - targets.keys())}"
    outputs = {}
    for label,pending in targets.items():
        seen, result = set(), {}
        while pending:
            key = pending.pop()
            if key in seen:
                continue
            seen.add(key)
            group = sets[key]
            pending.extend(f["id"] for f in group.get("fileSets", []))
            for f in group.get("files", []):
                uri = urlparse(f["uri"])
                assert uri.scheme == "file" and not uri.netloc
                path = Path(unquote(uri.path))
                assert path.stat().st_size < 80 << 20
                data = path.read_bytes()
                h = hashlib.sha256(data).hexdigest()
                assert not f.get("digest") or f["digest"] == h
                assert len(data) == int(f.get("length",len(data)))
                result[path] = h
        assert result
        outputs[label] = result
    return outputs


def fixture(version, patched, target, row):
    wd = WORK / version.replace("bazel","fixture")
    wd.mkdir(exist_ok=True)
    for name in ("defs.bzl","probe.bzl","with_cfg_keep.patch"):
        shutil.copyfile(SRC / name, wd / name)
    (wd / "BUILD.bazel").write_text((SRC / "fixture.BUILD").read_text() + '\nexports_files(["with_cfg_keep.patch"])\n')
    (wd / "MODULE.bazel").write_text('module(name="conditional_setting_fixture")\n'
        'bazel_dep(name="platforms", version="0.0.11")\nbazel_dep(name="with_cfg.bzl",version="0.12.0")\n'
        + (OVERRIDE.format(pkg="") if patched else ""))
    (wd / "mode.bzl").write_text("PATCHED = " + str(patched) + "\n")
    labels = ["//:naive_files"] + (["//:conditional_files","//:rule_files"] if patched else [])
    bep = WORK / (row["name"] + ".bep")
    bazel(version, wd, row, ["build",*LOCAL,"--enable_bzlmod","--host_platform=//:linux_glibc","--platforms=//:" + target,
                            "--build_event_json_file=" + str(bep),*labels])
    row["views"] = {}
    for label, paths in files(bep, labels).items():
        rows = [{"name":p.name, "value":p.read_text().strip(), "sha256":h} for p,h in paths.items()]
        row["views"][label] = rows
        save()
        if target == "linux_glibc":
            assert all(r["value"] == "musl opt" for r in rows), f"{label} did not select Linux musl: {rows}"
        elif label == "//:naive_files":
            assert all(r["value"] == "fallback opt" and r["name"].endswith(".tool") for r in rows), f"ordinary set([]) returned {rows}"
        else:
            assert all(r["value"] == "custom-msvc opt" and r["name"].endswith(".exe") for r in rows), f"{label} lost custom Windows input: {rows}"


def prepare_actual(wd):
    pkg = wd / "hosted_platform_qual"
    pkg.mkdir()
    shutil.copyfile(SRC / "with_cfg_keep.patch", pkg / "with_cfg_keep.patch")
    (pkg / "contract.bzl").write_text('''def _impl(ctx):
    return [DefaultInfo(files = depset([ctx.file.tool, ctx.executable.tool]))]
tool_files = rule(implementation = _impl, attrs = {"tool": attr.label(allow_single_file = True, cfg = "target", executable = True)})
''')
    (pkg / "BUILD.bazel").write_text('''load(":contract.bzl","tool_files")
exports_files(["with_cfg_keep.patch"])
constraint_setting(name="extra")
constraint_value(name="custom", constraint_setting=":extra")
platform(name="custom_windows", parents=["@llvm//platforms:windows_x86_64_msvc"], constraint_values=[":custom"])
tool_files(name="module_map",tool="@llvm//tools/internal:module-map-generator")
''')
    with (wd / "MODULE.bazel").open("a") as f:
        f.write("\n" + OVERRIDE.format(pkg=pkg.name))
    (WORK / "owner.star").write_text('''def format(target):
    opts = build_options(target)
    return json.encode([str(target.label), [str(p) for p in opts["//command_line_option:platforms"]],
                        [v for k,v in opts.items() if str(k).endswith("//toolchain:runtime_stage")],
                        [f.path for f in target.files.to_list()]])
''')


def actual(wd, kind, row):
    linux = "@llvm//platforms:linux_x86_64_gnu.2.28"
    custom = "//hosted_platform_qual:custom_windows"
    label = "//hosted_platform_qual:module_map"
    target = custom if kind == "custom-msvc" else linux
    flags = [*LOCAL,"-c","opt","--repo_env=BAZEL_DO_NOT_DETECT_CPP_TOOLCHAIN=1","--repo_env=BAZEL_MSVC_RUNTIME_VISUAL_STUDIO_EULA=1",
             "--repo_env=BAZEL_WINDOWS_SDK_EULA=1","--experimental_cc_static_library","--host_platform="+linux,"--platforms="+target]
    bep = WORK / (kind + ".bep")
    if kind == "msvc-consumer":
        owner = "//:main_windows_x86_64_msvc_external_include_paths"
        bazel("bazel9",wd,row,["build",*flags,"--nobuild","--build_event_json_file="+str(bep),owner])
        events = (json.loads(line) for line in bep.open())
        assert any(e["id"].get("targetCompleted",{}).get("label") == owner and (e.get("completed",{}).get("success")
                   or e.get("aborted",{}).get("reason") == "NO_BUILD") for e in events)
        row.update(owner=owner,scope="analysis")
        return
    query = 'attr("srcs", "module_map_generator[.]c", deps(' + label + "))"
    public = set(bazel("bazel9",wd,row,["cquery",*flags,"--output=files",label]).splitlines())
    stdout = bazel("bazel9",wd,row,["cquery",*flags,"--output=starlark","--starlark:file="+str(WORK / "owner.star"),query])
    candidates = [json.loads(s) for s in stdout.splitlines() if s.strip()]
    owners = [r for r in candidates if set(r[3]) & public]
    row.update(owners=owners,input=target,public_files=sorted(public),other_configured_rows=len(candidates)-len(owners))
    save()
    expected = "//platforms:linux_x86_64_musl" if target == linux else custom
    assert owners and all(len(p) == 1 and p[0].endswith(expected) and stage == ["stage1_hosted"] and len(out) == 1 for _,p,stage,out in owners), f"expected {expected} and stage1_hosted; public file and configured owner details recorded"
    if kind == "custom-msvc":
        assert all(f.endswith(".exe") for _,_,_,out in owners for f in out)
        return
    bazel("bazel9",wd,row,["build",*flags,"--build_event_json_file="+str(bep),label])
    paths = files(bep,[label])[label]
    assert len(set(paths.values())) == 1
    path = next(iter(paths))
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H",data,18)[0] == 62
    assert " INTERP " not in subprocess.check_output(["readelf","-lW",path],text=True)
    assert "(NEEDED)" not in subprocess.check_output(["readelf","-dW",path],text=True)
    result = subprocess.run([path],capture_output=True,text=True)
    assert result.returncode == 2 and "<output> @<params>" in result.stderr
    row["binary"] = {"sha256":paths[path],"bytes":len(data),"machine":62,"usage_exit":result.returncode}


def cell(name, fn, *args):
    row = REPORT["cells"][name] = {"name":name,"verified":False}
    save()
    try:
        fn(*args,row)
        row["verified"] = True
    except Exception as error:
        REPORT["errors"].append(f"{name}: {type(error).__name__}: {error}"[:350])
    save()


if __name__ == "__main__":
    assert subprocess.check_output(["git","rev-parse","HEAD^"],text=True).strip() == FIX
    REPORT.update(registry_archive_sha256="23c39c6dfca14dbd1607025c736c8bf4843f31958c0a87fbdb2c7d206585fea4",
                  patch_sha256=hashlib.sha256((SRC / "with_cfg_keep.patch").read_bytes()).hexdigest())
    assert REPORT["patch_sha256"] == "2ce62c5083b20570fe7b1a68bd6ecedce6a668b66b9f1d5d33f10e20aebc3e73"
    for version in ("bazel7","bazel9"):
        for plat in ("linux_glibc","windows_custom"):
            cell(f"{version}-conditional-{plat}",fixture,version,True,plat)
        subprocess.run([WORK/version,"--ignore_all_rc_files","--host_jvm_args=-Xmx3500m","shutdown"],
                       cwd=WORK/version.replace("bazel","fixture"))
    wd = Path.cwd() / "e2e/cross_compilation"
    prepare_actual(wd)
    for kind in ("custom-msvc","msvc-consumer","linux"):
        cell("actual-"+kind,actual,wd,kind)
    REPORT["verified"] = len(REPORT["cells"]) == 7 and not REPORT["errors"]
    save()
    raise SystemExit(not REPORT["verified"])
