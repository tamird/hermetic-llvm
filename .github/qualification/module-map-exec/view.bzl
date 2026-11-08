def _view(ctx):
    return [DefaultInfo(files = depset([ctx.executable.tool]))]

executable_view = rule(
    implementation = _view,
    attrs = {"tool": attr.label(cfg = "exec", executable = True, mandatory = True)},
)
