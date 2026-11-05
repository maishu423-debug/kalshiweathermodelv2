"""Whole-degree-C readings only narrow the true whole-degree-F temperature to one or two values."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from weather.asos_resolution import (  # noqa: E402
    is_whole_celsius, lower_bound_whole_f, possible_whole_f, upper_bound_whole_f)


@pytest.mark.parametrize("reading,expected", [
    (82.4, [82, 83]),      # 28C
    (84.2, [84, 85]),      # 29C
    (87.8, [87, 88]),      # 31C
    (80.6, [80, 81]),      # 27C
    (89.6, [89, 90]),      # 32C
    (86.0, [86]),          # 30C: one F value only
    (77.0, [77]),          # 25C
    (91.4, [91, 92]),      # 33C
])
def test_a_whole_celsius_reading_could_be_one_of_two_fahrenheit_values(reading, expected):
    assert possible_whole_f(reading) == expected


@pytest.mark.parametrize("reading,expected", [(84.92, 85), (84.02, 84), (78.08, 78), (88.0, 88), (85.0, 85), (84.0, 84)])
def test_a_reading_with_tenths_of_a_degree_c_is_exact(reading, expected):
    # 29.4C = 84.92F = 85F exactly; whole-F bounds such as a DSM high (85.0) are exact too
    assert possible_whole_f(reading) == [expected]


def test_every_true_fahrenheit_value_is_among_the_possibilities_its_celsius_reading_allows():
    for true_f in range(40, 111):
        c = int((true_f - 32) * 5 / 9 + 0.5)            # what the 5-minute feed reports
        seen_f = round(c * 9 / 5 + 32, 2)               # what the project then sees
        assert true_f in possible_whole_f(seen_f), (true_f, seen_f)
        assert upper_bound_whole_f(seen_f) - lower_bound_whole_f(seen_f) <= 1


def test_the_lower_and_upper_bounds():
    assert (lower_bound_whole_f(87.8), upper_bound_whole_f(87.8)) == (87, 88)
    assert (lower_bound_whole_f(86.0), upper_bound_whole_f(86.0)) == (86, 86)


def test_none_gives_nothing():
    assert possible_whole_f(None) == [] and lower_bound_whole_f(None) is None and upper_bound_whole_f(None) is None


def test_is_whole_celsius():
    assert is_whole_celsius(84.2) and is_whole_celsius(87.8) and is_whole_celsius(82.4)
    assert not is_whole_celsius(84.92) and not is_whole_celsius(85.0)
