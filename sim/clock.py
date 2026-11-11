"""Simulator clock: 15-min steps over a slice, as UTC ISO strings for messages plus local-time helpers."""
from dataclasses import dataclass

import numpy as np
import pandas as pd

ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Clock:
    ts_utc: pd.DatetimeIndex
    tz: str = "America/Chicago"

    @property
    def iso(self) -> list[str]:
        return list(self.ts_utc.strftime(ISO))

    @property
    def dt_h(self) -> float:
        return (self.ts_utc[1] - self.ts_utc[0]).total_seconds() / 3600

    @property
    def local(self) -> pd.DatetimeIndex:
        return self.ts_utc.tz_convert(self.tz)

    def local_days(self) -> list[np.ndarray]:
        """Step indices of each local calendar day, in order."""
        d = self.local.normalize()
        return [np.flatnonzero(d == day) for day in d.unique()]

    def end_iso(self, k: int) -> str:
        """End of step k (start of step k+1), even for the last step."""
        return (self.ts_utc[k] + (self.ts_utc[1] - self.ts_utc[0])).strftime(ISO)
