"""Check the three real LLVM consumers against the exact proposed supplier."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import struct
from urllib.parse import unquote, urlparse

WORK = Path(os.environ["RUNNER_TEMP"]) / "hosted-platform"
FIX = "c0768cac2fd3870b91d2dc4db9e2659f4c09ed45"
SUPPLIER = "01433cd771f82130524fd1a867fa995004b55ae3"
SUPPLIER_TREE = "b9ae482808bcdc1c3d4a8ef571b4d61964c3ce8c"
LOCAL = ["--lockfile_mode=off","--remote_cache=","--remote_executor=","--bes_backend=","--jobs=3"]
REPORT = {"consumer": FIX, "supplier": SUPPLIER, "supplier_tree": SUPPLIER_TREE,
          "previous_consumer_carrier": "255bd2e77", "cells": {}, "errors": []}


def save():
    (WORK / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def bazel(version, wd, row, args):
    argv = [WORK / version, "--ignore_all_rc_files", "--host_jvm_args=-Xmx3500m", *args]
    result = subprocess.run(argv, cwd=wd, capture_output=True, text=True)
    warning = "--platforms only supports a single target platform"
    row.setdefault("multiple_platform_warnings", 0)
    row["multiple_platform_warnings"] += result.stderr.count(warning)
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


def prepare_actual(wd):
    pkg = wd / "hosted_platform_qual"
    pkg.mkdir()
    (pkg / "contract.bzl").write_text('''def _impl(ctx):
    return [DefaultInfo(files = depset([ctx.file.tool, ctx.executable.tool]))]
tool_files = rule(implementation = _impl, attrs = {"tool": attr.label(allow_single_file = True, cfg = "target", executable = True)})
''')
    (pkg / "BUILD.bazel").write_text('''load(":contract.bzl","tool_files")
constraint_setting(name="extra")
constraint_value(name="custom", constraint_setting=":extra")
platform(name="custom_windows", parents=["@llvm//platforms:windows_x86_64_msvc"], constraint_values=[":custom"])
tool_files(name="module_map",tool="@llvm//tools/internal:module-map-generator")
''')
    with (wd / "MODULE.bazel").open("a") as f:
        f.write('\nlocal_path_override(module_name = "with_cfg.bzl", path = %s)\n' %
                json.dumps(str(WORK / "supplier")))
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
        configured = bazel("bazel9",wd,row,["cquery",*flags,"--output=files",owner]).splitlines()
        assert configured
        row.update(owner=owner,scope="analysis",configured_outputs=configured)
        return
    query = 'attr("srcs", "module_map_generator[.]c", deps(' + label + "))"
    public = set(bazel("bazel9",wd,row,["cquery",*flags,"--output=files",label]).splitlines())
    stdout = bazel("bazel9",wd,row,["cquery",*flags,"--output=starlark","--starlark:file="+str(WORK / "owner.star"),query])
    candidates = [json.loads(s) for s in stdout.splitlines() if s.strip()]
    owners = [r for r in candidates if set(r[3]) & public]
    row.update(owners=owners,input=target,public_files=sorted(public),other_configured_rows=len(candidates)-len(owners))
    save()
    expected = "//platforms:linux_x86_64_musl" if target == linux else custom
    assert owners, f"no configured owner for {public}"
    for _, platforms, stages, outputs in owners:
        assert platforms and platforms[0].endswith(expected), platforms
        assert stages == ["stage1_hosted"] and len(outputs) == 1
        if len(platforms) > 1:
            assert kind == "linux" and len(platforms) == 2
            assert platforms[1].endswith("//platforms:linux_x86_64_gnu.2.28")
    if kind == "custom-msvc":
        assert all(f.endswith(".exe") for _,_,_,out in owners for f in out)
        return
    bazel("bazel9",wd,row,["build",*flags,"--build_event_publish_all_actions","--build_event_json_file="+str(bep),label])
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
    row["link_actions"] = []
    for line in bep.open():
        event = json.loads(line)
        action = event.get("action", {})
        identity = event["id"].get("actionCompleted", {})
        if action.get("type") == "CppLink" and identity.get("primaryOutput") in {p for owner in owners for p in owner[3]}:
            row["link_actions"].append({"owner": identity.get("label"), "output": identity["primaryOutput"],
                                        "config": identity.get("configuration", {}).get("id"), "success": action.get("success")})
    save()
    # Exercise the same two inputs used by the project's textual_headers_test.
    scratch = WORK / "headers"
    scratch.mkdir()
    headers = {"system.h": b'#include "nested.h"\n', "nested.h": b'typedef int system_int;\n'}
    expected_map = 'module "crosstool" [system] {\n'
    for name, raw in headers.items():
        (scratch / name).write_bytes(raw)
        expected_map += f'  module "{name}" {{\n    textual header "{name}" {{ size {len(raw)} }}\n  }}\n'
    expected_map += '}\n'
    (scratch / "headers.params").write_text("\n".join(headers) + "\n")
    subprocess.run([path, "crosstool.modulemap", "@headers.params"], cwd=scratch, check=True)
    output = (scratch / "crosstool.modulemap").read_bytes()
    row["module_map"] = {"sha256": hashlib.sha256(output).hexdigest(), "text": output.decode(),
                         "headers": {name: len(raw) for name, raw in headers.items()}}
    assert output == expected_map.encode()


def check_supplier(wd):
    supplier = WORK / "supplier"
    def rev(key):
        return subprocess.check_output(["git", "-C", supplier, "rev-parse", key], text=True).strip()
    assert rev("HEAD") == SUPPLIER and rev("HEAD^{tree}") == SUPPLIER_TREE
    assert rev("HEAD^") == "9b8b10962419346889e528cbe57a539754489d57"
    assert not subprocess.check_output(["git", "-C", supplier, "status", "--porcelain", "-uall"], text=True)
    row = REPORT["resolved_supplier"] = {}
    location = bazel("bazel9", wd, row, ["query", "--lockfile_mode=off", "--output=location", "@with_cfg.bzl//:with_cfg"]).strip()
    match = re.match(r"^(.+/BUILD[.]bazel):[0-9]+:[0-9]+:", location)
    assert match, location
    directory = (wd / match[1]).parent
    for name in ("MODULE.bazel", "with_cfg.bzl", "with_cfg/private/builder.bzl", "with_cfg/private/transition.bzl"):
        raw = (directory / name).read_bytes()
        assert raw == (supplier / name).read_bytes()
        row[name] = hashlib.sha256(raw).hexdigest()
    row["verified"] = True
    save()


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
    wd = Path.cwd() / "e2e/cross_compilation"
    prepare_actual(wd)
    try:
        check_supplier(wd)
    except Exception as error:
        REPORT["errors"].append(f"supplier: {type(error).__name__}: {error}"[:350])
        save()
    for kind in ("custom-msvc","msvc-consumer","linux"):
        cell("actual-"+kind,actual,wd,kind)
    REPORT["verified"] = len(REPORT["cells"]) == 3 and not REPORT["errors"]
    save()
    raise SystemExit(not REPORT["verified"])
