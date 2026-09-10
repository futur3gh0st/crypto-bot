"""Trading desk: menu, live dashboard, and the autopilot that runs sleeves unattended.

Paper by default. Nothing in this package can place a live order on its own —
live remains behind the same gates as the rest of the bot.
"""

from stablebot.desk.state import DeskState, SleeveStat, TapeEvent

__all__ = ["DeskState", "SleeveStat", "TapeEvent"]
