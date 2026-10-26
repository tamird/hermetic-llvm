"""Regression tests for LLVM backend selection across module dependencies."""

load("@bazel_skylib//lib:unittest.bzl", "asserts", "unittest")
load("//private:llvm_targets.bzl", "llvm_targets")

def _module(name, root = False, defaults = None, required = []):
    return struct(
        name = name,
        is_root = root,
        tags = struct(
            configure = [struct(targets = required)],
            default_targets = [] if defaults == None else [struct(targets = defaults)],
        ),
    )

def _llvm_targets_test_impl(ctx):
    env = unittest.begin(ctx)
    llvm = _module("llvm", defaults = ["AArch64", "ARM", "X86"])
    dep = _module("dep", defaults = ["NVPTX"], required = ["BPF", "X86"])
    root = _module("root", root = True, required = ["X86"])

    # Without a root override, preserve the order of the additive requests.
    asserts.equals(env, ["X86", "BPF", "AArch64", "ARM"], llvm_targets([root, dep, llvm]))

    # Replacing defaults must not drop a dependency's explicit request.
    root = _module("root", root = True, defaults = ["AArch64"])
    asserts.equals(env, ["AArch64", "BPF", "X86"], llvm_targets([root, dep, llvm]))

    # An empty override is distinct from omitting the tag.
    root = _module("root", root = True, defaults = [])
    asserts.equals(env, ["BPF", "X86"], llvm_targets([root, dep, llvm]))

    # An indirect user keeps LLVM's defaults when the root does not use the extension.
    asserts.equals(env, ["BPF", "X86", "AArch64", "ARM"], llvm_targets([dep, llvm]))

    return unittest.end(env)

llvm_targets_test = unittest.make(_llvm_targets_test_impl)
