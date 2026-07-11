from .travelpayouts import TravelpayoutsAdapter
from .duffel import DuffelAdapter
from .liteapi import LiteAPIAdapter
from .viator import ViatorAdapter
# XoteloAdapter removed — requires RapidAPI auth (not free as originally assumed)

ALL_ADAPTERS = [
    TravelpayoutsAdapter,
    DuffelAdapter,
    LiteAPIAdapter,
    ViatorAdapter,
]
