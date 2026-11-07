load(":defs.bzl", "conditional", "conditional_rule", "naive", "tool_files")

constraint_setting(name = "libc")

constraint_value(name = "glibc", constraint_setting = ":libc")
constraint_value(name = "musl", constraint_setting = ":libc")
constraint_setting(name = "abi")
constraint_value(name = "msvc", constraint_setting = ":abi")
constraint_setting(name = "extra")
constraint_value(name = "custom", constraint_setting = ":extra")

platform(name = "linux_glibc", constraint_values = ["@platforms//os:linux", "@platforms//cpu:x86_64", ":glibc"])
platform(name = "linux_musl", constraint_values = ["@platforms//os:linux", "@platforms//cpu:x86_64", ":musl"])
platform(name = "windows_custom", constraint_values = ["@platforms//os:windows", "@platforms//cpu:x86_64", ":custom", ":msvc"])
config_setting(name = "is_linux", constraint_values = ["@platforms//os:linux", "@platforms//cpu:x86_64"])

naive(name = "naive_tool")
conditional(name = "conditional_tool")
conditional_rule(name = "rule_tool")

[tool_files(name = tool + "_files", tool = ":" + tool + "_tool") for tool in ("naive", "conditional", "rule")]
