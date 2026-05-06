"""Default fetch functions for the source-eval framework.

Importing this module registers each source via ``@register(name)``.
Each fetch function has signature::

    fetch(station_icao, lst_date, lat, lon) -> Optional[(mu_f, sigma_f)]

Returns the source's day-ahead-ish forecast μ for that (station, date),
or None if the source can't produce a value for that input.
"""

from __future__ import annotations

# Import each module side-effect-registers itself.
from . import om_ecmwf  # noqa: F401
from . import om_gfs    # noqa: F401  (baseline; should match production weather/nbm)
from . import om_global_models  # noqa: F401  (ICON, Meteo-France, UKMO, JMA, CMA)
from . import climatology  # noqa: F401
from . import iem_min_asos  # noqa: F401

# 2026-04-29: GraphCast variants all return HTTP 400 on Open-Meteo
# ("Cannot initialize MultiDomains from invalid String value graphcast.")
# in both live and historical APIs. Skipping until Open-Meteo lists it
# as a supported model. The agency models above (ICON, Meteo-France,
# etc.) replace GraphCast as our independent-source candidates.
