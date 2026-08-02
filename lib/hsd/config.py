"""Configuration loading.

The same flat two-level YAML dialect herdr-nerd-font-tab-name uses, parsed with
the stdlib so the plugin has no dependencies. Only `section:` / `  key: value`
is understood — that is all the config needs.
"""

import os

USER_CONFIG_ENV = "HERDR_SPACE_DIFFSTAT_CONFIG"

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(PLUGIN_ROOT, "config", "defaults.yml")

_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def user_config_paths():
    """Candidate user config locations, most specific first."""
    paths = []
    override = os.environ.get(USER_CONFIG_ENV)
    if override:
        paths.append(os.path.expanduser(override))
    plugin_config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if plugin_config_dir:
        paths.append(os.path.join(plugin_config_dir, "config.yml"))
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    paths.append(os.path.join(xdg, "herdr", "herdr-space-diffstat.yml"))
    return paths


def parse_yaml(text):
    """Parse the flat `section:` / `  key: value` subset used by the config."""
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            name = line.split(":", 1)[0].strip()
            if name:
                current = sections.setdefault(name, {})
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key.strip()] = _clean_value(value)
    return sections


def _clean_value(value):
    value = value.strip()
    # A trailing comment only counts when whitespace separates it, so an icon
    # that happens to be "#" survives.
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            return value[1:end]
    hash_at = value.find(" #")
    if hash_at != -1:
        value = value[:hash_at].rstrip()
    return value


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return parse_yaml(handle.read())
    except (OSError, UnicodeDecodeError):
        return {}


class Config:
    """Merged view of the user config over the shipped defaults."""

    def __init__(self, user=None, defaults=None):
        self.user = user or {}
        self.defaults = defaults or {}

    @classmethod
    def load(cls, default_path=None, user_path=None):
        defaults = _read(default_path or DEFAULT_CONFIG_PATH)
        if user_path is None:
            for candidate in user_config_paths():
                if os.path.isfile(candidate):
                    user_path = candidate
                    break
        user = _read(user_path) if user_path else {}
        return cls(user=user, defaults=defaults)

    def get(self, section, key, fallback=None):
        for source in (self.user, self.defaults):
            value = source.get(section, {}).get(key)
            if value is not None:
                return value
        return fallback

    def section(self, name):
        merged = dict(self.defaults.get(name, {}))
        merged.update(self.user.get(name, {}))
        return merged

    def bool(self, key, fallback=False):
        value = self.get("config", key)
        if value is None:
            return fallback
        value = value.strip().lower()
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        return fallback

    def float(self, key, fallback=0.0):
        try:
            return float(self.get("config", key, fallback))
        except (TypeError, ValueError):
            return fallback

    def int(self, key, fallback=0):
        try:
            return int(float(self.get("config", key, fallback)))
        except (TypeError, ValueError):
            return fallback

    def option(self, key, fallback=""):
        """A config value, with the literal string "null" meaning unset."""
        value = self.get("config", key, fallback)
        if value is None or value == "null":
            return ""
        return value
