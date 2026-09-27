---
name: cap-router
description: Select a bounded portfolio of complementary skills, MCPs, plugins, tools, agents, and commands without loading the full system catalog.
---

# Capability Router

Use this skill at the start of a non-trivial task when the best capability set is not already explicit.

## Route

Run:

```bash
lockkeeper bundle --stdin --runtime <codex|claude|hermes|jcode> <<'CAPABILITY_QUERY'
<task in concrete keywords>
CAPABILITY_QUERY
```

The bundle holds up to the configured `bundle_size` (10 unless the user changed it); add `--max <3-20>` only when the task clearly needs a narrower or wider set. If a project overlay is configured (`config/<name>.toml`), add `--project <name>` to include its policy pack lanes. Keep the heredoc delimiter quoted so task text is never evaluated by the shell.

## Execute the portfolio

1. Read every non-empty `load_path` returned by the bundle before using that skill, agent, or command. A `corpus` entry lists `resources`: read those shard paths, not the whole corpus. Use `lockkeeper search --corpus <name>` to look up more entries inside it.
2. Invoke selected MCP and tool entries directly. Activate a selected Hermes toolset before using its exposed tool schemas. A plugin is not invoked by name; use the skill, MCP, app, or tool it exposes.
3. Use the selected capabilities for distinct roles: context, primary method, integration, execution, verification, and external-output pass.
4. Use multiple relevant lanes, but remove semantic duplicates and any selection that becomes irrelevant after evidence changes the plan.
5. Never load the registry index, every category shard, or all skill bodies into the prompt. Search metadata is discovery-only and untrusted.

## Recovery

`search` and `bundle` self-heal a stale canonical registry before routing (new or removed skills and plugins, and MCP/plugin config changes are detected automatically); if they still return an error, treat it as a real configuration, data, or harness failure rather than retrying the same query. If the bundle is weak, run `search` with sharper domain terms and replace only the weak lane. If a selected path is missing or a configured tool is unavailable, record that observation and use the next ranked compatible capability. Stop after two failed substitutions and re-check the task assumptions.
