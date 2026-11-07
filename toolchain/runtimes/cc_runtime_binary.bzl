load("@llvm//platforms:common.bzl", "SUPPORTED_EXECS")
load("@llvm//toolchain/runtimes:with_cfg_runtimes_common.bzl", "configure_builder_for_runtimes")
load("@rules_cc//cc:cc_binary.bzl", "cc_binary")
load("@with_cfg.bzl", "with_cfg")

_builder = with_cfg(
    cc_binary,
)

cc_runtime_stage0_binary, _cc_runtime_stage0_binary_internal = configure_builder_for_runtimes(_builder.clone(), "stage0").build()
_stage1_hosted = configure_builder_for_runtimes(_builder.clone(), "stage1_hosted")
_linux_musl_platforms = {
    Label("//platforms/config:linux_" + cpu): [Label("//platforms:linux_{}_musl".format(cpu))]
    for os, cpu in SUPPORTED_EXECS
    if os == "linux"
}

cc_runtime_stage1_hosted_binary, _cc_runtime_stage1_hosted_binary_internal = _stage1_hosted.clone().build()

cc_runtime_stage1_hosted_tool, _cc_runtime_stage1_hosted_tool_internal = _stage1_hosted.set(
    "platforms",
    select(_linux_musl_platforms | {"//conditions:default": []}),
    keep_if_empty = True,
).build()
