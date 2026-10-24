"""codex-autoresume: unattended auto-continue for OpenAI Codex.

Watches the local Codex state databases (read-only) for threads and goals
interrupted by usage limits, waits for the quota window to reset, and resumes
them headlessly via ``codex exec resume`` with per-resume model/reasoning
overrides.
"""

__version__ = "0.2.6"
