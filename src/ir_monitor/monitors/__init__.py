"""Monitor registry.

Adding a company means: write ``monitors/<name>.py``, register it here, add an
entry to ``config/companies.yaml``. Nothing else in the system changes.
"""

from __future__ import annotations

from ..config import CompanyConfig
from .base import CompanyMonitor, ParserFailure
from .basic_fit import BasicFitMonitor
from .benefit_systems import BenefitSystemsMonitor
from .bluefit import BluefitMonitor
from .bodytech import BodytechMonitor
from .leejam import LeejamMonitor
from .planet_fitness import PlanetFitnessMonitor
from .puregym import PureGymMonitor
from .selfit import SelfitMonitor
from .sats import SATSMonitor
from .sports_world import SportsWorldMonitor
from .the_gym_group import TheGymGroupMonitor
from .xponential import XponentialMonitor

REGISTRY: dict[str, type[CompanyMonitor]] = {
    "bluefit": BluefitMonitor,
    "selfit": SelfitMonitor,
    "bodytech": BodytechMonitor,
    "planet_fitness": PlanetFitnessMonitor,
    "xponential": XponentialMonitor,
    "sports_world": SportsWorldMonitor,
    "basic_fit": BasicFitMonitor,
    "the_gym_group": TheGymGroupMonitor,
    "puregym": PureGymMonitor,
    "sats": SATSMonitor,
    "benefit_systems": BenefitSystemsMonitor,
    "leejam": LeejamMonitor,
}


def build_monitor(config: CompanyConfig) -> CompanyMonitor:
    try:
        cls = REGISTRY[config.monitor]
    except KeyError as exc:  # pragma: no cover - configuration error
        raise KeyError(
            f"no monitor registered under '{config.monitor}' "
            f"(company '{config.key}'); known: {sorted(REGISTRY)}"
        ) from exc
    return cls(config)


__all__ = ["REGISTRY", "build_monitor", "CompanyMonitor", "ParserFailure"]
