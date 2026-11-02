"""Check the public tool as a single file and as an executable in exec config."""

def _tool_file_impl(ctx):
    return [DefaultInfo(files = depset([
        ctx.executable.tool if ctx.attr.executable_view else ctx.file.tool,
    ]))]

tool_file = rule(
    implementation = _tool_file_impl,
    attrs = {
        "executable_view": attr.bool(),
        "tool": attr.label(allow_single_file = True, cfg = "exec", executable = True, mandatory = True),
    },
)
