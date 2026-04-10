"""Shared Rich UI primitives for ApplyPilot."""

from rich.console import Console

# A single shared console keeps live progress bars anchored while log messages
# are rendered above them instead of interleaving mid-line.
console = Console()
