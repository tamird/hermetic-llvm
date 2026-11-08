"""Observe the configured exec tool and the two platforms seen by the consumer."""

def _tool_file_impl(ctx):
    tool = ctx.attr.tool[DefaultInfo]
    info = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(info, json.encode({
        "defaults": [f.short_path for f in tool.files.to_list()],
        "executable": ctx.executable.tool.short_path,
        "owner": str(ctx.attr.tool.label),
    }))
    return [
        DefaultInfo(files = depset([ctx.executable.tool])),
        OutputGroupInfo(description = depset([info])),
    ]

tool_file = rule(
    implementation = _tool_file_impl,
    # exec_test.tools already selected the execution configuration for this view.
    attrs = {"tool": attr.label(cfg = "target", executable = True, mandatory = True)},
)

def _platform_facts_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(out, json.encode({
        constraint.label.name: ctx.target_platform_has_constraint(constraint[platform_common.ConstraintValueInfo])
        for constraint in ctx.attr.constraints
    }))
    return [DefaultInfo(files = depset([out]))]

platform_facts = rule(
    implementation = _platform_facts_impl,
    attrs = {"constraints": attr.label_list(mandatory = True)},
)
