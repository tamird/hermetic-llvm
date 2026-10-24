"""Exercise the public header aliases in configured and unconfigured modules."""

load("@bazel_lib//lib:transitions.bzl", "platform_transition_test")
load("@bazel_skylib//lib:unittest.bzl", "asserts", "unittest")
load("@bazel_skylib//rules/directory:providers.bzl", "DirectoryInfo")
load("@platforms//host:constraints.bzl", "HOST_CONSTRAINTS")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")
load("@rules_cc//cc:cc_test.bzl", "cc_test")

def _selected_headers_test_impl(ctx):
    env = unittest.begin(ctx)
    headers = ctx.attr.headers[CcInfo].compilation_context.headers.to_list()
    directory = ctx.attr.directory[DirectoryInfo].path
    asserts.equals(env, [directory], [header.path for header in headers])
    asserts.true(
        env,
        directory.endswith(".{}/include".format(ctx.attr.version)),
        "Expected Linux {} headers, got {}".format(ctx.attr.version, directory),
    )
    return unittest.end(env)

_selected_headers_test = unittest.make(
    _selected_headers_test_impl,
    attrs = {
        "directory": attr.label(default = Label("@kernel_headers//:kernel_headers_directory")),
        "headers": attr.label(default = Label("@kernel_headers//:kernel_headers")),
        "version": attr.string(mandatory = True),
    },
)

def kernel_headers_selection_tests(default_gnu_version, default_musl_version):
    """Test actual aliases and C includes without changing libc constraints.

    Args:
        default_gnu_version: Expected full version with no kernel constraint.
        default_musl_version: Expected full version for unconstrained musl.
    """
    for name, constraints, version in [
        ("gnu", ["@llvm//constraints/libc:gnu.2.28"], default_gnu_version),
        ("musl", ["@llvm//constraints/libc:musl"], default_musl_version),
        ("unconstrained", [], default_gnu_version),
        ("explicit_gnu", ["@llvm//constraints/libc:gnu.2.28", "@llvm//constraints/kernel/linux:5.4"], "5.4.293"),
        ("explicit_musl", ["@llvm//constraints/libc:musl", "@llvm//constraints/kernel/linux:5.4"], "5.4.293"),
    ]:
        platform = name + "_header_platform"
        native.platform(
            name = platform,
            constraint_values = HOST_CONSTRAINTS + constraints,
        )
        _selected_headers_test(
            name = name + "_selection",
            version = version,
            tags = ["manual"],
        )
        cc_test(
            name = name + "_compile",
            srcs = [Label("//tests/kernel_headers:version_test.c")],
            defines = ["EXPECTED_KERNEL_VERSION=KERNEL_VERSION({})".format(version.replace(".", ","))],
            tags = ["manual"],
            deps = [Label("@kernel_headers//:kernel_headers")],
        )
        for kind in ("selection", "compile"):
            platform_transition_test(
                name = name + "_" + kind + "_test",
                binary = name + "_" + kind,
                target_platform = platform,
                target_compatible_with = ["@platforms//os:linux"],
            )
