"""Generates a MariaDB repository file using MariaDB's own repo-setup scripts.

Outside myharem's core idea — nothing here deploys anything — but it is the
question that comes up beside it constantly: "what should this customer's
mariadb.repo actually say for version X on distro Y?"

**It wraps the official scripts rather than reimplementing them.** They are
~1000 (Community) and ~1300 (Enterprise) lines that already know every
supported distro, suite and architecture, and they are what the MariaDB docs
tell customers to run. A second implementation would be a second source of
truth, and the first support question on any repo problem would become "which
tool wrote this file?".

**It never applies anything.** Community runs with `--write-to-stdout`, which
also forces the script's own `skip_key_import`; Enterprise runs without
`--apply`, which is what gates both the file writes and the key import there.
So neither can touch /etc, neither needs root, and the output is a file you copy
to the target host yourself.
"""

import os
import re
import subprocess
from pathlib import Path

import click

from . import catalog
from . import config
from . import deployment
from . import report

#: Upstream publishes a sha256 for the Enterprise helper; there is no published
#: equivalent for the Community one, so that is fetched over TLS and not verified.
ES_CHECKSUMS_URL = (
    'https://dlm.mariadb.com/enterprise-release-helpers/checksums/sha256sums.txt'
)

OFFICIAL_SCRIPTS = {
    'CS': 'https://r.mariadb.com/downloads/mariadb_repo_setup',
    'ES': 'https://dlm.mariadb.com/enterprise-release-helpers/mariadb_es_repo_setup',
}

SCRIPT_NAMES = {
    'CS': 'mariadb_repo_setup',
    'ES': 'mariadb_es_repo_setup',
}

#: What a generated Enterprise file carries instead of the real token.
TOKEN_PLACEHOLDER = '__MARIADB_ES_TOKEN__'

#: (os_type, os_version, label) — the values the official scripts accept, which
#: are NOT always what a human would say. Ubuntu insists on the codename
#: ('jammy', not '22.04'); Debian takes either and normalises to the codename.
#: Mirrors the `$supported` string in mariadb_repo_setup; re-check it when that
#: changes, since an unsupported pair is only caught by the script itself.
TARGETS = (
    ('rhel', '8', 'RHEL / Rocky / Alma 8'),
    ('rhel', '9', 'RHEL / Rocky / Alma 9'),
    ('rhel', '10', 'RHEL / Rocky / Alma 10'),
    ('ubuntu', 'jammy', 'Ubuntu 22.04 LTS (jammy)'),
    ('ubuntu', 'noble', 'Ubuntu 24.04 LTS (noble)'),
    ('debian', 'bullseye', 'Debian 11 (bullseye)'),
    ('debian', 'bookworm', 'Debian 12 (bookworm)'),
    ('debian', 'trixie', 'Debian 13 (trixie)'),
    ('sles', '15', 'SLES 15'),
)

_DEB_FAMILY = ('debian', 'ubuntu')


def repo_filename(edition, version, os_type, os_version):
    """Named for what it is for, since that is the thing you forget.

    The target host wants it called mariadb.repo / mariadb.list; this name is
    for the directory you collect them in, so it carries the version and distro.
    """
    stem = 'mariadb-es' if edition == 'ES' else 'mariadb'
    suffix = '.list' if os_type in _DEB_FAMILY else '.repo'
    return f"{stem}-{version}-{os_type}-{os_version}{suffix}"


def install_path(os_type):
    """Where the file belongs on the target host."""
    if os_type in _DEB_FAMILY:
        return '/etc/apt/sources.list.d/mariadb.list'
    return '/etc/yum.repos.d/mariadb.repo'


def _strip_secrets(text, token=None):
    """Error text, safe to show.

    Only the Enterprise path has anything to hide: the script echoes the token
    back on a validation failure ("Invalid token format: '<token>'") and its
    repo URLs embed it as a path segment. A Community failure carries no secret
    and its message links the release documentation, so stripping URLs there
    would delete the most useful line.
    """
    if not token:
        return text
    return report.redact(text.replace(token, TOKEN_PLACEHOLDER))


def cache_dir():
    """Where the official scripts are cached.

    `<basedir>/remote` when it is writable — setup_myharem_dirs already creates
    that directory for exactly this kind of thing — and a per-user cache
    otherwise. `mh repo` is the one command that needs no root at all, and
    requiring sudo merely to cache a script would quietly take that away.
    """
    shared = config.get_basedir() / 'remote'
    try:
        shared.mkdir(parents=True, exist_ok=True)
        if os.access(shared, os.W_OK):
            return shared
    except OSError:
        pass
    base = os.environ.get('XDG_CACHE_HOME') or (Path.home() / '.cache')
    fallback = Path(base) / 'myharem'
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def published_sha256(edition):
    """The sha256 upstream publishes for this script, or None if it publishes none."""
    if edition != 'ES':
        return None
    try:
        body = catalog._fetch_url(ES_CHECKSUMS_URL)
    except Exception:
        return None
    for line in body.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip('./') == SCRIPT_NAMES['ES']:
            return parts[0].lower()
    return None


def script_path(edition, refresh=False):
    """The official script, cached locally.

    Fetched rather than vendored: a vendored copy goes stale silently, and the
    point is to produce what the official tool produces today. The user never
    has to obtain these scripts themselves.
    """
    path = cache_dir() / SCRIPT_NAMES[edition]
    if path.exists() and not refresh:
        return path
    if path.exists():
        path.unlink()
    report.log(f"Fetching {SCRIPT_NAMES[edition]} ...")
    try:
        deployment._download(OFFICIAL_SCRIPTS[edition], path, timeout=120)
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise click.ClickException(
            f"Could not fetch {SCRIPT_NAMES[edition]}: {report.redact(str(exc))}"
        ) from None

    # This script is about to be executed, so verify it where upstream makes
    # that possible. A mismatch is fatal; an unreachable checksum file is not,
    # since that would make the command depend on a second endpoint being up.
    expected = published_sha256(edition)
    if expected:
        actual = deployment._sha256(path)
        if actual != expected:
            path.unlink(missing_ok=True)
            raise click.ClickException(
                f"{SCRIPT_NAMES[edition]} does not match the sha256 published at "
                f"{ES_CHECKSUMS_URL} (expected {expected}, got {actual}). "
                f"Refusing to run it."
            )
    elif edition == 'ES':
        report.warn(
            "Could not fetch the published checksum for "
            f"{SCRIPT_NAMES[edition]}; it was downloaded over TLS but not verified."
        )
    return path


def build_args(edition, version, os_type, os_version, arch, token=None):
    """The arguments that make the script a generator rather than an installer."""
    args = [
        f"--mariadb-server-version={version}",
        f"--os-type={os_type}",
        f"--os-version={os_version}",
        f"--arch={arch}",
        # We are generating for some other host, so this one's package tooling
        # is irrelevant — on a Fedora laptop the check would fail outright.
        '--skip-check-installed',
        '--skip-key-import',
    ]
    if edition == 'CS':
        args.append('--write-to-stdout')
    else:
        # No --apply: for the Enterprise script that is what gates both writing
        # the repo files and importing keys.
        args.insert(0, f"--token={token}")
    return args


def generate(edition, version, os_type, os_version, arch, token=None,
             refresh_script=False):
    """Runs the official script and returns the repository file's contents.

    The Enterprise token goes in argv, which `ps` can read for as long as the
    script runs. That is unavoidable: verify_token() in the official script
    checks it against DLM on every run and is not behind a skip flag, so a
    placeholder cannot be used to generate. It is also exactly what MariaDB's
    own documented one-liner does. The token is kept out of everything we
    control: the saved file (placeholder unless asked otherwise), the error
    text, and this module's logging.
    """
    script = script_path(edition, refresh=refresh_script)
    args = build_args(edition, version, os_type, os_version, arch, token)
    try:
        completed = subprocess.run(
            ['bash', str(script), *args],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise click.ClickException(
            f"{SCRIPT_NAMES[edition]} did not finish within 120s."
        ) from None

    if completed.returncode != 0:
        detail = _strip_secrets(completed.stderr.strip(), token)
        raise click.ClickException(
            f"{SCRIPT_NAMES[edition]} refused {version} on "
            f"{os_type} {os_version} ({arch}):\n{detail}"
        )
    content = completed.stdout
    if not content.strip():
        raise click.ClickException(
            f"{SCRIPT_NAMES[edition]} produced no repository content for "
            f"{version} on {os_type} {os_version}. The script may have changed; "
            f"try --refresh-script."
        )
    return content, _strip_secrets(completed.stderr.strip(), token)


def redact_token(content, token):
    """Swaps the real token for the placeholder, so the file is shareable."""
    if not token:
        return content
    return content.replace(token, TOKEN_PLACEHOLDER)


def contains_token(content, token):
    return bool(token) and token in content


def os_version_label(os_type, os_version):
    for candidate_type, candidate_version, label in TARGETS:
        if candidate_type == os_type and candidate_version == os_version:
            return label
    return f"{os_type} {os_version}"
