# version.py
# Single source of truth for project version

__version__ = "0.1.0"
__version_info__ = tuple(map(int, __version__.split(".")))

# Version status
VERSION_STATUS = "alpha"  # alpha, beta, stable

# Full version string with status
def get_version():
    if VERSION_STATUS != "stable":
        return f"{__version__}-{VERSION_STATUS}"
    return __version__