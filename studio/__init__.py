"""agent-config-studio: inventory, graph, compliance and update governance for the
local Claude Code and Codex agent configuration.

Design constraints:

* Standard library only. This tool audits the agent config, so it must run from
  launchd on a bare interpreter with no venv and no pip state to drift.
* Scanning and health checking never write. Every mutation goes through
  :mod:`studio.patch`, which backs up the exact bytes it replaces first.
* Every rule cites the published requirement it enforces, so a "compliant"
  verdict can be re-derived from the sources rather than taken on trust.
"""

__version__ = "0.1.0"
