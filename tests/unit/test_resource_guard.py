import pytest

from lumen_data.resource_guard import check_budget


def test_resource_gate_stops_before_next_job():
    check_budget(free_disk=50, available_ram=10, min_disk=40, min_ram=8)
    with pytest.raises(RuntimeError, match='disk'):
        check_budget(free_disk=39, available_ram=10, min_disk=40, min_ram=8)
    with pytest.raises(RuntimeError, match='memory'):
        check_budget(free_disk=50, available_ram=7, min_disk=40, min_ram=8)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1])
def test_resource_thresholds_are_finite_nonnegative(value):
    with pytest.raises(ValueError):
        check_budget(free_disk=50, available_ram=10, min_disk=value, min_ram=8)
