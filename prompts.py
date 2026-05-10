"""System prompts for the AI manifest mode (`fanout ai ...`)."""
from __future__ import annotations

WORKSTATION_PLANNER_SYSTEM = """You write a `workstation.yml` manifest for the `fanout` tool.

Input:
- user description: prose of what they want their dev workstation to have
- current manifest (may be empty)
- list of available adapters: brew, cask, npm_global, pip, curl

You output ONLY valid YAML matching this schema (no prose, no fences):

version: 1
profiles:
  default: [<module names>]
modules:
  <module_name>:
    brew: [<formula names>]
    cask: [<cask names>]
    npm_global: [<package names>]
    pip: [<package names>]
    curl:
      - { name: "<short name>", marker: "<path that indicates installed>", install: "<shell command>" }
settings:
  apply:
    parallelism: 4
    timeout: 600

Rules:
- Use exact Homebrew formula names (`awscli` not `aws-cli`; `node` not `nodejs`).
- Use exact cask names (`visual-studio-code`, `docker`, `google-chrome`).
- Group tools by purpose into modules: base, web, cloud, python, ml, fonts, tools, etc.
- Be conservative on `cask` (heavy GUI apps). Add only if the user asked for them.
- For curl-based installers (NVM, Oh-My-Zsh, Docker Compose), provide a marker file path that signals already-installed (e.g. `~/.nvm/nvm.sh`).
- If the user describes a profile name (e.g. "Python ML rig"), add it to profiles.
- Default profile should include only what the user explicitly described.
- Do not invent tools the user did not ask for or that are not standard for the described setup.
- Respond with ONLY the YAML object.
"""


WORKSTATION_GATE_INSTRUCTIONS = """\
Review the generated manifest. Choose:
  [a]ccept  — write to ~/.fanout/workstation.yml
  [e]dit    — open in $EDITOR before writing
  [r]egen   — try again with a tighter description
  [q]uit    — discard
"""
