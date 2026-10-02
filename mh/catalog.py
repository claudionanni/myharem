"""What MariaDB publishes, and where.

Everything myharem knows about the two upstream download sources lives here:
the MariaDB Foundation REST API for Community, and MariaDB Corp's DLM for
Enterprise. Deliberately one file, separate from `deployment.py`:

- its subject is a remote vendor, not this host, and its failure mode is vendor
  drift (a renamed JSON key, a changed HTML template) rather than a broken
  local install;
- it needs no filesystem, so its tests are frozen response strings with no
  basedir fixture;
- when Corp changes the browse page, there is exactly one file to fix, and
  `git log mh/catalog.py` reads as the history of upstream breakage.

SECURITY: an Enterprise URL embeds the customer token as a PATH SEGMENT, so the
URL is a secret. Nothing here puts one in a message, and the download is done
with urllib in-process — never shell out to curl/wget, which would publish the
token in the process table to every user on the host.
"""

import json
import platform
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

import click

from . import report

FOUNDATION_API = 'https://downloads.mariadb.org/rest-api/mariadb'
DLM_BASE = 'https://dlm.mariadb.com'
# Where a customer or engineer finds their own download token. Printed, never
# opened: mh runs as root, usually over SSH on a headless repro host, where
# launching a browser would at best fail and at worst run one as root — and the
# page is behind SSO anyway, so it cannot hand the token over unattended.
ES_TOKEN_PAGE = 'https://customers.mariadb.com/downloads/token/'
ES_PRODUCT = 'mariadb_enterprise_server'


def _fetch_url(url, timeout=30):
    """Returns the decoded body of `url`.

    The single network seam for this module, shaped like deployment._download so
    it monkeypatches the same way. Deliberately NOT the same function: this
    reads a small body into memory (a catalog page is a few hundred KB at most),
    while _download streams a 600MB tarball to disk. Merging them would force
    every catalog test to deal with files.
    """
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode('utf-8', errors='replace')


def _get_json(url, what):
    """Fetches and parses JSON, turning both failure modes into clear errors."""
    try:
        body = _fetch_url(url)
    except Exception as exc:
        raise click.ClickException(
            f"Could not fetch {what}: {report.redact(str(exc))}\n"
            f"Check network/proxy access to downloads.mariadb.org, or download a "
            f"tarball by hand into the local/ directory (mh deploy finds it there)."
        ) from None
    try:
        return json.loads(body)
    except ValueError:
        raise click.ClickException(
            f"Unexpected (non-JSON) response while fetching {what} — the "
            f"download API may have changed; myharem may need an update."
        ) from None


# ---------- version ordering ----------

def _version_key(value):
    """Sort key: numeric per component, over both '.' and '-' separators.

    Enterprise releases carry a build suffix ('11.4.12-9'), and a string sort
    puts 11.4.9-6 above 11.4.12-9. Non-numeric components sort as -1 rather
    than raising, so an unexpected label can never crash a listing.
    """
    parts = []
    for component in re.split(r'[.-]', value):
        parts.append(int(component) if component.isdigit() else -1)
    return parts


def sort_releases_desc(releases):
    return sorted(releases, key=_version_key, reverse=True)


def series_of(release):
    """'11.4.12-9' -> '11.4'."""
    components = release.split('.')
    return '.'.join(components[:2])


# ---------- the model ----------

@dataclass
class Series:
    id: str
    status: str
    support: str | None = None
    eol: str | None = None

    @property
    def is_stable(self):
        return self.status.lower() == 'stable'

    def label(self):
        bits = [self.status]
        if self.support:
            bits.append(self.support)
        if self.eol:
            bits.append(f"EOL {self.eol}")
        return ', '.join(b for b in bits if b)


@dataclass
class Artifact:
    edition: str
    version: str
    filename: str
    # SECRET for Enterprise: the DLM token is a path segment of this URL. Never
    # log it, never put it in an exception, never emit it in --json output.
    url: str
    sha256: str | None = None


def arch():
    """The DLM/Foundation spelling of this machine's architecture."""
    machine = platform.machine()
    return 'x86_64' if machine in ('x86_64', 'amd64') else machine


# ---------- Community ----------

def list_cs_series():
    """Stable series first, then RC/Preview — each in the API's own order.

    The API currently returns Preview and RC at the top; presenting one as the
    first menu entry is how somebody deploys a preview build by accident.
    """
    body = _get_json(f"{FOUNDATION_API}/", 'the MariaDB release list')
    series = []
    for entry in body.get('major_releases') or []:
        release_id = entry.get('release_id')
        if not release_id:
            continue
        series.append(Series(
            id=str(release_id),
            status=entry.get('release_status') or 'Unknown',
            support=entry.get('release_support_type'),
            eol=entry.get('release_eol_date'),
        ))
    return [s for s in series if s.is_stable] + [s for s in series if not s.is_stable]


def list_cs_releases(series):
    """Every point release in a series, newest first.

    Note the top-level key here is `releases`, while a CONCRETE version (below)
    answers under `release_data`. That is the API's own inconsistency and it
    reads like a bug until you notice the path differs.
    """
    body = _get_json(f"{FOUNDATION_API}/{series}/", f"the {series} releases")
    releases = body.get('releases')
    if not isinstance(releases, dict) or not releases:
        raise click.ClickException(
            f"No releases listed for MariaDB {series}. Check the series exists "
            f"(mh download --list --edition CS)."
        )
    return sort_releases_desc(list(releases.keys()))


def _pick_cs_file(files, want_arch=None):
    """The one Linux binary tarball among everything a release publishes.

    Selected on the API's own fields rather than by guessing at filenames:
    os/cpu/package_type identify it exactly, which also excludes the source
    tarball (os 'Source'), both Windows builds, and the directory entries
    ('yum/', 'repo/') whose fields are all null.

    Where more than one survives, the 'linux-systemd' flavour wins: it is the
    one that bundles libgalera_smm.so (see the table in README and
    galera._find_galera_lib). Today the Foundation publishes only that flavour,
    so this is insurance rather than a live branch — but it is the first time
    that knowledge exists as code instead of prose.
    """
    want_arch = want_arch or arch()
    candidates = [
        f for f in files or []
        if (f.get('os') == 'Linux'
            and f.get('cpu') == want_arch
            and f.get('package_type') == 'gzipped tar file'
            and (f.get('file_name') or '').endswith('.tar.gz'))
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda f: 0 if f'linux-systemd-{want_arch}' in f['file_name'] else 1
    )
    return candidates[0]


def resolve_cs_artifact(version, want_arch=None):
    """The downloadable tarball for a concrete Community version."""
    body = _get_json(f"{FOUNDATION_API}/{version}/", f"MariaDB {version}")
    release_data = body.get('release_data')
    if not isinstance(release_data, dict):
        raise click.ClickException(
            f"Unexpected response for MariaDB {version} — the download API may "
            f"have changed; myharem may need an update."
        )
    entry = release_data.get(version) or {}
    chosen = _pick_cs_file(entry.get('files'), want_arch)
    if not chosen:
        raise click.ClickException(
            f"MariaDB {version} publishes no Linux {want_arch or arch()} binary "
            f"tarball. Pick another release (mh download --list --edition CS "
            f"--version {series_of(version)})."
        )
    url = chosen.get('file_download_url') or ''
    # The API advertises http://; the https form works and the download is a
    # 302 to a third-party mirror either way, which is also why the published
    # sha256 is verified rather than trusted.
    if url.startswith('http://'):
        url = 'https://' + url[len('http://'):]
    checksum = (chosen.get('checksum') or {}).get('sha256sum')
    return Artifact(
        edition='CS',
        version=version,
        filename=chosen['file_name'],
        url=url,
        sha256=checksum or None,
    )


def resolve_cs_version(version):
    """'11.4' -> the newest release in that series; '11.4.13' -> itself."""
    if len(version.split('.')) >= 3:
        return version
    return list_cs_releases(version)[0]


# ---------- Enterprise ----------

def list_es_series():
    """The Enterprise series, from the endpoint that needs NO token.

    Means the series menu — and a useful refusal message — can be shown before
    any secret is involved. The response is plain text, not JSON, despite the
    /rest/ in the path.
    """
    url = f"{DLM_BASE}/rest/releases/{ES_PRODUCT}/"
    try:
        body = _fetch_url(url)
    except Exception as exc:
        raise click.ClickException(
            f"Could not fetch the MariaDB Enterprise release list: "
            f"{report.redact(str(exc))}"
        ) from None
    releases = sort_releases_desc(body.split())
    seen = []
    for release in releases:
        series = series_of(release)
        if series not in seen:
            seen.append(series)
    return seen


def _browse(url, what):
    """Fetches a token-bearing DLM listing. The URL never reaches a message."""
    try:
        return _fetch_url(url)
    except Exception as exc:
        raise click.ClickException(
            f"DLM request failed for {what}: {report.redact(str(exc))}\n"
            f"Check the Enterprise token (MYHAREM_ES_TOKEN) and that this build "
            f"exists."
        ) from None


def list_es_releases(series, token):
    """Every Enterprise build in a series, newest first.

    Scraped, because DLM publishes no JSON for this. The regex is anchored on
    the product path segment so a version mentioned anywhere else on the page
    cannot be mistaken for a release directory.
    """
    html = _browse(
        f"{DLM_BASE}/browse/{urllib.parse.quote(token)}/{ES_PRODUCT}/{series}/",
        f"the Enterprise {series} listing",
    )
    pattern = re.compile(rf"/{ES_PRODUCT}/(\d+\.\d+\.\d+-\d+)/")
    found = {match.group(1) for match in pattern.finditer(html)}
    if not found:
        raise click.ClickException(
            f"No Enterprise releases found in the {series} listing. Either the "
            f"series does not exist, the token is not entitled to it, or the DLM "
            f"listing format has changed (myharem may need an update)."
        )
    return sort_releases_desc(found)


def resolve_es_version(version, token):
    """'11.4' -> newest build; '11.4.12' -> its newest build; '11.4.12-9' -> itself."""
    if '-' in version:
        return version
    series = series_of(version)
    releases = list_es_releases(series, token)
    if len(version.split('.')) >= 3:
        for release in releases:
            if release.startswith(f"{version}-"):
                return release
        raise click.ClickException(
            f"No Enterprise build of {version} in the {series} listing. "
            f"Available: {', '.join(releases[:8])}"
        )
    return releases[0]


def es_filename(release, target):
    return f"mariadb-enterprise-{release}-{target}.tar.gz"


def _find_download_href(html, filename):
    """The real download URL for `filename` in a DLM listing.

    Matched on the known filename rather than on page structure, so a cosmetic
    template change survives. The href cannot be constructed: it embeds an
    opaque per-build id.
    """
    for match in re.finditer(r'href="([^"]+)"', html):
        href = match.group(1)
        if href.startswith('https://') and href.endswith('/' + filename):
            return href
    return None


def resolve_es_artifact(release, target, token):
    """The downloadable bintar for a concrete Enterprise release and distro."""
    filename = es_filename(release, target)
    html = _browse(
        f"{DLM_BASE}/browse/{urllib.parse.quote(token)}/{ES_PRODUCT}/{release}"
        f"/bintar/bintar-{target}/",
        f"Enterprise {release} ({target})",
    )
    href = _find_download_href(html, filename)
    if not href:
        raise click.ClickException(
            f"No '{filename}' in the Enterprise listing for {release} ({target}).\n"
            f"Check that this build exists for this distro, that the token is "
            f"entitled to it, or pass a different --target. If the build is "
            f"definitely there, the DLM listing format may have changed."
        )
    # sha256 is None: Enterprise listings publish no checksum.
    return Artifact(edition='ES', version=release, filename=filename, url=href)


# ---------- which Enterprise bintar this host needs ----------

_RHEL_FAMILY = {'rhel', 'centos', 'rocky', 'almalinux', 'ol', 'oraclelinux'}

KNOWN_TARGETS = (
    'rhel-8-x86_64, rhel-9-x86_64, rhel-10-x86_64, '
    'ubuntu-2004-x86_64, ubuntu-2204-x86_64, ubuntu-2404-x86_64, '
    'debian-11-x86_64, debian-12-x86_64, sles-15-x86_64 '
    '(and the matching -aarch64 variants)'
)


def _parse_os_release(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def dlm_target_from_os_release(text, machine=None):
    """Maps /etc/os-release contents to a DLM bintar target, or None.

    Enterprise ships one tarball per distro and it must match THIS host — a
    mismatch is not a clean failure, it is a missing libssl at server start.
    myharem runs on the host it unpacks onto, so unlike a control plane it can
    detect this rather than be told.

    Returns None rather than guessing for anything unrecognised; the caller
    refuses and names --target. The one exception is ID_LIKE, where a rhel-9
    guess is returned and the caller says out loud that it guessed.
    """
    machine = machine or arch()
    values = _parse_os_release(text)
    distro = (values.get('ID') or '').lower()
    version = values.get('VERSION_ID') or ''
    major = version.split('.')[0]

    if distro in _RHEL_FAMILY and major.isdigit():
        return f"rhel-{major}-{machine}"
    if distro == 'ubuntu' and '.' in version:
        return f"ubuntu-{version.replace('.', '')}-{machine}"
    if distro == 'debian' and major.isdigit():
        return f"debian-{major}-{machine}"
    if distro in ('sles', 'sled', 'opensuse-leap') and major.isdigit():
        return f"sles-{major}-{machine}"

    id_like = (values.get('ID_LIKE') or '').lower()
    if any(family in id_like for family in ('rhel', 'fedora', 'centos')):
        report.warn(
            f"Mapped {distro or 'this host'} {version} to rhel-9-{machine} via "
            f"ID_LIKE — pass --target if that is wrong."
        )
        return f"rhel-9-{machine}"
    return None


def detect_dlm_target(path='/etc/os-release'):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            text = handle.read()
    except OSError:
        return None
    return dlm_target_from_os_release(text)


def resolve_es_target(explicit=None, configured=None, os_release='/etc/os-release'):
    """--target, then env/config, then detection, then a refusal that helps."""
    if explicit:
        return explicit, 'from --target'
    if configured:
        return configured, 'from config'
    detected = detect_dlm_target(os_release)
    if detected:
        return detected, 'detected'

    try:
        with open(os_release, 'r', encoding='utf-8') as handle:
            values = _parse_os_release(handle.read())
        seen = f"ID={values.get('ID')} VERSION_ID={values.get('VERSION_ID')}"
    except OSError:
        seen = f"{os_release} not readable"
    raise click.ClickException(
        f"Could not work out which MariaDB Enterprise bintar this host needs "
        f"({seen}).\n"
        f"Enterprise ships one tarball per distro and it must match the host. "
        f"Pick one explicitly:\n"
        f"  mh download --edition ES --version 11.4 --target rhel-9-{arch()}\n"
        f"Known targets: {KNOWN_TARGETS}\n"
        f"(or set es_bintar_target in myharem.conf / MYHAREM_ES_BINTAR_TARGET)"
    )
