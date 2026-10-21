"""Local stand-in for the checks CI would run.

hassfest and the HACS action run on GitHub; this approximates the parts of
them that can be checked with no network at all, plus the cross-file
consistency that nothing else checks: translation keys against icons,
exceptions raised against exceptions declared, user-facing exceptions raised
without a translation key, actions registered against actions described, the
three version fields against each other, the quality scale against the pinned
rule list, the documentation and issue-tracker URLs against hosts a user
cannot open, and every published text file against private IPv4 and IPv6
address literals, private-suffix and dotless URL hosts, plus any names in
HA_DEV_HOST_NAMES. Every file git lists is scanned except the binary suffixes
and the one exempt file, and a file that will not decode as UTF-8 is reported
rather than skipped. Run it before a push so the push is not the first
verification.

    python tools/validate_local.py
    HA_DEV_HOST_NAMES=<name>,<name> python tools/validate_local.py
"""

from __future__ import annotations

import ast
import ipaddress
import json
import os
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

# tools/ is not on sys.path when this file is run by path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _netblocks

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DOMAIN = "sslreverseproxy"
COMP = os.path.join(ROOT, "custom_components", DOMAIN)
PLATFORMS = ("binary_sensor", "button", "sensor")

# The hacs.json keys HACS documents and the Marketplace reads.
HACS_JSON_KEYS = {
    "content_in_root",
    "country",
    "filename",
    "hacs",
    "hide_default_branch",
    "homeassistant",
    "name",
    "persistent_directory",
    "zip_release",
}

# hassfest requires these for a custom integration.
REQUIRED_MANIFEST = [
    "domain",
    "name",
    "documentation",
    "codeowners",
    "iot_class",
    "version",
]
VALID_IOT_CLASS = {
    "assumed_state",
    "cloud_polling",
    "cloud_push",
    "local_polling",
    "local_push",
    "calculated",
}

# Pinned from developers.home-assistant.io/docs/core/integration-quality-scale/checklist
# (checked 2026-10-21: 54 rules, none new or deprecated). The list is pinned
# here on purpose: a quality_scale.yaml that is missing a rule reads as
# complete, and checking against the full list turns an omission into a
# failure.
ALL_RULES = {
    # Bronze
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-flow-test-coverage",
    "config-flow",
    "dependency-transparency",
    "docs-actions",
    "docs-conditions",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "docs-triggers",
    "entity-event-setup",
    "entity-unique-id",
    "has-entity-name",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "unique-config-entry",
    # Silver
    "action-exceptions",
    "config-entry-unloading",
    "docs-configuration-parameters",
    "docs-installation-parameters",
    "entity-unavailable",
    "integration-owner",
    "log-when-unavailable",
    "parallel-updates",
    "reauthentication-flow",
    "test-coverage",
    # Gold
    "devices",
    "diagnostics",
    "discovery-update-info",
    "discovery",
    "docs-data-update",
    "docs-examples",
    "docs-known-limitations",
    "docs-supported-devices",
    "docs-supported-functions",
    "docs-troubleshooting",
    "docs-use-cases",
    "dynamic-devices",
    "entity-category",
    "entity-device-class",
    "entity-disabled-by-default",
    "entity-translations",
    "exception-translations",
    "icon-translations",
    "reconfiguration-flow",
    "repair-issues",
    "stale-devices",
    # Platinum
    "async-dependency",
    "inject-websession",
    "strict-typing",
}

failures: list[str] = []
notes: list[str] = []


def read(*parts: str) -> str:
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def read_json(*parts: str) -> Any:
    return json.loads(read(*parts))


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def constants(source: str, prefix: str) -> dict[str, str]:
    """Module-level string assignments whose name starts with prefix."""
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        # `NAME: Final = "x"` and `NAME = "x"` alike.
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            target: ast.expr = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and target.id.startswith(prefix)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found[target.id] = node.value.value
    return found


# Every exception a user can see on the integration card or in an action
# error. A raise of one of these without translation_key shows an English
# f-string to every user, whatever their language.
TRANSLATED_EXCEPTIONS = {
    "HomeAssistantError",
    "ServiceValidationError",
    "ConfigEntryNotReady",
    "ConfigEntryAuthFailed",
    "ConfigEntryError",
    "UpdateFailed",
}


def untranslated_raises(source: str, filename: str) -> list[str]:
    """Raises of Home Assistant's user-facing exceptions that carry no key."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name not in TRANSLATED_EXCEPTIONS:
            continue
        keywords = {kw.arg for kw in node.exc.keywords}
        if "translation_key" not in keywords:
            found.append(
                f"{filename}:{node.lineno} raises {name} without translation_key"
            )
    return found


def pyproject_version() -> str | None:
    """The version in pyproject.toml, or None when the file does not carry one."""
    path = os.path.join(ROOT, "pyproject.toml")
    if not os.path.isfile(path):
        return None
    import tomllib

    with open(path, "rb") as fh:
        project = tomllib.load(fh).get("project", {})
    version = project.get("version")
    return str(version) if version is not None else None


REPO_ALLOWED_HOSTS = frozenset(
    {
        # the hostname the KVM reports about itself, in api.py, the flow and
        # the fixtures
        "localhost.localdomain",
        # the zeroconf name in tests/ha/test_config_flow.py
        "gl-rm10-215.local",
    }
)


# ------------------------------------------------- development-host refusal
# hassfest and the HACS action read the manifest and nothing else, so a
# development address anywhere in the tree - a workflow comment, a README, a
# docstring - ships with every check green. Two rules, one strict and one
# narrower: the manifest URLs are refused for anything a user cannot open,
# and every published file is refused for anything that names this network.
MANIFEST_NETS = tuple(
    ipaddress.ip_network(c)
    for c in _netblocks.TREE_CIDRS + _netblocks.MANIFEST_ONLY_CIDRS
)
TREE_NETS = tuple(ipaddress.ip_network(c) for c in _netblocks.TREE_CIDRS)
MANIFEST_ONLY_NAMES = ("localhost",)
PRIVATE_SUFFIXES = _netblocks.PRIVATE_SUFFIXES

# Hosts that look like a development host and are not one. Every entry is
# load-bearing in this repository and carries the reason on its line; an
# entry added without one is how the rule stops working.
ALLOWED_HOSTS = frozenset(
    {
        # the default Home Assistant address, in every install document
        "homeassistant.local",
    }
    | REPO_ALLOWED_HOSTS
)

# Development host names are matched too, and they cannot be listed here:
# naming them in a published file is the disclosure this rule exists to
# prevent. The environment carries them in from outside the tree.
DEV_HOST_ENV = "HA_DEV_HOST_NAMES"


def internal_names() -> list[str]:
    """Development host names, comma separated, from the environment."""
    return [n.strip() for n in os.environ.get(DEV_HOST_ENV, "").split(",") if n.strip()]


# Text that ships to whoever clones or installs the repository. The file list
# comes from git rather than a walk: git already knows what is ignored, which
# is how private operational notes under an ignored directory stay out, and
# --others adds a file staged for this commit but not yet added.
#
# A deny-list of binary suffixes, not an allow-list of text ones. An allow-list
# passes every file type nobody listed, which is how a shell script, a
# Dockerfile or a .env.example would ship unscanned; the deny-list only has to
# name the files whose bytes are not text.
BINARY_SUFFIXES = {
    ".bin",
    ".bmp",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mo",
    ".otf",
    ".pdf",
    ".png",
    ".pyc",
    ".pyd",
    ".so",
    ".tar",
    ".ttf",
    ".webp",
    ".whl",
    ".woff",
    ".woff2",
    ".zip",
}
# Build artefacts the walk fallback drops, the file half of the directory list
# it already prunes. git excludes them itself, so this list only applies when
# git is absent. .coverage is a SQLite database with no suffix to deny, and
# under the deny-list it would otherwise be read, fail to decode and report a
# failure against a file that never ships.
WALK_SKIP_NAMES = {".coverage", "coverage.xml"}
# The one published file the scan skips: it holds the CIDRs the scan matches
# on, so it would report itself. Nothing else may live in it.
SCAN_EXEMPT = ("tools/_netblocks.py",)

IP_LITERAL_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# Colon-separated hex groups, deliberately loose. Every candidate goes to
# blocked_address, which parses it, so a MAC address, a clock time or a Python
# slice costs one failed parse and yields no hit.
IPV6_LITERAL_RE = re.compile(
    r"(?<![0-9A-Za-z:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![0-9A-Za-z:])"
)
# The bracketed-host form is matched first. The general form's character class
# ends at the closing bracket, which hands urlsplit an unterminated IPv6 URL to
# raise on rather than a host to judge.
URL_RE = re.compile(
    r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://"
    r"(?:\[[0-9A-Fa-f:.]+\][^\s\"'`<>)\]},]*|[^\s\"'`<>)\]},]+)"
)
# A host written in prose with no scheme. The suffix must end the name:
# \b would match the "home" of home-assistant.io.
BARE_HOST_RE = re.compile(
    r"(?<![\w.-])(?:[a-z0-9][a-z0-9-]*\.)+"
    r"(?:corp|home|home\.arpa|intranet|internal|lan|local|localdomain)(?![\w-])",
    re.IGNORECASE,
)
# A host made of anything else is a template - f"http://{host}/" - not a host.
HOST_CHARS_RE = re.compile(r"^[a-z0-9.\-\[\]:]+$", re.IGNORECASE)


def is_netmask(text: str) -> bool:
    """A dotted quad written as a contiguous subnet mask, 255.255.255.0 and up.

    Every such mask sits in the top reserved block and would otherwise be
    refused as a reserved address. The all-zero mask is a mask too, and is
    deliberately not exempt: as a host it is the unspecified address.
    """
    if not text.startswith("255."):
        return False
    try:
        value = int(ipaddress.IPv4Address(text))
    except ipaddress.AddressValueError:
        return False
    inverted = (~value) & 0xFFFFFFFF
    return inverted & (inverted + 1) == 0


def blocked_address(text: str, nets: tuple[Any, ...]) -> bool:
    """Whether text is an address literal inside one of nets."""
    if is_netmask(text):
        return False
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return any(address in net for net in nets)


def blocked_host(host: str, nets: tuple[Any, ...], names: tuple[str, ...] = ()) -> bool:
    """Whether a hostname resolves or routes inside one network only.

    `names` are hosts refused by spelling rather than by address family. Only
    the manifest rule passes any: "localhost" names no machine on this
    network, so it is a dead documentation link but not a disclosure.
    """
    host = host.strip().rstrip(".").lower()
    if not host or host in ALLOWED_HOSTS:
        return False
    if host in names:
        return True
    if blocked_address(host, nets):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # A literal outside nets is a public address, whatever its shape.
        return False
    if host.endswith(PRIVATE_SUFFIXES):
        return True
    # A name with no dot is resolved against whatever search domain the reader
    # happens to have, so it names a machine on a LAN rather than on the net.
    return "." not in host


def unreachable_host(url: str) -> str | None:
    """The host of a manifest URL no user outside this network can open."""
    if not isinstance(url, str) or not url:
        return None
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return None
    if host and not HOST_CHARS_RE.match(host):
        return None
    return host if blocked_host(host, MANIFEST_NETS, MANIFEST_ONLY_NAMES) else None


def malformed_url(url: Any) -> bool:
    """A manifest URL that is not an absolute http(s) URL with a host.

    Separate from unreachable_host, whose answer is a host or None: a value
    like "not-a-url" or an ftp:// URL has no host to report and would pass.
    """
    if not isinstance(url, str) or not url:
        return True
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    return parts.scheme not in {"http", "https"} or not parts.hostname


def published_files() -> list[str]:
    """Every text file that ships, relative to ROOT, from git's own index.

    Falls back to a walk when git is not there - an extracted tarball - so the
    rule still runs, and says so, rather than passing on an empty list.
    """
    paths: list[str] = []
    try:
        listing = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        listing = None
    if listing is not None and listing.returncode == 0:
        paths = [p for p in listing.stdout.split("\0") if p]
    else:
        notes.append("git not available - the tree scan walked the directory instead")
        for dirpath, dirs, files in os.walk(ROOT):
            dirs[:] = [
                d
                for d in dirs
                if d not in {"__pycache__", "venv", "htmlcov", "node_modules"}
                and not (d.startswith(".") and d not in {".gitea", ".github"})
            ]
            for f in files:
                if f in WALK_SKIP_NAMES:
                    continue
                paths.append(
                    os.path.relpath(os.path.join(dirpath, f), ROOT).replace("\\", "/")
                )
    keep: list[str] = []
    for path in paths:
        if path in SCAN_EXEMPT:
            continue
        name = path.rsplit("/", 1)[-1]
        if os.path.splitext(name)[1].lower() not in BINARY_SUFFIXES:
            keep.append(path)
    return sorted(keep)


def tree_hits(text: str, name_re: Any = None) -> list[tuple[int, str]]:
    """Every development host named in text, as (line number, host)."""
    hits: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if name_re is not None:
            for name in name_re.findall(line):
                if name.lower() not in ALLOWED_HOSTS:
                    hits.append((number, name.lower()))
        for literal in IP_LITERAL_RE.findall(line):
            if literal not in ALLOWED_HOSTS and blocked_address(literal, TREE_NETS):
                hits.append((number, literal))
        for literal in IPV6_LITERAL_RE.findall(line):
            if literal not in ALLOWED_HOSTS and blocked_address(literal, TREE_NETS):
                hits.append((number, literal))
        for url in URL_RE.findall(line):
            try:
                host = urlsplit(url).hostname or ""
            except ValueError:
                continue
            if not host or not HOST_CHARS_RE.match(host):
                continue
            if blocked_host(host, TREE_NETS):
                hits.append((number, host))
        for name in BARE_HOST_RE.findall(line):
            if name.lower() not in ALLOWED_HOSTS:
                hits.append((number, name.lower()))
    return hits


def scan_published_tree() -> None:
    """Refuse a development host anywhere in the published tree."""
    exempt = os.path.join(ROOT, *SCAN_EXEMPT[0].split("/"))
    if os.path.isfile(exempt):
        allowed_names = {"TREE_CIDRS", "MANIFEST_ONLY_CIDRS", "PRIVATE_SUFFIXES"}
        for node in ast.parse(read(exempt)).body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            targets = node.targets if isinstance(node, ast.Assign) else []
            if not all(
                isinstance(t, ast.Name) and t.id in allowed_names for t in targets
            ):
                failures.append(
                    f"{SCAN_EXEMPT[0]} holds more than the pinned address space; "
                    "the tree scan skips this file, so nothing else may live in it"
                )
                break
    names = internal_names()
    name_re = None
    if names:
        name_re = re.compile(
            r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\w*",
            re.IGNORECASE,
        )
    plural = "name" if len(names) == 1 else "names"
    notes.append(f"{len(names)} development host {plural} from {DEV_HOST_ENV}")
    seen = 0
    for path in published_files():
        full = os.path.join(ROOT, *path.split("/"))
        if not os.path.isfile(full):
            continue
        try:
            text = read(full)
        except OSError, UnicodeDecodeError:
            failures.append(
                f"{path} could not be read as UTF-8 text and its suffix is not "
                "in BINARY_SUFFIXES, so the scan proved nothing about it"
            )
            continue
        seen += 1
        for number, host in tree_hits(text, name_re):
            failures.append(
                f"{path}:{number} names {host} - that host is on the development "
                "network and means nothing to a user who installs this"
            )
    check(seen > 0, "the published-tree scan read no files, so it proved nothing")


# Entity methods that act on the control plane when a user presses or sets them.
ENTITY_COMMANDS = frozenset(
    {
        "async_press",
        "async_turn_on",
        "async_turn_off",
        "async_toggle",
        "async_select_option",
        "async_set_native_value",
        "async_set_value",
    }
)


def _starts_with_admin_check(fn: ast.AsyncFunctionDef) -> bool:
    """Whether the first statement after any docstring is the admin check.

    The check is `await require_admin(self.hass, self._context)`; the context may
    be bound there (`context := self._context`) for the write that follows.
    """
    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body or not isinstance(body[0], ast.Expr):
        return False
    awaited = body[0].value
    if not isinstance(awaited, ast.Await) or not isinstance(awaited.value, ast.Call):
        return False
    call = awaited.value
    args = [a.value if isinstance(a, ast.NamedExpr) else a for a in call.args]
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "require_admin"
        and [ast.unparse(a) for a in args] == ["self.hass", "self._context"]
        and not call.keywords
    )


def _only_override(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether the method's decorators leave it as written; any other may replace it."""
    return all(
        (isinstance(d, ast.Name) and d.id == "override")
        or (isinstance(d, ast.Attribute) and d.attr == "override")
        for d in fn.decorator_list
    )


def entity_commands() -> list[tuple[str, str, bool]]:
    """Each entity command method in a class body, and if it follows the convention.

    The convention: async, decorated by `override` at most, first statement the
    check. The guarantee is tests/ha/test_actions.py, which presses every entity
    the integration creates as an ordinary user.
    """
    found = []
    for f in sorted(os.listdir(COMP)):
        if not f.endswith(".py"):
            continue
        tree = ast.parse(read(COMP, f))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if fn.name not in ENTITY_COMMANDS:
                    continue
                checked = (
                    isinstance(fn, ast.AsyncFunctionDef)
                    and _only_override(fn)
                    and _starts_with_admin_check(fn)
                )
                found.append((f, f"{cls.name}.{fn.name}", checked))
    return found


def main() -> int:
    manifest = read_json(COMP, "manifest.json")
    const_src = read(COMP, "const.py")
    strings = read_json(COMP, "translations", "en.json")

    # ---------------------------------------------------------- manifest
    for key in REQUIRED_MANIFEST:
        check(key in manifest, f"manifest.json missing required key {key!r}")
    check(
        manifest.get("domain") == DOMAIN,
        f"manifest domain is {manifest.get('domain')!r}",
    )
    check(
        manifest.get("iot_class") in VALID_IOT_CLASS,
        f"manifest iot_class {manifest.get('iot_class')!r} is not a valid value",
    )
    check(
        isinstance(manifest.get("codeowners"), list)
        and all(c.startswith("@") for c in manifest["codeowners"]),
        "manifest codeowners entries must start with @",
    )
    keys = list(manifest)
    check(
        keys[:2] == ["domain", "name"] and keys[2:] == sorted(keys[2:]),
        "manifest keys must be domain, name, then alphabetical (hassfest MANIFEST)",
    )
    # HACS serves both links to strangers. A development or LAN URL here
    # passes every other check and gives a user a page they cannot open.
    for key in ("documentation", "issue_tracker"):
        url = manifest.get(key)
        host = unreachable_host(url) if isinstance(url, str) else None
        check(
            host is None,
            f"manifest {key} points at {host!r}, which is not reachable "
            f"outside this LAN",
        )
    check(
        "quality_scale" not in manifest,
        "quality_scale in manifest.json: the badge is core-only, a custom "
        "integration builds to the rules and does not claim a tier",
    )

    const_version = constants(const_src, "VERSION").get("VERSION")
    check(
        const_version == manifest.get("version"),
        f"const.VERSION {const_version!r} != manifest version "
        f"{manifest.get('version')!r} - HA reports one and HACS the other",
    )
    project_version = pyproject_version()
    if project_version is not None:
        check(
            project_version == manifest.get("version"),
            f"pyproject version {project_version!r} != manifest version "
            f"{manifest.get('version')!r} - bump them together",
        )

    # Optional: the hassfest image of Home Assistant 2026.10 refuses the key,
    # and the HACS action only warns when it is missing.
    categories = manifest.get("categories")
    check(
        categories is None
        or (
            isinstance(categories, list)
            and 1 <= len(categories) <= 3
            and len(set(categories)) == len(categories)
            and all(
                isinstance(c, str) and re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)?", c)
                for c in categories
            )
        ),
        "manifest categories must be one to three integration categories "
        "(the HACS action checks them against core's list)",
    )

    # ---------------------------------------------------------- hacs.json
    hacs = read_json(ROOT, "hacs.json")
    check("name" in hacs, "hacs.json must contain name")
    check(
        set(hacs) <= HACS_JSON_KEYS,
        f"hacs.json keys {sorted(set(hacs) - HACS_JSON_KEYS)} are not ones "
        "HACS or the Marketplace read",
    )

    # ---------------------------------------------------------- brand images
    brand = os.path.join(COMP, "brand")
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        check(os.path.isfile(os.path.join(brand, name)), f"missing brand/{name}")

    # ---------------------------------------------------------- translations
    # A custom integration ships translations/en.json as the source and the
    # fallback; strings.json is compiled only in core, so it ships unused.
    check(
        not os.path.exists(os.path.join(COMP, "strings.json")),
        "strings.json is core-only - edit translations/en.json",
    )

    def leaves(node: object, path: str) -> list[tuple[str, str]]:
        if isinstance(node, dict):
            return [x for k, v in node.items() for x in leaves(v, f"{path}.{k}")]
        return [(path.lstrip("."), node)] if isinstance(node, str) else []

    for where, text in leaves(strings, ""):
        # hassfest: "the string should not contain URLs, please use description
        # placeholders instead".
        check(
            "http://" not in text and "https://" not in text,
            f"en.json {where} contains a URL - use a description placeholder",
        )
        # Nothing resolves a reference outside core: the user reads it raw.
        check(
            "[%key:" not in text,
            f"en.json {where} holds a [%key:...%] reference - write it out",
        )

    # ---------------------------------------------------------- actions
    services_yaml = os.path.join(COMP, "services.yaml")
    check(os.path.isfile(services_yaml), "services.yaml is missing")
    service_consts = set(constants(const_src, "SERVICE_").values())
    declared_services = set(strings.get("services", {}))
    check(
        declared_services == service_consts,
        f"en.json describes {sorted(declared_services)} but const.py "
        f"registers {sorted(service_consts)}",
    )
    try:
        import yaml

        services = yaml.safe_load(read(services_yaml)) or {}
        check(
            set(services) == service_consts,
            f"services.yaml declares {sorted(services)} but const.py registers "
            f"{sorted(service_consts)}",
        )
        for name, spec in services.items():
            yaml_fields = set((spec or {}).get("fields", {}))
            described = set(strings.get("services", {}).get(name, {}).get("fields", {}))
            check(
                yaml_fields == described,
                f"action {name}: services.yaml fields {sorted(yaml_fields)} != "
                f"en.json fields {sorted(described)}",
            )
            for field_spec in (spec or {}).get("fields", {}).values():
                selector = (field_spec or {}).get("selector", {})
                tkey = (selector.get("select") or {}).get("translation_key")
                if tkey:
                    check(
                        tkey in strings.get("selector", {}),
                        f"selector translation {tkey!r} missing from en.json",
                    )
    except ImportError:
        check(
            False,
            "PyYAML not installed - services.yaml was not parsed, so its "
            "checks did not run; run under the repo .venv",
        )

    # ---------------------------------------------------------- quality scale
    scale_path = os.path.join(COMP, "quality_scale.yaml")
    check(os.path.isfile(scale_path), "quality_scale.yaml is missing")
    declared: dict[str, Any] = {}
    if os.path.isfile(scale_path):
        try:
            import yaml

            declared = yaml.safe_load(read(scale_path)).get("rules", {})
            missing = ALL_RULES - set(declared)
            check(not missing, f"quality_scale.yaml does not mention {sorted(missing)}")
            unknown = set(declared) - ALL_RULES
            check(not unknown, f"quality_scale.yaml invents rules {sorted(unknown)}")
            for rule, value in sorted(declared.items()):
                if isinstance(value, dict):
                    check(
                        value.get("status") in {"done", "todo", "exempt"},
                        f"{rule}: status must be done/todo/exempt",
                    )
                    if value.get("status") != "done":
                        check(
                            bool(str(value.get("comment", "")).strip()),
                            f"{rule}: a non-done status needs a comment saying why",
                        )
                else:
                    check(value == "done", f"{rule}: bare value must be 'done'")
            todo = sorted(
                r
                for r, v in declared.items()
                if isinstance(v, dict) and v.get("status") == "todo"
            )
            if todo:
                notes.append(f"quality scale still todo: {', '.join(todo)}")
        except ImportError:
            check(
                False,
                "PyYAML not installed - quality_scale.yaml was not parsed, so its "
                "checks did not run; run under the repo .venv",
            )

    # ------------------------------------------------- quality scale evidence
    # A rule filed `done` that the file set contradicts is worse than one
    # filed `todo`: the todo gets read, the done does not. Each check below is
    # the smallest fact that would be false if the mechanism were absent.
    flow_src = read(COMP, "config_flow.py")

    entity_src = read(COMP, "entity.py")
    workflows = ""
    for root in (".github", ".gitea"):
        folder = os.path.join(ROOT, root, "workflows")
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder)):
                workflows += read(folder, name)

    def status(rule: str) -> str:
        value = declared.get(rule)
        if isinstance(value, dict):
            return str(value.get("status", ""))
        return str(value or "")

    def needs(rule: str, condition: bool, message: str) -> None:
        if status(rule) == "done":
            check(condition, f"{rule} is done but {message}")

    # Discovery is a manifest key plus the flow step that answers it.
    discovery_keys = ("zeroconf", "dhcp", "ssdp", "bluetooth", "homekit", "usb")
    found_discovery = [key for key in discovery_keys if key in manifest]
    needs(
        "discovery",
        bool(found_discovery),
        "manifest.json declares no discovery method",
    )
    for key in found_discovery:
        needs(
            "discovery",
            f"async_step_{key}(" in flow_src,
            f"manifest declares {key} with no async_step_{key} to answer it",
        )
    needs(
        "discovery-update-info",
        "_abort_if_unique_id_configured(updates=" in flow_src
        or "async_update_entry(" in flow_src,
        "no discovery path updates a configured entry's connection details",
    )
    needs(
        "reauthentication-flow",
        "async_step_reauth_confirm(" in flow_src
        and "_abort_if_unique_id_mismatch(" in flow_src,
        "reauth does not check that the unit answering is still this entry's",
    )
    needs(
        "reconfiguration-flow",
        "async_step_reconfigure(" in flow_src,
        "there is no reconfigure step",
    )
    needs(
        "entity-unavailable",
        entity_src.count("def available(") >= 2
        and "in self.coordinator.data" in entity_src,
        "no entity goes unavailable when the API stops listing its row",
    )
    needs(
        "test-coverage",
        "--cov-fail-under" in workflows,
        "no workflow gates on a coverage threshold",
    )
    needs(
        "strict-typing",
        "strict = true" in read(ROOT, "pyproject.toml"),
        "pyproject.toml does not run mypy in strict mode",
    )
    for relaxation in (
        "disallow_subclassing_any = false",
        "disallow_untyped_decorators = false",
        "warn_unused_ignores = false",
        "disallow_untyped_defs = false",
        "ignore_errors = true",
    ):
        needs(
            "strict-typing",
            relaxation not in read(ROOT, "pyproject.toml").split("[[tool.mypy")[0],
            f"pyproject.toml relaxes mypy with {relaxation}",
        )

    # --------------------------------------------------- config flow strings
    # Every step the flow shows, every error it can put on a form and every
    # reason it aborts with has to have text. A missing one shows the user a
    # translation key.
    config_strings = strings.get("config", {})
    for step in set(re.findall(r'step_id="([a-z_]+)"', flow_src)):
        check(
            step in config_strings.get("step", {}),
            f"config flow shows step {step!r} with no en.json entry",
        )
    for step in config_strings.get("step", {}):
        check(
            f"async_step_{step}(" in flow_src,
            f"en.json describes step {step!r} that the flow does not have",
        )
    for error in set(re.findall(r'\{"base": "([a-z_]+)"\}', flow_src)):
        check(
            error in config_strings.get("error", {}),
            f"config flow returns error {error!r} with no en.json entry",
        )
    # The three Home Assistant raises itself, whatever the flow says.
    abort_reasons = set(re.findall(r'reason="([a-z_]+)"', flow_src)) | {
        "already_configured",
        "reconfigure_successful",
        "reauth_successful",
    }
    for reason in abort_reasons:
        check(
            reason in config_strings.get("abort", {}),
            f"config flow aborts with {reason!r} with no en.json entry",
        )
    for reason in config_strings.get("abort", {}):
        check(
            reason in abort_reasons,
            f"en.json declares unused abort reason {reason!r}",
        )

    # ------------------------------------------------------ icon translations
    # Every translation key an entity uses needs an icon and a name, and every
    # icon and name needs an entity using it. Both forms are matched: the
    # class attribute and the EntityDescription keyword.
    icons = read_json(COMP, "icons.json")
    # The entity bases take the key positionally: super().__init__(..., "key")
    # and the duration helper _ms("key", ...).
    key_re = re.compile(
        r"(?:_attr_translation_key\s*=|\btranslation_key=|"
        r'super\(\)\.__init__\([^()"]*|\b_ms\(\s*)\s*"([^"]+)"'
    )
    # An exception is raised with translation_domain=DOMAIN right before its
    # key; entity and issue keys never carry translation_domain. Subtracted
    # here so an error raised inside a platform file is not read as one of
    # that platform's entities.
    exc_re = re.compile(r'translation_domain=DOMAIN,\s*translation_key="([^"]+)"')
    for platform in PLATFORMS:
        source = read(COMP, f"{platform}.py")
        used = set(key_re.findall(source)) - set(exc_re.findall(source))
        declared_icons = set(icons.get("entity", {}).get(platform, {}))
        named = set(strings.get("entity", {}).get(platform, {}))
        check(
            used == declared_icons,
            f"{platform}: icons {sorted(declared_icons ^ used)} out of step",
        )
        check(used == named, f"{platform}: names {sorted(named ^ used)} out of step")
    service_icons = set(icons.get("services", {}))
    check(
        service_icons == service_consts,
        f"icons.json services {sorted(service_icons)} != {sorted(service_consts)}",
    )

    # ------------------------------------------------- exception translations
    # Two checks: every key raised is declared (and vice versa), and no
    # user-facing exception is raised without a key anywhere in the
    # component - setup and poll failures in __init__.py and coordinator.py
    # included, since those show on the integration card.
    raised: set[str] = set()
    for f in sorted(os.listdir(COMP)):
        if f.endswith(".py"):
            source = read(COMP, f)
            raised |= set(exc_re.findall(source))
            for message in untranslated_raises(source, f):
                failures.append(message)
    declared_exc = set(strings.get("exceptions", {}))
    check(
        raised <= declared_exc,
        f"code raises undeclared exception keys {sorted(raised - declared_exc)}",
    )
    check(
        declared_exc <= raised,
        f"en.json declares unused exceptions {sorted(declared_exc - raised)}",
    )

    # ----------------------------------------------------- issue translations
    issue_consts = set(constants(const_src, "ISSUE_").values())
    declared_issues = set(strings.get("issues", {}))
    check(
        issue_consts == declared_issues,
        f"const.py issues {sorted(issue_consts)} != en.json issues "
        f"{sorted(declared_issues)}",
    )

    # ------------------------------------------------------------ platforms
    init_src = read(COMP, "__init__.py")
    for platform in PLATFORMS:
        check(
            f"Platform.{platform.upper()}" in init_src,
            f"{platform}.py exists but Platform.{platform.upper()} is not forwarded",
        )
        check(
            "PARALLEL_UPDATES" in read(COMP, f"{platform}.py"),
            f"{platform}.py does not set PARALLEL_UPDATES",
        )

    # ------------------------------------------------------------ privilege
    # Reload and Renew change the proxy, so only an administrator may use
    # them: the actions register as admin services and every button press
    # checks the caller (AGENTS.md house rule, from frenck on hacs/default
    # #11057 and #11059).
    for f in sorted(os.listdir(COMP)):
        if f.endswith(".py"):
            check(
                "hass.services.async_register(" not in read(COMP, f),
                f"{f} registers an action with hass.services.async_register; "
                "use async_register_admin_service",
            )
    services_src = read(COMP, "services.py")
    check(
        services_src.count("async_register_admin_service(") == len(service_consts),
        "every action registers with async_register_admin_service",
    )
    commands = entity_commands()
    check(bool(commands), "no button press found to check for the administrator check")
    for f, method, checked in commands:
        check(checked, f"{f} {method} does not start with the administrator check")

    # ---------------------------------------------- development-host refusal
    for _key in ("documentation", "issue_tracker"):
        if _key in manifest:
            check(
                not malformed_url(manifest.get(_key)),
                f"manifest {_key} is not an absolute http(s) URL a user can open",
            )
    scan_published_tree()

    # ---------------------------------------------------------- syntax
    for dirpath, _dirs, files in os.walk(COMP):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(dirpath, f)
                try:
                    ast.parse(read(path))
                except SyntaxError as err:
                    failures.append(f"{f}: {err}")

    # ---------------------------------------------------------- report
    print(f"manifest {manifest.get('domain')} {manifest.get('version')}")
    for n in notes:
        print(f"  NOTE   {n}")
    for f in failures:
        print(f"  FAIL   {f}")
    if not failures:
        print("  all offline checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
