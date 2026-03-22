"""fit_merger – Merge Garmin FIT activity files."""

from .core.merger import merge, fmt_time
from .core.parser import FitParser, FitRecord

__all__ = ["merge", "fmt_time", "FitParser", "FitRecord"]
