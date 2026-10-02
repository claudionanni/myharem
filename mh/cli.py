import json
import os
import sys

import click

from . import __version__
from . import catalog
from . import config
from . import deployment
from . import galera
from . import manifest
from . import report
from . import service
from .instance import Instance


@click.group()
@click.version_option(version=__version__, prog_name='myharem')
@click.option('--json', 'json_out', is_flag=True,
              help='Emit machine-readable JSON results on stdout.')
@click.pass_context
def main(ctx, json_out):
    """MyHarem: A tool for managing local MariaDB instances."""
    ctx.ensure_object(dict)
    ctx.obj['json'] = json_out
    report.set_json_mode(json_out)
    config.setup_myharem_dirs()


def _emit_deploy(ctx, result):
    """Writes a deploy result to stdout: JSON when --json, else a summary."""
    if ctx.obj.get('json'):
        click.echo(json.dumps(result.to_dict()))
        return
    click.echo(f"{result.topology} deployment '{result.cluster_id}' ready:")
    for node in result.nodes:
        click.echo(
            f"  {node.id:<8} {node.role:<8} port={node.port} "
            f"socket={node.socket}"
        )


def _emit_action(ctx, payload, human):
    if ctx.obj.get('json'):
        click.echo(json.dumps(payload))
    else:
        click.echo(human)


# ---------- deploy ----------

@main.command()
@click.argument('tarball', required=False)
@click.argument('instance_id', required=False)
@click.pass_context
def deploy(ctx, tarball, instance_id):
    """Deploys a single MariaDB instance (interactive if no args given)."""
    if tarball and instance_id:
        result = deployment.deploy_single(tarball, instance_id)
        _emit_deploy(ctx, result)
        return
    _deploy_wizard(ctx)


@main.command('fetch-tarball')
@click.argument('url')
@click.option('--name', 'filename', default=None,
              help="Filename to save as under <basedir>/local/ "
                   "(defaults to the URL's own basename — required when the "
                   "URL doesn't end in the real filename, e.g. a presigned URL).")
@click.pass_context
def fetch_tarball_command(ctx, url, filename):
    """Downloads a tarball into <basedir>/local/ (skipped if already staged)."""
    dest = deployment.fetch_tarball(url, filename=filename)
    _emit_action(ctx, {'path': str(dest)}, f"Tarball staged at {dest}")


# ---------- token ----------

def _can_open_a_browser():
    """(may_open, why_not) — whether launching a browser here is a good idea.

    Deliberately conservative. `mh` normally runs as root over SSH on a headless
    repro host, where webbrowser.open() either fails silently (making the
    feature a lie) or, if a text browser is installed, launches one AS ROOT
    inside the user's terminal — a surprising thing to have to escape from.
    """
    if os.environ.get('SUDO_USER'):
        return False, "running under sudo (a browser would open as root)"
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        return False, "running as root"
    if sys.platform == 'darwin':
        return True, None
    if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
        return True, None
    return False, "no graphical session (no DISPLAY)"


@main.command()
@click.option('--no-open', 'open_browser', flag_value=False, default=True,
              help="Only print the address; never launch a browser.")
@click.pass_context
def token(ctx, open_browser):
    """Where to get the MariaDB Enterprise download token, and whether one is set.

    Opens the page in a browser when this is a desktop session; otherwise just
    prints the address. The token itself is never printed.
    """
    configured = config.get_es_token()
    source = None
    if configured:
        source = 'config' if config.es_token_came_from_config() else 'env'
        config.warn_if_config_is_world_readable()

    may_open, why_not = _can_open_a_browser()
    opened = False
    if open_browser and may_open and not ctx.obj.get('json'):
        import webbrowser
        try:
            opened = webbrowser.open(catalog.ES_TOKEN_PAGE)
        except Exception:
            opened = False

    lines = [f"Enterprise download token page: {catalog.ES_TOKEN_PAGE}"]
    if configured:
        where = ("MYHAREM_ES_TOKEN" if source == 'env'
                 else "es_token in the myharem config")
        # Deliberately never the value, not even a prefix: it is a working
        # credential and this output lands in terminals and tickets.
        lines.append(f"A token is already configured ({where}).")
    else:
        lines.append("No token is configured yet. Sign in with your MariaDB ID,")
        lines.append("copy the token, then either:")
        lines.append("  export MYHAREM_ES_TOKEN=...      # and run mh with sudo -E")
        lines.append("  or add `es_token=...` to the myharem config (chmod 600 it)")
    if opened:
        lines.append("Opened it in your browser.")
    elif open_browser and not may_open:
        lines.append(f"Not opening a browser: {why_not}.")

    _emit_action(
        ctx,
        # No token value, by construction.
        {'token_page': catalog.ES_TOKEN_PAGE,
         'configured': bool(configured),
         'source': source,
         'opened': opened},
        "\n".join(lines),
    )


# ---------- download ----------

# How many releases the wizard lists before offering 'show all'.
_RELEASES_SHOWN = 10


def _stdin_is_tty():
    """Indirection so the interactive tests can pretend to be a terminal.

    CliRunner's stdin always reports isatty() False, which is also exactly what
    a piped or cron invocation looks like — so the guard below is right for both
    and the wizard test patches this one symbol.
    """
    return sys.stdin.isatty()


def _es_token_or_refuse():
    """The Enterprise token, or a refusal that is actually useful.

    Checked before any network call, and the refusal still lists what exists:
    the Enterprise release list needs no token, so there is no reason to answer
    a question about versions with nothing but 'set a variable'.
    """
    token = config.get_es_token()
    if token:
        config.warn_if_config_is_world_readable()
        return token

    lines = [
        "MariaDB Enterprise downloads need a customer token.",
        f"Get yours at {catalog.ES_TOKEN_PAGE} (MariaDB ID login) — "
        f"`mh token` opens it.",
        "Then set MYHAREM_ES_TOKEN, or add `es_token=...` to the myharem config",
        "(chmod 600 it — the token is a secret).",
    ]
    if os.environ.get('SUDO_USER'):
        # The likeliest reason the variable "is set" and we still cannot see it:
        # sudo resets the environment, so `export ...; sudo mh download` loses
        # it and fails exactly as if it had never been set.
        lines.append(
            "Running under sudo, which resets the environment — if you exported "
            "MYHAREM_ES_TOKEN, re-run with 'sudo -E', or put es_token in the "
            "config file."
        )
    try:
        series = catalog.list_es_series()
        lines.append(f"Enterprise series currently published: {', '.join(series)}")
    except click.ClickException:
        pass
    lines.append("Community downloads need no token:")
    lines.append("  mh download --edition CS --version 11.4")
    raise click.ClickException("\n".join(lines))


def _prompt_for_target(explicit):
    """Asks which distro the tarball is FOR.

    Detection is a default here, never a gate: `mh download` is routinely run
    somewhere other than the machine that will run the nodes — a laptop staging
    a tarball for a repro host, say — so refusing because THIS host is not a
    distro DLM publishes for would be answering the wrong question. The
    non-interactive path still refuses, because there is nobody to ask.
    """
    if explicit:
        return explicit

    configured = config.get_es_bintar_target()
    detected = catalog.detect_dlm_target()
    suggested = configured or detected
    note = ' (configured)' if configured else ' (this host)'

    click.echo("\nEnterprise publishes one tarball per distribution — pick the one")
    click.echo("the nodes will run, which need not be this machine:")
    default_index = None
    for i, distro in enumerate(catalog.ES_TARGET_DISTROS, 1):
        marker = ''
        if suggested and suggested.startswith(distro + '-'):
            marker = note
            default_index = i
        click.echo(f"  [{i}] {distro}{marker}")
    choice = click.prompt(
        "\nSelect distribution",
        type=click.IntRange(1, len(catalog.ES_TARGET_DISTROS)),
        default=default_index,
        show_default=default_index is not None,
    )
    distro = catalog.ES_TARGET_DISTROS[choice - 1]

    local = catalog.arch()
    click.echo("\nArchitecture:")
    for i, name in enumerate(catalog.ES_ARCHES, 1):
        click.echo(f"  [{i}] {name}" + ("  (this host)" if name == local else ""))
    arch_choice = click.prompt(
        "\nSelect architecture",
        type=click.IntRange(1, len(catalog.ES_ARCHES)),
        default=(catalog.ES_ARCHES.index(local) + 1) if local in catalog.ES_ARCHES else 1,
    )
    target = f"{distro}-{catalog.ES_ARCHES[arch_choice - 1]}"
    click.echo(f"  → {target}")
    return target


def _resolve_download(edition, version, target, want_arch=None,
                      target_note='from --target'):
    """(artifact, summary rows) for a fully specified request."""
    if edition == 'CS':
        release = catalog.resolve_cs_version(version)
        artifact = catalog.resolve_cs_artifact(release, want_arch)
        rows = [
            ("Edition", "Community Server"),
            ("Version", release),
            ("File", artifact.filename),
            ("Verify", "sha256 (published)" if artifact.sha256 else "none published"),
        ]
        return artifact, rows

    token = _es_token_or_refuse()
    resolved_target, how = catalog.resolve_es_target(
        explicit=target, configured=config.get_es_bintar_target(),
        explicit_note=target_note,
    )
    release = catalog.resolve_es_version(version, token)
    artifact = catalog.resolve_es_artifact(release, resolved_target, token)
    rows = [
        ("Edition", "Enterprise Server"),
        ("Version", release),
        ("Target", f"{resolved_target} ({how})"),
        ("File", artifact.filename),
        ("Verify", "none (Enterprise publishes no checksum)"),
    ]
    return artifact, rows


def _stage(ctx, artifact, verify):
    """Downloads an artifact, keeping a secret URL out of every message."""
    dest = config.get_basedir() / 'local' / artifact.filename
    already = dest.exists()
    if artifact.edition == 'ES' and verify:
        report.warn("Enterprise listings publish no checksum — skipping verification.")
    path = deployment.stage_tarball(
        artifact.url,
        artifact.filename,
        # The label, not the URL: an Enterprise URL carries the customer token.
        label=artifact.filename,
        sha256=artifact.sha256,
        verify=verify,
        timeout=1800,
    )
    _emit_action(
        ctx,
        # Deliberately no 'url' key, for either edition — so no future Enterprise
        # branch can inherit one by forgetting to remove it.
        {
            'path': str(path),
            'filename': artifact.filename,
            'edition': artifact.edition,
            'version': artifact.version,
            'verified': bool(artifact.sha256 and verify),
            'already_staged': already,
        },
        f"Tarball staged at {path}",
    )


@main.command()
@click.option('--edition', '-e', 'edition', default=None,
              help="CS (Community) or ES (Enterprise). Omit for the wizard.")
@click.option('--version', '-v', 'version', default=None,
              help="A series (11.4 — newest release in it) or an exact version "
                   "(11.4.13, or an Enterprise build 11.4.13-10). Omit for the "
                   "wizard. Note: `mh --version` prints myharem's own version.")
@click.option('--target', default=None,
              help="Enterprise bintar target, e.g. rhel-9-x86_64. Defaults to "
                   "the one detected from /etc/os-release; the wizard asks.")
@click.option('--arch', 'want_arch', default=None,
              help="Architecture for a Community tarball (default: this "
                   "machine's). For Enterprise the architecture is part of "
                   "--target.")
@click.option('--list', 'list_only', is_flag=True,
              help="List what is published and exit.")
@click.option('--no-verify', 'verify', flag_value=False, default=True,
              help="Skip the published sha256 check (Community).")
@click.pass_context
def download(ctx, edition, version, target, want_arch, list_only, verify):
    """Downloads a MariaDB tarball into <basedir>/local/ (interactive if no args).

    Community comes from downloads.mariadb.org and needs no credentials.
    Enterprise comes from dlm.mariadb.com and needs a customer token in
    MYHAREM_ES_TOKEN or `es_token` in the config file.
    """
    edition = _normalise_edition(edition)
    if target and edition == 'CS':
        raise click.UsageError(
            "--target applies to Enterprise only: a Community tarball is built "
            "per CPU architecture, not per distribution."
        )

    if list_only:
        _list_catalog(ctx, edition, version)
        return

    if not edition or not version:
        if ctx.obj.get('json') or not _stdin_is_tty():
            raise click.UsageError(
                "mh download needs --edition and --version when there is no "
                "terminal to prompt on (piped stdin, cron, or --json).\n"
                "Example: mh download --edition CS --version 11.4.13"
            )
        _download_wizard(ctx, edition, target, want_arch, verify)
        return

    artifact, rows = _resolve_download(edition, version, target, want_arch)
    _stage(ctx, artifact, verify)


def _normalise_edition(value):
    if value is None:
        return None
    normalised = value.strip().upper()
    aliases = {'COMMUNITY': 'CS', 'CS': 'CS', 'ENTERPRISE': 'ES', 'ES': 'ES'}
    if normalised not in aliases:
        raise click.UsageError(
            f"Unknown edition '{value}'. Use CS (Community) or ES (Enterprise)."
        )
    return aliases[normalised]


def _list_catalog(ctx, edition, version):
    """--list: what is published, for a human or for a pipeline."""
    if not edition:
        raise click.UsageError("--list needs --edition CS or --edition ES.")

    if edition == 'CS':
        if version:
            releases = catalog.list_cs_releases(version)
            payload = {'edition': 'CS', 'series': version, 'releases': releases}
            human = "\n".join(f"  {r}" for r in releases)
        else:
            series = catalog.list_cs_series()
            payload = {'edition': 'CS',
                       'series': [{'id': s.id, 'status': s.status} for s in series]}
            human = "\n".join(f"  {s.id:<8} {s.label()}" for s in series)
            human += ("\n\nAdd --version <series> to list the releases in one, "
                      "e.g. --list --edition CS --version 10.6")
    else:
        if version:
            token = _es_token_or_refuse()
            releases = catalog.list_es_releases(version, token)
            payload = {'edition': 'ES', 'series': version, 'releases': releases}
            human = "\n".join(f"  {r}" for r in releases)
        else:
            series = catalog.list_es_series()
            payload = {'edition': 'ES', 'series': series}
            human = "\n".join(f"  {s}" for s in series)
            human += ("\n\nAdd --version <series> to list the builds in one, "
                      "e.g. --list --edition ES --version 11.4")
    _emit_action(ctx, payload, human)


def _download_wizard(ctx, edition, target, want_arch, verify):
    """Interactive download: pick edition, series, release, confirm."""
    if not edition:
        click.echo("\nEdition:")
        click.echo("  [1] Community Server  (downloads.mariadb.org)")
        click.echo("  [2] Enterprise Server (dlm.mariadb.com, token required)")
        choice = click.prompt("\nSelect edition", type=click.IntRange(1, 2),
                              default=1)
        edition = 'CS' if choice == 1 else 'ES'
    click.echo(f"  → {'Community Server' if edition == 'CS' else 'Enterprise Server'}")

    if edition == 'ES':
        # Before any listing, so a missing token is one clear message rather
        # than a menu the user cannot act on.
        _es_token_or_refuse()

    if edition == 'CS':
        report.log("Fetching the MariaDB release list ...")
        series_list = catalog.list_cs_series()
        click.echo("\nSeries:")
        for i, s in enumerate(series_list, 1):
            click.echo(f"  [{i}] {s.id:<8} {s.label()}")
        choice = click.prompt("\nSelect series", type=click.IntRange(1, len(series_list)))
        series = series_list[choice - 1].id
    else:
        report.log("Fetching the MariaDB Enterprise release list ...")
        series_list = catalog.list_es_series()
        click.echo("\nSeries:")
        for i, sid in enumerate(series_list, 1):
            click.echo(f"  [{i}] {sid}")
        choice = click.prompt("\nSelect series", type=click.IntRange(1, len(series_list)))
        series = series_list[choice - 1]
    click.echo(f"  → {series}")

    report.log(f"Fetching the {series} releases ...")
    if edition == 'CS':
        releases = catalog.list_cs_releases(series)
    else:
        releases = catalog.list_es_releases(series, config.get_es_token())

    # Newest first, truncated — but never truncated with no way out: a long
    # series has far more than fits on a screen (10.6 has 28), and the whole
    # point of reproducing a customer's problem is often an OLD release.
    shown = releases[:_RELEASES_SHOWN]
    while True:
        click.echo(f"\nReleases in {series}:")
        for i, release in enumerate(shown, 1):
            click.echo(f"  [{i}] {release}")
        truncated = len(shown) < len(releases)
        if truncated:
            click.echo(f"  [0] show all {len(releases)}")
        choice = click.prompt(
            "\nSelect release",
            type=click.IntRange(0 if truncated else 1, len(shown)),
            default=1,
        )
        if choice == 0:
            shown = releases
            continue
        break
    release = shown[choice - 1]
    click.echo(f"  → {release}")

    note = 'from --target'
    if edition == 'ES':
        if not target:
            note = 'chosen'
        target = _prompt_for_target(target)
    artifact, rows = _resolve_download(edition, release, target, want_arch, note)

    dest = config.get_basedir() / 'local' / artifact.filename
    click.echo("")
    for key, value in rows:
        click.echo(f"  {key + ':':<9} {value}")
    if dest.exists():
        click.echo(f"  {'Dest:':<9} {dest} (already staged — nothing to download)")
    else:
        click.echo(f"  {'Dest:':<9} {dest}")

    if not click.confirm("\nProceed?", default=True):
        click.echo("Aborted.")
        return
    _stage(ctx, artifact, verify)


def _deploy_wizard(ctx):
    """Interactive deployment wizard: pick tarball, type, and instance ID."""
    from . import replication

    local_dir = config.get_basedir() / 'local'
    if not local_dir.exists():
        raise click.ClickException(
            f"No local tarball directory found at {local_dir}"
        )
    tarballs = sorted(local_dir.glob('*.tar.gz'))
    if not tarballs:
        raise click.ClickException("No tarballs found in local/")

    click.echo("\nAvailable tarballs:")
    for i, t in enumerate(tarballs, 1):
        click.echo(f"  [{i}] {t.name}")

    choice = click.prompt("\nSelect tarball", type=click.IntRange(1, len(tarballs)))
    tarball = tarballs[choice - 1]
    click.echo(f"  → {tarball.name}")

    deploy_types = ['single', 'replica', 'galera']
    click.echo("\nDeployment type:")
    click.echo("  [1] Single instance")
    click.echo("  [2] Async replication (master + slaves)")
    click.echo("  [3] Galera cluster")
    dtype_choice = click.prompt(
        "\nSelect type", type=click.IntRange(1, len(deploy_types))
    )
    deploy_type = deploy_types[dtype_choice - 1]

    instance_id = click.prompt("\nInstance ID (base port)", type=int)

    count = 1
    if deploy_type == 'galera':
        count = click.prompt("Number of Galera nodes", type=int, default=3)
    elif deploy_type == 'replica':
        count = click.prompt("Number of slaves", type=int, default=1)

    existing = {inst.id for inst in Instance.get_all_instances()}
    if deploy_type == 'single':
        needed_ids = [instance_id]
    elif deploy_type == 'replica':
        needed_ids = [instance_id] + [instance_id + replication.REPL_STEP * i
                                      for i in range(1, count + 1)]
    else:  # galera
        needed_ids = [instance_id + galera.INST_STEP * i for i in range(count)]

    conflicts = [str(nid) for nid in needed_ids if str(nid) in existing]
    if conflicts:
        raise click.ClickException(
            f"Instance ID(s) already exist: {', '.join(conflicts)}"
        )

    click.echo(f"\n  Tarball: {tarball.name}")
    click.echo(f"  Type:    {deploy_type}")
    click.echo(f"  IDs:     {', '.join(str(i) for i in needed_ids)}")
    if not click.confirm("\nProceed?", default=True):
        click.echo("Aborted.")
        return

    if deploy_type == 'single':
        result = deployment.deploy_single(str(tarball), str(instance_id))
    elif deploy_type == 'replica':
        result = replication.deploy_replication(
            str(tarball), str(instance_id), slaves=count
        )
    else:
        result = galera.deploy_cluster(
            str(tarball), str(instance_id), nodes=count,
            wsrep_provider=config.get_wsrep_provider(),
        )
    _emit_deploy(ctx, result)


# ---------- deploygalera ----------

@main.command()
@click.argument('tarball')
@click.argument('first_instance_id')
@click.option('--nodes', default=3, type=int, show_default=True,
              help='Number of Galera nodes.')
@click.option('--wsrep-provider', 'wsrep_provider', default=None,
              metavar='PATH',
              help='Path to libgalera_smm.so, for tarballs that do not bundle '
                   'Galera (e.g. generic linux-x86_64 builds). Overrides '
                   'wsrep_provider / MYHAREM_WSREP_PROVIDER.')
@click.option('--advertise', 'advertise', default='127.0.0.1', metavar='IP',
              show_default=True,
              help='Address peers use to reach these nodes (single host). For a '
                   'cluster spanning hosts, use `mh galera-node` per host.')
@click.pass_context
def deploygalera(ctx, tarball, first_instance_id, nodes, wsrep_provider, advertise):
    """Deploys an N-node Galera cluster (default 3)."""
    provider = wsrep_provider or config.get_wsrep_provider()
    result = galera.deploy_cluster(tarball, first_instance_id, nodes=nodes,
                                   wsrep_provider=provider, advertise=advertise)
    _emit_deploy(ctx, result)


# ---------- deployreplication ----------

@main.command()
@click.argument('tarball')
@click.argument('instance_id')
@click.option('--slaves', default=1, type=int, show_default=True,
              help='Number of slaves.')
@click.pass_context
def deployreplication(ctx, tarball, instance_id, slaves):
    """Deploys a master + N async (GTID) slaves."""
    from . import replication
    result = replication.deploy_replication(tarball, instance_id, slaves=slaves)
    _emit_deploy(ctx, result)


# ---------- distributed (multi-host) primitives ----------

@main.command('galera-node')
@click.argument('tarball')
@click.argument('node_id')
@click.option('--members', 'members', required=True,
              help='Comma-separated gcomm seed list host:wsrep_port,... (all peers).')
@click.option('--cluster-name', 'cluster_name', required=True,
              help='Shared cluster name — identical on every host.')
@click.option('--advertise', 'advertise', default=None, metavar='IP',
              help="This host's reachable IP (default: config / "
                   'MYHAREM_ADVERTISE_ADDRESS).')
@click.option('--bootstrap', is_flag=True, default=False,
              help="Marker only; start with 'mh service start --bootstrap <id>'.")
@click.option('--wsrep-provider', 'wsrep_provider', default=None, metavar='PATH',
              help="Path to libgalera_smm.so if the tarball doesn't bundle it.")
@click.pass_context
def galera_node(ctx, tarball, node_id, members, cluster_name, advertise,
                bootstrap, wsrep_provider):
    """Deploys ONE local Galera node into a multi-host cluster."""
    provider = wsrep_provider or config.get_wsrep_provider()
    adv = advertise or config.get_advertise_address()
    member_list = [m.strip() for m in members.split(',') if m.strip()]
    result = galera.deploy_node(tarball, node_id, member_list, adv, cluster_name,
                                bootstrap=bootstrap, wsrep_provider=provider)
    _emit_deploy(ctx, result)


@main.command('repl-master')
@click.argument('tarball')
@click.argument('master_id')
@click.option('--advertise', 'advertise', default=None, metavar='IP',
              help="This host's reachable IP (default: config / "
                   'MYHAREM_ADVERTISE_ADDRESS).')
@click.pass_context
def repl_master(ctx, tarball, master_id, advertise):
    """Deploys + starts a replication master on this host (distributed)."""
    from . import replication
    adv = advertise or config.get_advertise_address()
    result = replication.deploy_master(tarball, master_id, advertise=adv)
    _emit_deploy(ctx, result)


@main.command('repl-slave')
@click.argument('tarball')
@click.argument('slave_id')
@click.option('--master-host', 'master_host', required=True,
              help="Master's reachable IP.")
@click.option('--master-port', 'master_port', required=True, type=int,
              help="Master's port (its instance id).")
@click.option('--advertise', 'advertise', default=None, metavar='IP',
              help="This host's reachable IP (default: config / "
                   'MYHAREM_ADVERTISE_ADDRESS).')
@click.pass_context
def repl_slave(ctx, tarball, slave_id, master_host, master_port, advertise):
    """Deploys + starts a replication slave on this host, wired to a master."""
    from . import replication
    adv = advertise or config.get_advertise_address()
    result = replication.deploy_slave(tarball, slave_id, master_host,
                                      master_port, advertise=adv)
    _emit_deploy(ctx, result)


# ---------- list / status ----------

def _collect_status():
    rows = []
    for inst in Instance.get_all_instances():
        rows.append({
            'id': inst.id,
            'status': inst.get_status(),
            'path': str(inst.path) if inst.path else None,
        })
    return sorted(rows, key=lambda r: int(r['id']) if r['id'].isdigit() else 0)


def _print_status_human(rows):
    if not rows:
        click.echo("No instances found.")
        return
    from collections import defaultdict
    groups = defaultdict(list)
    for row in rows:
        dirname = os.path.basename(row['path']) if row['path'] else "unknown"
        base = dirname.rsplit('.', 1)[0] if '.' in dirname else dirname
        groups[base].append(row)
    for base in sorted(groups):
        click.secho(f"\n  {base}", bold=True)
        for row in groups[base]:
            color = 'green' if row['status'] == 'Running' else 'red'
            click.echo(f"    {row['id']:<10} ", nl=False)
            click.secho(row['status'], fg=color)


@main.command(name='list')
@click.pass_context
def list_command(ctx):
    """Lists all deployed instances and their status."""
    rows = _collect_status()
    if ctx.obj.get('json'):
        click.echo(json.dumps({
            'instances': rows,
            'deployments': manifest.all_deployments(),
        }))
    else:
        _print_status_human(rows)


# ---------- service ----------

@main.group(name='service')
def service_group():
    """Manages MariaDB services (single instances)."""


@service_group.command()
@click.argument('instance_id')
@click.option('--bootstrap', is_flag=True,
              help='Galera: bootstrap a new cluster from this node.')
@click.pass_context
def start(ctx, instance_id, bootstrap):
    """Starts a MariaDB instance."""
    service.start_instance(instance_id, bootstrap=bootstrap)
    _emit_action(ctx, {'instance': instance_id, 'action': 'start', 'ok': True},
                 f"Instance {instance_id} started.")


@service_group.command()
@click.argument('instance_id')
@click.pass_context
def stop(ctx, instance_id):
    """Stops a MariaDB instance."""
    service.stop_instance(instance_id)
    _emit_action(ctx, {'instance': instance_id, 'action': 'stop', 'ok': True},
                 f"Instance {instance_id} stopped.")


@service_group.command(name='status')
@click.pass_context
def status_command(ctx):
    """Shows the status of all deployed instances."""
    rows = _collect_status()
    if ctx.obj.get('json'):
        click.echo(json.dumps({'instances': rows}))
    else:
        _print_status_human(rows)


# ---------- cluster (whole-deployment lifecycle) ----------

@main.group(name='cluster')
def cluster_group():
    """Manage a whole deployment (all nodes) by its cluster id."""


@cluster_group.command(name='start')
@click.argument('cluster_id')
def cluster_start(cluster_id):
    """Starts every node of a deployment in the correct order."""
    service.start_cluster(cluster_id)


@cluster_group.command(name='stop')
@click.argument('cluster_id')
def cluster_stop(cluster_id):
    """Stops every node of a deployment."""
    service.stop_cluster(cluster_id)


@cluster_group.command(name='erase')
@click.argument('cluster_id')
@click.option('--yes', is_flag=True, help='Skip the confirmation prompt.')
@click.option('--purge', is_flag=True,
              help='Delete data instead of moving to erased/.')
def cluster_erase(cluster_id, yes, purge):
    """Stops and removes every node of a deployment."""
    if not yes:
        entry = manifest.get(cluster_id)
        count = len(entry.get('nodes', [])) if entry else 0
        if not click.confirm(
            f"Erase deployment '{cluster_id}' ({count} nodes)? Data will be "
            f"{'deleted' if purge else 'moved to erased/'}."
        ):
            click.echo("Aborted.")
            return
    service.erase_cluster(cluster_id, purge=purge)


# ---------- scli / cli ----------

@main.command()
@click.argument('instance_id')
def scli(instance_id):
    """Connects to a MariaDB instance via socket (root)."""
    service.scli_instance(instance_id)


@main.command()
@click.argument('instance_id')
def cli(instance_id):
    """Connects to a MariaDB instance via TCP (root)."""
    service.cli_instance(instance_id)


# ---------- cd / chdir ----------

def _go_to_instance_dir(instance_id, open_shell=False):
    instance = Instance(instance_id)
    instance._require_exists()
    instance_path = str(instance.path)
    if open_shell:
        shell = os.environ.get('SHELL', '/bin/bash')
        os.chdir(instance_path)
        os.execv(shell, [shell])
    else:
        click.echo(instance_path)


@main.command(name='cd')
@click.argument('instance_id')
@click.option('--shell', 'open_shell', is_flag=True,
              help='Open a subshell in the instance directory.')
def cd_command(instance_id, open_shell):
    """Prints instance path (use with: cd \"$(mh cd <instance_id>)\")."""
    _go_to_instance_dir(instance_id, open_shell=open_shell)


@main.command(name='chdir', hidden=True)
@click.argument('instance_id')
@click.option('--shell', 'open_shell', is_flag=True)
def chdir_command(instance_id, open_shell):
    """Alias for 'mh cd'."""
    _go_to_instance_dir(instance_id, open_shell=open_shell)


# ---------- log ----------

@main.command()
@click.argument('instance_id')
@click.option('--lines', default=20, help='Number of lines to show.')
@click.option('--level', help='Filter by log level (e.g., ERROR, Warning).')
def log(instance_id, lines, level):
    """Shows the latest log entries for an instance."""
    instance = Instance(instance_id)
    instance._require_exists()
    for entry in instance.get_log_entries(num_lines=lines, level=level):
        click.echo(entry)


# ---------- var ----------

@main.command()
@click.argument('variable_name')
@click.pass_context
def var(ctx, variable_name):
    """Extracts a server variable from all running instances."""
    instances = Instance.get_all_instances()
    values = {inst.id: inst.get_variable(variable_name) for inst in instances}
    if ctx.obj.get('json'):
        click.echo(json.dumps({'variable': variable_name, 'values': values}))
        return
    if not values:
        click.echo("No instances found.")
        return
    click.echo(f"Variable '{variable_name}':")
    for inst_id, value in values.items():
        click.echo(f"  [{inst_id}] {value}")


# ---------- erase ----------

@main.command()
@click.argument('instance_id')
@click.option('--yes', is_flag=True, help='Skip the confirmation prompt.')
@click.option('--purge', is_flag=True,
              help='Delete data instead of moving to erased/.')
def erase(instance_id, yes, purge):
    """Removes a single instance (moves to erased/, or --purge to delete)."""
    instance = Instance(instance_id)
    instance._require_exists()

    if not yes:
        click.secho(f"WARNING: Instance {instance_id} will be erased!",
                    fg='red', bold=True)
        click.echo(f"  Path: {instance.path}")
        datadir = instance.path / 'data'
        if datadir.exists():
            total = sum(f.stat().st_size for f in datadir.rglob('*')
                        if f.is_file())
            click.echo(f"  Data size: {total / 1024 / 1024:.1f} MB")
        if not click.confirm("Are you sure?"):
            click.echo("Aborted.")
            return

    deployment.teardown_instance(instance_id, purge=purge)
    report.success(f"Instance {instance_id} erased.")


# ---------- show ----------

@main.group()
def show():
    """Lists available tarballs (local or remote)."""


@show.command(name='local')
def show_local():
    """Lists locally available tarballs."""
    local_dir = config.get_basedir() / 'local'
    if not local_dir.exists():
        click.echo("No local tarball directory found.")
        return
    tarballs = sorted(local_dir.glob('*.tar.gz'))
    if not tarballs:
        click.echo("No local tarballs found.")
        return
    click.echo("Available local tarballs:")
    click.echo("=" * 40)
    for i, t in enumerate(tarballs, 1):
        click.echo(f"  [L{i}] {t.name}")


# ---------- update ----------

@main.command()
def update():
    """Updates MyHarem from the GitHub repository."""
    import subprocess
    import tempfile

    repo_url = "https://github.com/claudionanni/myharem.git"
    # Canonical branch is master; override with MYHAREM_UPDATE_BRANCH if needed.
    branch = os.environ.get("MYHAREM_UPDATE_BRANCH", "master")
    click.echo(f"Updating MyHarem from {repo_url} ({branch})...")

    with tempfile.TemporaryDirectory() as tmpdir:
        click.echo("Cloning repository...")
        result = subprocess.run(
            ['git', 'clone', '--depth=1', '--branch', branch, repo_url, tmpdir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise click.ClickException(
                f"Failed to clone repository:\n{result.stderr}"
            )
        click.echo("Installing...")
        result = subprocess.run(
            ['pip', 'install', '--force-reinstall', '--no-deps', '.'],
            capture_output=True, text=True, cwd=tmpdir,
        )
        if result.returncode != 0:
            raise click.ClickException(f"Failed to install:\n{result.stderr}")

    click.secho("MyHarem updated successfully!", fg='green')
