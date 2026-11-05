"""Generate the resolver manifest from GNU and qualify only its Bazel artifact."""
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import traceback

ARCH = os.environ["ARCH"]
MACHINE = {"x86_64": 62, "aarch64": 183}[ARCH]
ROOT = Path("3rd_party/libc/glibc/static")
GENERATED = ROOT / ARCH
D = Path(os.environ["RUNNER_TEMP"]) / ("resolver-" + ARCH)
PUBLIC = D / "public"
SOURCE = "a60470fd45eddfcc0635dfccf2f227fa61d266a5"
PREVIOUS = "6f070847add9ac9bf0d19c96dee873e84f9594d9"
GNU = "37f600f2bef3c5e8300147059568b2a2e40a7ad6ccc65ce942556d49429cc667"
LIB = "libresolv.a"
R = {"status":"INCOMPLETE", "product":SOURCE, "base":PREVIOUS, "carrier":os.environ["CARRIER"],
     "arch":ARCH, "host_arch":os.uname().machine, "commands":{}}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(phase):
    R["phase"] = phase
    (PUBLIC / "receipt.json").write_text(json.dumps(R, indent=2) + "\n")


def read(argv, **kwargs):
    return subprocess.check_output(list(map(str, argv)), **kwargs)


def run(label, argv, env=None):
    save(label)
    log = D / (label + ".log")
    with log.open("wb") as stream:
        rc = subprocess.run(list(map(str, argv)), stdout=stream, stderr=subprocess.STDOUT, env=env).returncode
    R["commands"][label] = {"exit":rc, "bytes":log.stat().st_size}
    if rc:
        # Raw reference Make output is many MB; retain the first failure and
        # bounded final diagnostics without publishing a toolchain trace.
        logs = [log]
        if label == "gnu_reference":
            logs += [D / "reference" / ("build-" + ARCH) / name for name in ("configure.log","make.log")]
        hints = []
        for f in logs:
            if not f.is_file(): continue
            with f.open(errors="replace") as lines:
                for idx,line in enumerate(lines, 1):
                    if re.search(r"(?i)error:|fatal:|error [1-9]|no rule to make|not found|no such file|no space left|unable to|checksum|not declared|not visible|permission denied",line):
                        line=re.sub(r"(?:https?://|/home/|/tmp/)[^\s]+","<location>",line.strip())
                        if not re.search(r"(?i)credential|authorization|cookie|password|token|gh[pours]_",line):
                            hints.append([f.name,idx,line[:350]])
                        if len(hints)>=16: break
            if len(hints)>=16: break
        R["error_lines"]=hints
        save(label)
        raise RuntimeError(label)
    save(label)


def manifest(raw):
    env = {}
    for s in ast.parse(raw).body:
        if isinstance(s, ast.Assign): env[s.targets[0].id]=ast.literal_eval(s.value)
    return env


def objects(m):
    result = {}
    for path,source,sub,idx in m["OBJECTS"]:
        raw=[]
        for n in m["FLAG_SETS"][idx]:
            val=m["FLAGS"][n]
            raw.extend(m["INCLUDES"] if val=="@INCLUDES" else [val])
        result[path]=(source,sub,tuple(raw))
    return result


def archive(path, tool):
    members=read([tool/"llvm-ar","t",path]).decode().splitlines()
    assert members and len(members)==len(set(members))
    symbols={}
    objects={}
    for member in members:
        raw=read([tool/"llvm-ar","p",path,member])
        assert raw.startswith(b"\x7fELF\x02\x01") and struct.unpack_from("<HH",raw,16)==(1,MACHINE)
        text=read([tool/"llvm-nm","--defined-only","--extern-only","--format=posix","-"],input=raw).decode()
        syms=[]
        for line in text.splitlines():
            if not line.strip(): continue
            parts=line.split()
            assert len(parts)>=2 and len(parts[1])==1
            syms.append((parts[0],parts[1]))
        symbols[member]=sorted(syms)
        objects[member]=sha(raw)
    assert sum(map(len,symbols.values()))>0
    return members,symbols,objects


def main():
    assert os.uname().machine=="x86_64" and read(["git","rev-parse","HEAD"]).decode().strip()==SOURCE
    assert Path(".bazelversion").read_text().strip()=="9.2.0"
    assert GNU in Path("runtimes/glibc/extension/glibc.bzl").read_text()
    assert LIB in (ROOT/"gen_manifest.py").read_text()
    run("bazel_fetch_compiler",["bazel","fetch","--repo=@llvm-toolchain-minimal-linux-amd64"])
    output=Path(read(["bazel","info","output_base"]).decode().strip())
    dirs = [p for p in (output/"external").glob("*llvm-toolchain-minimal-linux-amd64") if (p/"bin/clang").is_file()]
    toolroot,=dirs
    tool=(toolroot/"bin").resolve()
    R["compiler"]=read([tool/"clang","--version"]).decode().splitlines()[0]
    ref=D/"reference";ref.mkdir()
    src=ref/"glibc-2.44"; tar=D/"glibc-2.44.tar.xz"
    for pos,url in enumerate((
            "https://mirrors.kernel.org/gnu/glibc/glibc-2.44.tar.xz",
            "https://ftp.gnu.org/gnu/glibc/glibc-2.44.tar.xz",
            "https://ftpmirror.gnu.org/glibc/glibc-2.44.tar.xz",
            "https://mirror.bazel.build/ftp.gnu.org/gnu/glibc/glibc-2.44.tar.xz")):
        cmd=["curl","--fail","--location","--silent","--show-error","--connect-timeout","10","--max-time","100",
             "--speed-limit","4096","--speed-time","16",url,"-o",str(tar)]
        result=subprocess.run(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        R.setdefault("source_attempts",[]).append({"url":url,"exit":result.returncode})
        save("gnu_download")
        if result.returncode==0:
            valid=sha(tar.read_bytes())==GNU
            R["source_attempts"][-1]["digest_matches"]=valid
            save("gnu_download")
            if valid: break
    else:
        raise OSError("no GNU source mirror returned the pinned archive")
    local_flags=["--remote_cache=","--experimental_remote_downloader=","--remote_executor=","--distdir="+str(D)]
    run("check_bazel_configuration",["bazel","info",*local_flags,"execution_root"])
    run("extract_gnu",["tar","-xf",tar,"-C",ref])
    # A mirror is preferable on shared GitHub addresses. The unmodified
    # canonical script sees the same official Ubuntu reference by its tag.
    ret=subprocess.run(["docker","pull","mirror.gcr.io/library/ubuntu:24.04"],stdout=subprocess.DEVNULL,stderr=subprocess.STDOUT).returncode
    if not ret: run("tag_reference_image",["docker","tag","mirror.gcr.io/library/ubuntu:24.04","ubuntu:24.04"])
    reference = ROOT/"reference_build.sh"
    if ARCH == "aarch64":
        # Its canonical generation already passed. Use exactly the old
        # configure and native cross compiler; ask GNU for only libresolv.
        # If GNU needs an earlier generation step, let its normal build
        # finish on the same configured tree and record which goal worked.
        text=reference.read_text()
        old='  make -j\\$(nproc) V=1 > make.log 2>&1'
        assert text.count(old)==1
        text=text.replace(old, '  if make -j\\$(nproc) -C /w/glibc-2.44/resolv objdir=/w/build-$arch V=1 /w/build-$arch/resolv/libresolv.a > resolver.log 2>&1; then echo resolver > reference.scope; else make -j\\$(nproc) V=1 > make.log 2>&1; echo full > reference.scope; fi')
        text=text.replace('ls -la libc.a math/libm.a resolv/libresolv.a','ls -la resolv/libresolv.a')
        reference=D/"reference_resolver.sh"
        reference.write_text(text); reference.chmod(0o755)
    run("gnu_reference",[reference,ARCH,toolroot.resolve(),ref])
    R["reference_image"]=json.loads(read(["docker","image","inspect","ubuntu:24.04"]))[0]["Id"]
    b=ref/("build-"+ARCH); lib=b/"resolv"/LIB
    proof=D/"reference.a";shutil.copyfile(lib,proof)
    ref_members,ref_symbols,ref_hash=archive(proof,tool)
    reference_record={"source":GNU,"arch":ARCH,"machine":MACHINE,"compiler":R["compiler"],"reference_image":R["reference_image"],
                      "archive_sha256":sha(proof.read_bytes()),"members":ref_members,"typed_symbols":ref_symbols,"objects":ref_hash}
    (PUBLIC/"gnu-reference.json").write_text(json.dumps(reference_record,sort_keys=True)+"\n")
    R["reference_scope"]=(b/"reference.scope").read_text().strip() if (b/"reference.scope").is_file() else "full"
    assert R["reference_scope"] in ("resolver","full")
    if ARCH=="aarch64":
        R["prior_generation"]={"run":42122964048,"artifact":13458434054,"patch_sha256":"bab72ee036b18dc12307e52a84cf89470af2f9c56f49af32a21600c62e851362"}
        m=manifest((GENERATED/"manifest.bzl").read_text())
        assert [Path(x).name for x in m["ARCHIVES"][LIB]] == ref_members
        unchanged=True
    else:
        old=manifest((GENERATED/"manifest.bzl").read_text())
        run("canonical_generator",[ROOT/"gen_manifest.py",ARCH,src,b,b/"make.log",GENERATED],env=dict(os.environ,AR=str(tool/"llvm-ar")))
        subprocess.run(["git","add","-N","--",GENERATED],check=True)
        diff=read(["git","diff","--binary","--",GENERATED])
        files=read(["git","diff","--name-only","--",GENERATED]).decode().splitlines()
        assert files and all(f.startswith(str(GENERATED)+"/") for f in files)
        assert len(diff)<8500000 and b"PRIVATE KEY" not in diff
        (PUBLIC/"generated.patch").write_bytes(diff)
        R["generated_patch"]={"bytes":len(diff),"sha256":sha(diff),"files":files}
        new=manifest((GENERATED/"manifest.bzl").read_text()); before,after=objects(old),objects(new)
        changed=[p for p in before if before[p]!=after.get(p)]
        R["old_semantic_changes"]=changed[:12]
        R["retained_archives"]={k:old["ARCHIVES"][k]==new["ARCHIVES"].get(k) for k in old["ARCHIVES"]}
        R["retained_crt"]=old["CRT"]==new["CRT"]
        R["resolver_objects"]=new["ARCHIVES"][LIB]
        save("check_generator_scope")
        existing_headers = [f for f in files if "/gen/" in f and subprocess.run(["git", "cat-file", "-e", SOURCE+":"+f], stderr=subprocess.DEVNULL).returncode==0]
        unchanged = R["retained_crt"] and all(R["retained_archives"].values()) and not changed and not existing_headers
        R["preexisting_headers_changed"] = existing_headers
        if not unchanged:
            # Preserve the actual delta between generators on the same reference.
            # This makes a difference in upstream's original host recoverable
            # without running configure/make again, and is never called a pass.
            oldscript=D/"old_generator.py"; oldscript.write_bytes(read(["git","show",PREVIOUS+":"+str(ROOT/"gen_manifest.py")]))
            oldout=D/"old_output"; oldout.mkdir()
            run("same_reference_old_generator",["python3",oldscript,ARCH,src,b,b/"make.log",oldout],env=dict(os.environ,AR=str(tool/"llvm-ar")))
            oldpaths={f.relative_to(oldout) for f in oldout.rglob("*") if f.is_file()}
            newpaths={f.relative_to(GENERATED) for f in GENERATED.rglob("*") if f.is_file()}
            delta=bytearray()
            for short in sorted(oldpaths|newpaths):
                oldfile, newfile = oldout/short, GENERATED/short
                oldbytes = oldfile.read_bytes() if short in oldpaths else b""
                newbytes = newfile.read_bytes() if short in newpaths else b""
                if oldbytes != newbytes:
                    name=str(GENERATED/short).encode()
                    delta.extend(b"".join(difflib.diff_bytes(difflib.unified_diff, oldbytes.splitlines(keepends=True),newbytes.splitlines(keepends=True),
                                fromfile=(b"a/"+name if short in oldpaths else b"/dev/null"),tofile=(b"b/"+name if short in newpaths else b"/dev/null"))))
            assert 0<len(delta)<8500000
            (PUBLIC/"same-reference.patch").write_bytes(delta)
            R["same_reference_sha256"]=sha(delta)
        assert set(new["ARCHIVES"])==set(old["ARCHIVES"])|{LIB}
        assert set(after)-set(before)<=set(R["resolver_objects"])
    # GNU's reference leaves large make products; retain only its resolver
    # archive before the independent Bazel target build.
    subprocess.run(["sudo","rm","-rf","--",ref],check=True)
    pkg=Path("tests/qualification_resolver");pkg.mkdir()
    (pkg/"select.bzl").write_text('''def _impl(ctx):
    files = [f for f in ctx.attr.src[DefaultInfo].files.to_list() if f.basename == "libresolv.a"]
    if len(files) != 1:
        fail("expected one real resolver archive")
    return [DefaultInfo(files = depset(files))]

resolver = rule(implementation = _impl, attrs = {"src": attr.label(mandatory = True)})
''')
    (pkg/"BUILD.bazel").write_text('load(":select.bzl", "resolver")\nresolver(name = "resolver", src = "//3rd_party/libc/glibc/static:archives")\n')
    target="//tests/qualification_resolver:resolver"
    opts=["--platforms=//platforms:linux_"+ARCH+"_glibc_static","--extra_toolchains=//toolchain:all","--jobs=4","--lockfile_mode=off","--noremote_accept_cached",*local_flags]
    bep=D/"resolver.bep"
    run("bazel_resolver",["bazel","build",*opts,"--build_event_publish_all_actions","--build_event_json_file="+str(bep),target])
    f=read(["bazel","cquery",*opts,"--output=starlark","--starlark:expr=target.files.to_list()[0].path",target]).decode().strip()
    built=Path(f);assert built.is_file() and built.name==LIB
    archives=[]; sample_configs=set()
    with bep.open() as lines:
        for line in lines:
            e=json.loads(line); k=e["id"].get("actionCompleted",{})
            a=e.get("action",{})
            if a.get("type")=="GlibcStaticCompile" and k.get("primaryOutput","").endswith("/resolv/ns_parse.o") and a.get("success"):
                sample_configs.add(k["configuration"]["id"])
            if a.get("type")=="GlibcStaticArchive": archives.append((k.get("label",""),k.get("primaryOutput",""),a.get("success",False)))
    assert len(archives)==1 and archives[0][1]==f and archives[0][2]
    got_members,got_symbols,got_hash=archive(built,tool)
    R["archive"]={"owner":archives[0][0],"reference_sha256":sha(proof.read_bytes()),"bazel_sha256":sha(built.read_bytes()),
        "reference_members":ref_members,"bazel_members":got_members,"bytes":built.stat().st_size,"machine":MACHINE,
        "defined_symbols":sum(map(len,got_symbols.values())),"typed_symbols_match":got_symbols==ref_symbols,
        "equal_objects":sum(ref_hash.get(k)==v for k,v in got_hash.items()),"symbols_sha256":sha(json.dumps(got_symbols,sort_keys=True).encode())}
    assert ref_members==got_members and got_symbols==ref_symbols
    save("bind_bazel_compiler")
    expr='outputs(".*resolv/ns_parse[.]o$", mnemonic("GlibcStaticCompile", deps('+target+')))'
    aq=json.loads(read(["bazel","aquery",*opts,"--noinclude_artifacts","--output=jsonproto",expr]))
    configs={int(c["id"]):c["checksum"] for c in aq["configuration"]}
    compilers=[]
    for cc in aq.get("actions",[]):
        cfg=configs[int(cc["configurationId"])]
        if cfg not in sample_configs: continue
        argv=cc["arguments"]; compiler=argv[0]
        assert cc["mnemonic"]=="GlibcStaticCompile"
        assert not Path(compiler).is_absolute() and "clang" in Path(compiler).name.lower()
        assert any(ARCH in token for token in argv[1:])
        compilers.append({"executable":compiler,"action_key":cc["actionKey"],"configuration":cfg})
    R["bazel_compilers"]=compilers
    assert sample_configs and {c["configuration"] for c in compilers}==sample_configs
    assert unchanged, "reference differs from the original for inputs outside resolver"
    R["status"]="PASS";save("verified")


try:
    main()
except Exception as error:
    R["error"]={"type":type(error).__name__,"lines":[f.lineno for f in traceback.extract_tb(error.__traceback__)]}
    save(R.get("phase","bootstrap"))
    sys.exit(1)
