"""Sizes are decimal (1 TB = 10¹² bytes), like drive labels and `df -H`.
fmt_size divided by 1024 but said "TB": oak's 4.53 TB read as "4.1 TB"."""
import pytest

from mmw.core.format import fmt_size, fmt_speed


@pytest.mark.parametrize("n,shown", [
    (0, "0.0 B"),
    (999, "999.0 B"),
    (1_000, "1.0 KB"),
    (697_891_622_062, "697.9 GB"),
    (4_530_833_816_200, "4.5 TB"),
    (7_900_517_449_424, "7.9 TB"),
    (2_500_000_000_000_000, "2.5 PB"),
])
def test_sizes_are_decimal(n, shown):
    assert fmt_size(n) == shown


def test_speed_uses_the_same_units():
    assert fmt_speed(12_500_000) == "12.5 MB/s"
