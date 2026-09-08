"""Capability registration. Importing this module populates the registry.

Adding a capability is: a directory here, a Capability instance, one import.
"""

from gaia.capabilities.base import registry

__all__ = ["registry"]
