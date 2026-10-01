import pathlib
from importlib.metadata import PackageNotFoundError, version

from mopidy import config, ext

try:
    __version__ = version("Mopidy-Subidy")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+local"


class SubidyExtension(ext.Extension):

    dist_name = "Mopidy-Subidy"
    ext_name = "subidy"
    version = __version__

    def get_default_config(self):
        return config.read(pathlib.Path(__file__).parent / "ext.conf")

    def get_config_schema(self):
        schema = super().get_config_schema()
        schema["url"] = config.String()
        schema["username"] = config.String()
        schema["password"] = config.Secret()
        schema["legacy_auth"] = config.Boolean(optional=True)
        schema["api_version"] = config.String(optional=True)
        schema["insecure"] = config.Boolean(optional=True)
        return schema

    def setup(self, registry):
        from .backend import SubidyBackend

        registry.add("backend", SubidyBackend)
