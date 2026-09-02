from dataclasses import dataclass

'''Pure-Python alarm definitions. No Qt imports here (and none should be added) so
this module can be evaluated headlessly and unit-tested without a display.'''


@dataclass
class AlarmSpec:
    high: float | None = None
    low: float | None = None
    clear_high: float | None = None
    clear_low: float | None = None
    abs_high: float | None = None
    clear_abs_high: float | None = None
    consecutive_samples: int = 3
    stale_multiplier: float = 5
