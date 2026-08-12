"""Module entry point: ``python -m bench_core``.

Delegates to :func:`bench_core.bench.main` (the host-agnostic smoke CLI). Real
provider entries live in ``e2b_bench`` / ``docker_bench``.
"""
from bench_core.bench import main

if __name__ == "__main__":
    main()
