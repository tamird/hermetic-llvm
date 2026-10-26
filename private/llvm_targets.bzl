"""Resolve backend requests for the shared LLVM source repository."""

def llvm_targets(modules):
    """Resolve the source backends selected across the module graph.

    Args:
        modules: Modules using the LLVM extension, in Bazel's traversal order.

    Returns:
        The selected backend names, without duplicates.
    """
    root_sets_defaults = any([module.is_root and module.tags.default_targets for module in modules])
    targets = {}
    for module in modules:
        tags = module.tags.configure
        if module.is_root or (module.name == "llvm" and not root_sets_defaults):
            if len(module.tags.default_targets) > 1:
                fail("Only one llvm.default_targets(...) tag is allowed per module")
            tags = module.tags.default_targets + tags
        for tag in tags:
            for target in tag.targets:
                targets[target] = None
    return targets.keys()
