"""Weekend MOTD generation — resolve the line-up, render it, publish it.

The builder turns the current weekend's workbook sheet into a fixed-size grid
of poster slots, resolving each curator-supplied ``Title (YYYY)`` to an IMDb
tt# and poster art. Slots that cannot be resolved (or that have no title yet)
become mystery boxes, so the grid is always complete and self-heals on the next
run once a curator fixes their title.
"""

from .builder import MOTDSlot, build_slots, week_context
from .render import render_motd

__all__ = ["MOTDSlot", "build_slots", "week_context", "render_motd"]
