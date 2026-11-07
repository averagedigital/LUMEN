from pathlib import Path
import math
import shutil


def check_budget(*, free_disk, available_ram, min_disk, min_ram):
    if any(not math.isfinite(value) or value < 0
           for value in (free_disk, available_ram, min_disk, min_ram)):
        raise ValueError('resource values must be finite and nonnegative')
    if free_disk < min_disk:
        raise RuntimeError('resource_error: disk reserve reached')
    if available_ram < min_ram:
        raise RuntimeError('resource_error: memory reserve reached')


def resources(path):
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) * 1024
    available = memory['MemAvailable']
    limit_file = Path('/sys/fs/cgroup/memory.max')
    current_file = Path('/sys/fs/cgroup/memory.current')
    if limit_file.exists() and current_file.exists():
        limit = limit_file.read_text().strip()
        if limit != 'max':
            available = min(available, int(limit) - int(current_file.read_text()))
    return {'free_disk': shutil.disk_usage(path).free, 'available_ram': available}


def guard(path, min_disk_gib=40, min_ram_gib=1):
    state = resources(path)
    check_budget(**state, min_disk=min_disk_gib * 2**30, min_ram=min_ram_gib * 2**30)
    return state
