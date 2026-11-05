"""
What a KMIA temperature reading can actually tell us, in whole degrees F (pure functions, no I/O).

ASOS measures in WHOLE degrees F. Two kinds of report carry that measurement:

  * 5-minute observations (the NOAA HFMETAR feed, and the 5-minute rows in the NWS API / Synoptic) are converted to
    WHOLE degrees C. One degree C is 1.8 F, so two neighbouring F values often give the same C value:
    84F and 85F are both 29C (84.2F), 82F and 83F are both 28C (82.4F), 87F and 88F are both 31C (87.8F).
  * Hourly METAR / SPECI reports also carry tenths of a degree C (the T-group), which identifies the F value exactly:
    29.4C is 85F.

So a whole-degree-C value is only known to within a set of one or two whole-F values, while a value with tenths is exact.
The 5-minute value is the station's own 5-minute average, rounded to whole F and then to whole C (verified against
the 1-minute ASOS record).
"""
import math

WHOLE_C_TOLERANCE = 0.02       # a reading rounded to 2 decimals in F is within 0.005 C of a whole-degree-C value


def _round_half_up(x):
    return int(math.floor(x + 0.5))


def is_whole_celsius(temp_f):
    """True when the reading is (to within rounding) a whole number of degrees C, i.e. it has lost its precision."""
    c = (temp_f - 32.0) * 5.0 / 9.0
    return abs(c - round(c)) <= WHOLE_C_TOLERANCE


def possible_whole_f(temp_f):
    """
    The whole-degree-F values the station may have measured, given a reading in F.
    A whole-degree-C reading gives every F value that rounds to that C value (one or two); a reading with tenths
    of a degree C is exact and gives just its own rounded F. None gives [].
    """
    if temp_f is None:
        return []
    temp_f = float(temp_f)
    if not is_whole_celsius(temp_f):
        return [_round_half_up(temp_f)]
    c = round((temp_f - 32.0) * 5.0 / 9.0)
    base = int(math.floor(temp_f))
    return [t for t in range(base - 3, base + 4) if _round_half_up((t - 32.0) * 5.0 / 9.0) == c]


def lower_bound_whole_f(temp_f):
    """The lowest whole-degree-F value consistent with the reading, or None."""
    possible = possible_whole_f(temp_f)
    return min(possible) if possible else None


def upper_bound_whole_f(temp_f):
    """The highest whole-degree-F value consistent with the reading, or None."""
    possible = possible_whole_f(temp_f)
    return max(possible) if possible else None
