"""Capability registration. Importing this module populates the registry.

Adding a capability is: a directory here, a Capability instance, one import.
"""

from gaia.capabilities.base import registry
from gaia.capabilities.leads import CAPABILITY as LEADS
from gaia.capabilities.meetings import CAPABILITY as MEETINGS

registry.register(MEETINGS)
registry.register(LEADS)

__all__ = ["registry"]
