from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kryten-webqueue")
except PackageNotFoundError:  # running from source without an install
    __version__ = "0.0.0+dev"
