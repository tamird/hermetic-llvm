"""Observe the original default file and files_to_run of a configured tool."""

load("@with_cfg.bzl", "with_cfg")
load(":mode.bzl", "PATCHED")
load(":probe.bzl", "legacy_binary", "probe_binary")

def _make(kind, conditional):
    builder = with_cfg(kind, executable = True).set("compilation_mode", "opt")
    platform = select({Label("//:is_linux"): [Label("//:linux_musl")], "//conditions:default": []})
    if conditional and PATCHED:
        return builder.prepend("platforms", platform).build()
    return builder.set("platforms", platform).build()

naive, _naive_alias = _make(legacy_binary, False)
conditional, _conditional_alias = _make(legacy_binary, True)
conditional_rule, _rule_alias = _make(probe_binary, True)

def _files(ctx):
    return [DefaultInfo(files = depset([ctx.file.tool, ctx.executable.tool]))]

tool_files = rule(
    implementation = _files,
    attrs = {"tool": attr.label(allow_single_file = True, cfg = "target", executable = True, mandatory = True)},
)
