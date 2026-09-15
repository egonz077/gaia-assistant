from gaia.capabilities.base import registry
from gaia.capabilities.calendar import CAPABILITY as CALENDAR
from gaia.capabilities.leads import CAPABILITY as LEADS
from gaia.capabilities.meetings import CAPABILITY as MEETINGS

registry.register(MEETINGS)
registry.register(LEADS)
registry.register(CALENDAR)
