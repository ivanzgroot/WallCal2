"""
WallCal presence subsystem.

Contains the HLK-LD2410C radar driver, the display power abstraction that
autodetects whatever output stack the Pi happens to be running, and the
daemon that ties the two together.
"""

__all__ = ["ld2410", "display", "runtime", "daemon"]
