load("@llvm//toolchain:selects.bzl", "platform_module_map")
load(":view.bzl", "executable_view")

[
    platform(
        name = name,
        constraint_values = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
            "@llvm//constraints/libc:" + libc,
        ],
    )
    for name, libc in [("gnu", "gnu.2.28"), ("musl", "musl")]
]

# Select the stock complete-stage module map used by the Linux x86_64
# cc_toolchain. Its generator is an executable dependency with cfg = "exec".
alias(
    name = "real_map",
    actual = platform_module_map("linux", "x86_64"),
)

# Match the same execution transition for the independent closure inventory.
executable_view(name = "generator", tool = "@llvm//tools/internal:module-map-generator")
