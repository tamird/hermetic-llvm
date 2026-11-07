"""Small executable for observing incoming configuration changes."""

def _probe_impl(ctx):
    def has(name):
        return ctx.target_platform_has_constraint(getattr(ctx.attr, name)[platform_common.ConstraintValueInfo])

    flavor = "custom-msvc" if has("_msvc") and has("_custom") else "musl" if has("_musl") else "fallback"
    out = ctx.actions.declare_file(ctx.label.name + (".exe" if has("_msvc") else ".tool"))
    ctx.actions.write(out, flavor + " " + ctx.var["COMPILATION_MODE"] + "\n", is_executable = True)
    return [DefaultInfo(files = depset([out]), executable = out)]

probe_binary = rule(
    implementation = _probe_impl,
    attrs = {
        "_custom": attr.label(default = ":custom"),
        "_msvc": attr.label(default = ":msvc"),
        "_musl": attr.label(default = ":musl"),
    },
    executable = True,
)

def legacy_binary(name, **kwargs):
    probe_binary(name = name, **kwargs)
