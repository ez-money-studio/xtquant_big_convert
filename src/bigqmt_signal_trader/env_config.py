# coding: utf-8
""".env profiles: one file per deployment, no secrets in the repo or in argv.

Two machines are involved in a deployment and both need the same account id and
transport, so the values were previously written into two hand-edited Python
files (``bigqmt_signal_trader_local_config.py`` on the QMT side,
``bigqmt_signal_trader_client_config.py`` on the client side) plus a handful of
environment variables read directly by the client. Keeping those in step by
hand is exactly the mistake this module exists to remove: one ``.env.<profile>``
is the single source, and everything else is derived from it.

    .env.template   committed shape, no real values
    .env.dev        simulated / paper account   (gitignored)
    .env.prod       live account                (gitignored)

Which file is used, in order:

  1. an explicit ``path`` argument (``--file``)
  2. ``BIGQMT_ENV_FILE``
  3. ``.env.<profile>`` where profile is ``BIGQMT_ENV`` (default ``dev``),
     searched from the current directory upward
  4. a plain ``.env``, same search

Nothing is required: with no file anywhere this module is a no-op and the
client behaves exactly as before.

Two ways to consume a profile:

  * implicit -- ``bigqmt_signal_trader.xtquant_compat`` calls :func:`autoload`
    on import, so a profile in the working directory (or one named by
    ``BIGQMT_ENV``/``BIGQMT_ENV_FILE``) is applied to ``os.environ`` and the
    client picks it up through its normal environment fallbacks.
  * explicit -- ``bigqmt-env`` renders the two config ``.py`` files from the
    profile, which is what carries the server-side keys (account type, order
    switch, adjust interval). That generator lives in ``init_config``; keeping
    it out of here is deliberate -- this module is reachable from the client's
    import path, and ``init_config`` imports ``subprocess``/``getpass``, which
    the QMT sandbox whitelist rejects.

When a profile is *explicitly* selected (``BIGQMT_ENV``/``BIGQMT_ENV_FILE``/
``--file``) it wins over a discovered ``*_config.py`` module; otherwise the
config module keeps its usual precedence and the profile only fills in
environment variables that are unset.

Set ``BIGQMT_ENV_DISABLE=1`` to switch discovery off entirely (harnesses that
must not pick up a developer's local profile).
"""

import json
import os


ENV_PROFILE_VAR = "BIGQMT_ENV"
ENV_FILE_VAR = "BIGQMT_ENV_FILE"
DISABLE_VAR = "BIGQMT_ENV_DISABLE"
PROFILES = ("dev", "prod")
DEFAULT_PROFILE = "dev"
PROFILE_FILENAME = ".env.%s"
PLAIN_FILENAME = ".env"
TEMPLATE_FILENAME = ".env.template"
MAX_WALK_UP = 8


def _is_key(name):
    if not name:
        return False
    if not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(char.isalnum() or char == "_" for char in name)


def _clean_value(value):
    """Strip one layer of quotes, or an unquoted trailing ``# comment``."""
    value = str(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    hash_at = value.find("#")
    if hash_at >= 0 and (hash_at == 0 or value[hash_at - 1].isspace()):
        value = value[:hash_at].rstrip()
    return value


def parse_env_text(text):
    """Parse ``.env`` text into a dict.

    Handles blank lines, ``#`` comments, an optional ``export `` prefix, and
    single/double quoted values (which keep ``#``, spaces and ``=`` verbatim --
    passwords end up here, so a ``#`` in one must survive). A malformed line is
    skipped rather than fatal: one typo should not stop a deployment.
    """
    values = {}
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    for raw in normalized.split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not _is_key(key):
            continue
        values[key] = _clean_value(value)
    return values


def active_profile(profile=None):
    value = profile or os.environ.get(ENV_PROFILE_VAR) or DEFAULT_PROFILE
    return str(value).strip().lower() or DEFAULT_PROFILE


def search_dirs(start=None):
    """Directories to look in: ``start`` (default cwd) and its parents."""
    directory = os.path.abspath(start or os.getcwd())
    for _ in range(MAX_WALK_UP):
        yield directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return
        directory = parent


def resolve_env_file(profile=None, path=None, start=None):
    """Return the profile file to read, or ``None``.

    The profile-specific name is searched in full before a plain ``.env`` is
    considered at all, so a ``.env.dev`` in a parent directory still beats an
    unrelated ``.env`` sitting next to the script.
    """
    if path:
        candidate = os.path.abspath(path)
        return candidate if os.path.isfile(candidate) else None
    named = os.environ.get(ENV_FILE_VAR)
    if named:
        candidate = os.path.abspath(named)
        return candidate if os.path.isfile(candidate) else None
    wanted = PROFILE_FILENAME % active_profile(profile)
    for directory in search_dirs(start):
        candidate = os.path.join(directory, wanted)
        if os.path.isfile(candidate):
            return candidate
    for directory in search_dirs(start):
        candidate = os.path.join(directory, PLAIN_FILENAME)
        if os.path.isfile(candidate):
            return candidate
    return None


def apply_env(values, override=False):
    """Copy ``values`` into ``os.environ``; returns the names actually set.

    An existing non-empty environment variable wins unless ``override`` is
    given, so ``set BIGQMT_ACCOUNT_ID=...`` in a shell still beats the file.
    """
    applied = []
    for key, value in (values or {}).items():
        if not _is_key(key):
            continue
        if not override and os.environ.get(key) not in (None, ""):
            continue
        os.environ[key] = str(value)
        applied.append(key)
    return applied


def load_env(profile=None, path=None, override=False, apply=True, start=None):
    """Read the active profile and (by default) apply it to ``os.environ``.

    Returns a dict with ``profile``, ``source`` (path or None), ``values``,
    ``applied`` and ``explicit``. Never raises for a missing file.
    """
    explicit = bool(path or os.environ.get(ENV_FILE_VAR) or os.environ.get(ENV_PROFILE_VAR))
    resolved = resolve_env_file(profile=profile, path=path, start=start)
    info = {
        "profile": active_profile(profile),
        "source": resolved,
        "values": {},
        "applied": [],
        "explicit": explicit,
    }
    if not resolved:
        return info
    with open(resolved, "rb") as handle:
        raw = handle.read()
    values = parse_env_text(raw.decode("utf-8-sig"))
    info["values"] = values
    if apply:
        info["applied"] = apply_env(values, override=override)
    return info


_AUTOLOAD = {"done": False, "info": None}


def autoload(force=False, start=None):
    """Apply the active profile once per process. Safe to call repeatedly.

    A profile that fails to load is recorded instead of raised: this runs on
    plain ``import bigqmt_signal_trader.xtquant_compat``, where an unreadable
    dotfile must not take the client down.
    """
    if _AUTOLOAD["done"] and not force:
        return _AUTOLOAD["info"]
    # Escape hatch for a harness that must not pick up a developer's local
    # profile (a test run, a build): BIGQMT_ENV_DISABLE=1 turns it off.
    if _as_bool(os.environ.get(DISABLE_VAR), False):
        info = {
            "profile": active_profile(),
            "source": None,
            "values": {},
            "applied": [],
            "explicit": False,
            "disabled": True,
        }
        _AUTOLOAD["done"] = True
        _AUTOLOAD["info"] = info
        return info
    try:
        info = load_env(start=start)
    except Exception as exc:  # pragma: no cover - defensive
        info = {
            "profile": active_profile(),
            "source": None,
            "values": {},
            "applied": [],
            "explicit": False,
            "error": str(exc),
        }
    _AUTOLOAD["done"] = True
    _AUTOLOAD["info"] = info
    return info


def loaded_info():
    """The profile applied by :func:`autoload`, or ``None`` if it never ran."""
    return _AUTOLOAD["info"]


def is_authoritative():
    """True when an explicitly selected profile is in force.

    ``xtquant_compat.load_client_config`` skips the discovered ``*_config.py``
    module in that case, so ``BIGQMT_ENV=prod`` cannot be silently overridden
    by a stale config file left next to the script.
    """
    info = _AUTOLOAD["info"]
    return bool(info and info.get("source") and info.get("explicit"))


def reset_autoload():
    """Forget the autoload result (tests, or switching profiles in-process)."""
    _AUTOLOAD["done"] = False
    _AUTOLOAD["info"] = None


def describe(info=None):
    """Human-readable summary, secrets masked."""
    info = info or loaded_info() or autoload()
    secrets = ("PASSWORD", "SECRET", "TOKEN")
    lines = [
        "profile : %s" % info.get("profile"),
        "source  : %s" % (info.get("source") or "(none found)"),
    ]
    if info.get("error"):
        lines.append("error   : %s" % info["error"])
    values = info.get("values") or {}
    if not values:
        return "\n".join(lines)
    width = max(len(name) for name in values)
    lines.append("values  :")
    for name in sorted(values):
        value = values[name]
        if value and any(word in name.upper() for word in secrets):
            value = "***"
        lines.append("  %-*s = %s" % (width, name, value))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# config generation
# ----------------------------------------------------------------------

def _as_bool(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _account_type(value):
    """A single type name, or the list form ``["STOCK", "HUGANGTONG"]``."""
    text = str(value or "").strip()
    if not text:
        return "STOCK"
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            names = [str(item).strip().upper() for item in parsed if str(item).strip()]
            if names:
                return names if len(names) > 1 else names[0]
    return text.upper()


def answers_from_env(values=None, env=None):
    """Build the ``bigqmt-init`` answers dict from profile values.

    Reuses ``init_config``'s renderers, so a generated file is byte-for-byte
    the same shape as one produced interactively -- including the derived zmq
    port and the server-side switch defaults.
    """
    source = dict(os.environ) if env is None else dict(env)
    source.update(values or {})
    transport = str(source.get("BIGQMT_RPC_TRANSPORT") or "zmq").strip().lower()
    account_id = str(source.get("BIGQMT_ACCOUNT_ID") or "").strip()
    if transport == "zmq":
        host = str(source.get("BIGQMT_ZMQ_HOST") or "127.0.0.1").strip()
        raw_port = str(source.get("BIGQMT_ZMQ_PORT") or "").strip()
        if raw_port:
            port = int(raw_port)
        else:
            from .transports.zmq_transport import _default_zmq_port

            port = _default_zmq_port(account_id)
    else:
        host = str(source.get("BIGQMT_REDIS_HOST") or "127.0.0.1").strip()
        port = int(str(source.get("BIGQMT_REDIS_PORT") or "6379").strip())
    return {
        "account_id": account_id,
        "account_type": _account_type(source.get("BIGQMT_ACCOUNT_TYPE")),
        "transport": transport,
        "host": host,
        "port": port,
        "db": int(str(source.get("BIGQMT_REDIS_DB") or "5").strip()),
        "username": str(source.get("BIGQMT_REDIS_USERNAME") or ""),
        "password": str(source.get("BIGQMT_REDIS_PASSWORD") or ""),
        "allow_order_methods": _as_bool(source.get("BIGQMT_ALLOW_ORDER_METHODS"), False),
        "deployment": "package",
        "qmt_python_dir": str(source.get("BIGQMT_QMT_PYTHON_DIR") or ""),
        "client_dir": str(source.get("BIGQMT_CLIENT_DIR") or ""),
    }


