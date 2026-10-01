"""Unit tests for myharem's pure logic and deploy orchestration.

These run without a real MariaDB tarball or root: the tar-extract, DB-init,
process start, and SQL steps are stubbed, so we validate port math, the
structured results, manifest recording, and rollback — not a live server.
"""

import getpass
import hashlib
import json
import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import mh
from mh import cli as cli_module
from mh import (catalog, config, deployment, galera, manifest, model,
                replication, report, service)
from mh.cli import main


@pytest.fixture
def basedir(tmp_path, monkeypatch):
    conf = tmp_path / "myharem.conf"
    conf.write_text(
        f"[DEFAULT]\nbasedir={tmp_path / 'harem'}\ndbuser={getpass.getuser()}\n"
    )
    monkeypatch.setenv("MYHAREM_CONF", str(conf))
    return tmp_path / "harem"


@pytest.fixture
def stub_deploy(basedir, monkeypatch):
    """Stub the infra steps (extract/init) so deploys need no real MariaDB."""
    def fake_deploy_instance(tarball, instance_id, init_db=True):
        path = basedir / "instances" / f"fake-11.8.6.{instance_id}"
        # Simulate a tarball that bundles the Galera provider under lib/galera/.
        (path / "lib" / "galera").mkdir(parents=True, exist_ok=True)
        (path / "lib" / "galera" / "libgalera_smm.so").write_text("")
        return path

    monkeypatch.setattr(deployment, "deploy_instance", fake_deploy_instance)
    monkeypatch.setattr(deployment, "initialize_database", lambda p: None)
    monkeypatch.setattr(
        deployment, "resolve_tarball", lambda t: Path("fake-11.8.6.tar.gz")
    )
    return basedir


# ---- pure port math ----

def test_compute_node_ids():
    assert galera.compute_node_ids(20000, 3) == [20000, 20010, 20020]
    assert galera.compute_node_ids(5000, 1) == [5000]


def test_compute_slave_ids():
    assert replication.compute_slave_ids(3000, 2) == [3010, 3020]
    assert replication.compute_slave_ids(3000, 1) == [3010]


# ---- result model ----

def test_model_serialization():
    result = model.DeploymentResult(
        topology="single", cluster_id="1", tarball="t.tar.gz",
        nodes=[model.NodeInfo(id="1", role="single", port=1,
                              socket="/tmp/mh-1.sock", datadir="/d", path="/p")],
    )
    payload = result.to_dict()
    assert payload["topology"] == "single"
    assert payload["nodes"][0]["port"] == 1
    json.dumps(payload)  # must be JSON-serializable


# ---- manifest ----

def test_manifest_roundtrip(basedir):
    result = model.DeploymentResult(
        topology="galera", cluster_id="20000", tarball="t", nodes=[]
    )
    manifest.record(result)
    assert manifest.get("20000")["topology"] == "galera"
    assert "20000" in manifest.all_deployments()
    manifest.remove("20000")
    assert manifest.get("20000") is None


# ---- galera deploy orchestration ----

def test_deploy_galera_records_result_and_manifest(stub_deploy):
    result = galera.deploy_cluster("fake-11.8.6.tar.gz", "20000", nodes=3)
    assert result.topology == "galera"
    assert [n.id for n in result.nodes] == ["20000", "20010", "20020"]
    assert result.nodes[0].wsrep_port == 20001
    assert result.nodes[0].sst_port == 20003
    assert manifest.get("20000")["topology"] == "galera"
    # every node has a generated my.cnf with a unique cluster name
    my_cnf = Path(result.nodes[0].path) / "my.cnf"
    assert "wsrep_cluster_name=mh_cluster_20000" in my_cnf.read_text()


def test_deploy_galera_rolls_back_on_failure(stub_deploy, basedir, monkeypatch):
    calls = {"n": 0}
    real = deployment.deploy_instance

    def flaky(tarball, instance_id, init_db=True):
        calls["n"] += 1
        if calls["n"] == 2:  # fail on the 2nd node
            raise RuntimeError("boom")
        return real(tarball, instance_id, init_db=init_db)

    monkeypatch.setattr(deployment, "deploy_instance", flaky)
    with pytest.raises(RuntimeError):
        galera.deploy_cluster("fake-11.8.6.tar.gz", "20000", nodes=3)
    # first node dir should have been rolled back (purged)
    assert not (basedir / "instances" / "fake-11.8.6.20000").exists()
    assert manifest.get("20000") is None


# ---- fetch_tarball (idempotent download into <basedir>/local/) ----

def test_fetch_tarball_downloads_when_missing(basedir, monkeypatch):
    calls = []

    def fake_download(url, dest, timeout=300):
        calls.append(url)
        Path(dest).write_text('fake-tarball-bytes')

    monkeypatch.setattr(deployment, '_download', fake_download)
    dest = deployment.fetch_tarball('https://example.org/mariadb-11.4.8.tar.gz')

    assert dest == basedir / 'local' / 'mariadb-11.4.8.tar.gz'
    assert dest.read_text() == 'fake-tarball-bytes'
    assert calls == ['https://example.org/mariadb-11.4.8.tar.gz']


def test_fetch_tarball_skips_download_when_already_staged(basedir, monkeypatch):
    local_dir = basedir / 'local'
    local_dir.mkdir(parents=True)
    existing = local_dir / 'mariadb-11.4.8.tar.gz'
    existing.write_text('already-here')

    def fail_download(url, dest, timeout=300):
        raise AssertionError('should not download when already staged')

    monkeypatch.setattr(deployment, '_download', fail_download)
    dest = deployment.fetch_tarball('https://example.org/mariadb-11.4.8.tar.gz')

    assert dest == existing
    assert dest.read_text() == 'already-here'


def test_fetch_tarball_honors_name_override_for_presigned_urls(basedir, monkeypatch):
    monkeypatch.setattr(
        deployment, '_download',
        lambda url, dest, timeout=300: Path(dest).write_text('x'),
    )
    dest = deployment.fetch_tarball(
        'https://s3.example.com/bucket/key?X-Amz-Signature=abc123',
        filename='mariadb-11.4.8-linux-systemd-x86_64.tar.gz',
    )
    assert dest.name == 'mariadb-11.4.8-linux-systemd-x86_64.tar.gz'


def test_fetch_tarball_cleans_up_partial_file_on_failure(basedir, monkeypatch):
    def failing_download(url, dest, timeout=300):
        Path(dest).write_text('partial')
        raise OSError('connection reset')

    monkeypatch.setattr(deployment, '_download', failing_download)
    with pytest.raises(click.ClickException, match='Failed to fetch tarball'):
        deployment.fetch_tarball('https://example.org/mariadb-11.4.8.tar.gz')

    assert not (basedir / 'local' / 'mariadb-11.4.8.tar.gz').exists()
    assert not (basedir / 'local' / 'mariadb-11.4.8.tar.gz.part').exists()


# ---- service orchestration (joiner sync must be WSREP-aware, not socket-only) ----

class _FakeJoinerInstance:
    """A Galera joiner whose socket is ready immediately but whose WSREP sync
    completes only after `synced_after` polls — reproducing the real timing
    (socket connectable before SST/WSREP sync actually finishes)."""

    def __init__(self, synced_after):
        self.id = "30000"
        self._polls = 0
        self._synced_after = synced_after
        self.started_with = None

    def _require_exists(self):
        pass

    def start(self, wsrep_new_cluster=False):
        self.started_with = wsrep_new_cluster

    def is_socket_ready(self):
        return True

    def wsrep_local_state_comment(self):
        self._polls += 1
        return "Synced" if self._polls >= self._synced_after else "Joined"


def test_start_instance_waits_for_wsrep_sync_before_declaring_joiner_ok(monkeypatch):
    fake = _FakeJoinerInstance(synced_after=3)
    monkeypatch.setattr(service, "Instance", lambda instance_id: fake)
    monkeypatch.setattr(service, "_is_galera_instance", lambda instance: True)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)

    service.start_instance("30000", bootstrap=False)

    # Declared success only once wsrep_local_state_comment() actually said
    # "Synced" — not on the first is_socket_ready() poll.
    assert fake._polls == 3
    assert fake.started_with is False


def test_start_instance_joiner_times_out_if_never_synced(monkeypatch):
    fake = _FakeJoinerInstance(synced_after=10_000)  # never reaches "Synced"
    monkeypatch.setattr(service, "Instance", lambda instance_id: fake)
    monkeypatch.setattr(service, "_is_galera_instance", lambda instance: True)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)

    with pytest.raises(click.ClickException, match="did not sync within 5 min"):
        service.start_instance("30000", bootstrap=False)


# ---- bootstrap/non-joiner readiness must be connection-based, not socket-file-based
# (the false-success bug's sibling: is_socket_ready() can be True well before
# mariadbd, with Galera/wsrep enabled, is actually answering queries) ----

class _FakeBootstrapInstance:
    """A bootstrap node whose socket FILE exists immediately but that doesn't
    actually accept connections until `connectable_after` polls -- reproducing
    the real production timing bug (is_socket_ready() true, then an immediate
    'Connection refused' on the very next real query)."""

    def __init__(self, connectable_after):
        self.id = "40000"
        self._polls = 0
        self._connectable_after = connectable_after
        self.started_with = None

    def _require_exists(self):
        pass

    def start(self, wsrep_new_cluster=False):
        self.started_with = wsrep_new_cluster

    def is_socket_ready(self):
        return True

    def is_accepting_connections(self):
        self._polls += 1
        return self._polls >= self._connectable_after


def test_start_instance_waits_for_a_real_connection_before_creating_users(monkeypatch):
    fake = _FakeBootstrapInstance(connectable_after=3)
    monkeypatch.setattr(service, "Instance", lambda instance_id: fake)
    monkeypatch.setattr(service, "_is_galera_instance", lambda instance: False)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(deployment, "create_service_users", lambda inst: True)

    service.start_instance("40000", bootstrap=True)

    # Declared ready only once a real connection actually succeeded, not on
    # the first is_socket_ready() poll (always True for this fake).
    assert fake._polls == 3
    assert fake.started_with is True


def test_start_instance_raises_clearly_when_service_users_fail_to_create(monkeypatch):
    """Regression: user creation failing used to only warn and let the deploy
    limp forward -- a downstream joiner would then fail confusingly 5 minutes
    later with no obvious link to the real cause. Must fail fast, right here,
    instead."""
    fake = _FakeBootstrapInstance(connectable_after=1)
    monkeypatch.setattr(service, "Instance", lambda instance_id: fake)
    monkeypatch.setattr(service, "_is_galera_instance", lambda instance: False)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(deployment, "create_service_users", lambda inst: False)

    with pytest.raises(click.ClickException, match="service users/grants could not be created"):
        service.start_instance("40000", bootstrap=False)


# ---- galera provider resolution (the linux-x86_64 "no bundled Galera" bug) ----

def _bare_instance_stub(basedir, monkeypatch):
    """Stub deploy_instance to produce an instance whose tarball did NOT bundle
    the Galera provider (a lib/ with no libgalera*.so)."""
    def bare_deploy_instance(tarball, instance_id, init_db=True):
        path = basedir / "instances" / f"bare.{instance_id}"
        (path / "lib").mkdir(parents=True, exist_ok=True)
        return path
    monkeypatch.setattr(deployment, "deploy_instance", bare_deploy_instance)
    monkeypatch.setattr(deployment, "initialize_database", lambda p: None)
    monkeypatch.setattr(
        deployment, "resolve_tarball", lambda t: Path("bare.tar.gz")
    )


def test_galera_deploy_fails_loudly_when_provider_absent(basedir, monkeypatch):
    _bare_instance_stub(basedir, monkeypatch)
    with pytest.raises(click.ClickException, match="No Galera provider"):
        galera.deploy_cluster("bare.tar.gz", "20000", nodes=1)
    # nothing recorded; the partial node is rolled back rather than left broken
    assert manifest.get("20000") is None
    assert not (basedir / "instances" / "bare.20000").exists()


def test_galera_honors_wsrep_provider_override(basedir, tmp_path, monkeypatch):
    _bare_instance_stub(basedir, monkeypatch)
    provider = tmp_path / "libgalera_smm.so"  # supplied out-of-band
    provider.write_text("")
    result = galera.deploy_cluster(
        "bare.tar.gz", "20000", nodes=1, wsrep_provider=str(provider)
    )
    assert result.topology == "galera"
    my_cnf = Path(result.nodes[0].path) / "my.cnf"
    assert f"wsrep_provider={provider}" in my_cnf.read_text()


def test_wsrep_provider_override_missing_path_errors(basedir, monkeypatch):
    _bare_instance_stub(basedir, monkeypatch)
    with pytest.raises(click.ClickException, match="does not exist"):
        galera.deploy_cluster(
            "bare.tar.gz", "20000", nodes=1,
            wsrep_provider="/nope/libgalera_smm.so",
        )


def test_wsrep_provider_config_resolution(monkeypatch):
    monkeypatch.setenv("MYHAREM_WSREP_PROVIDER", "/opt/galera/libgalera_smm.so")
    assert config.get_wsrep_provider() == "/opt/galera/libgalera_smm.so"
    monkeypatch.delenv("MYHAREM_WSREP_PROVIDER", raising=False)
    monkeypatch.setenv("MYHAREM_CONF", "/nonexistent/myharem.conf")
    assert config.get_wsrep_provider() is None


# ---- multi-host / advertise address ----

def test_gcomm_builds_address():
    assert galera._gcomm(["1.2.3.4:11000", "5.6.7.8:21000"]) == (
        "gcomm://1.2.3.4:11000,5.6.7.8:21000"
    )


def test_advertise_address_config(monkeypatch):
    monkeypatch.setenv("MYHAREM_ADVERTISE_ADDRESS", "192.168.1.50")
    assert config.get_advertise_address() == "192.168.1.50"
    monkeypatch.delenv("MYHAREM_ADVERTISE_ADDRESS", raising=False)
    monkeypatch.setenv("MYHAREM_CONF", "/nonexistent/myharem.conf")
    assert config.get_advertise_address() == "127.0.0.1"


def test_deploy_galera_single_host_is_loopback(stub_deploy):
    # Regression guard: the default (colocated) output must stay loopback.
    result = galera.deploy_cluster("fake-11.8.6.tar.gz", "20000", nodes=2)
    my_cnf = (Path(result.nodes[0].path) / "my.cnf").read_text()
    assert "wsrep_node_address=127.0.0.1:20001" in my_cnf
    assert "gmcast.listen_addr=tcp://127.0.0.1:20001" in my_cnf
    assert "wsrep_sst_receive_address=127.0.0.1:20003" in my_cnf
    assert "wsrep_cluster_address=gcomm://127.0.0.1:20001,127.0.0.1:20011" in my_cnf


def test_deploy_galera_advertises_real_ip(stub_deploy):
    result = galera.deploy_cluster(
        "fake-11.8.6.tar.gz", "20000", nodes=2, advertise="10.0.0.5"
    )
    my_cnf = (Path(result.nodes[0].path) / "my.cnf").read_text()
    assert "wsrep_node_address=10.0.0.5:20001" in my_cnf
    assert "gmcast.listen_addr=tcp://0.0.0.0:20001" in my_cnf  # listen all ifaces
    assert "wsrep_sst_receive_address=10.0.0.5:20003" in my_cnf
    assert "wsrep_cluster_address=gcomm://10.0.0.5:20001,10.0.0.5:20011" in my_cnf


def test_deploy_node_distributed(stub_deploy):
    # Member-list entries are opaque peer addresses supplied by the caller
    # (representing other hosts in a distributed cluster) -- not derived from
    # this node's own id, so they're left as arbitrary fixture values.
    members = ["10.0.0.1:21000", "10.0.0.2:21000"]
    result = galera.deploy_node(
        "fake-11.8.6.tar.gz", "20000", members, "10.0.0.2", "mh_env42"
    )
    assert result.topology == "galera" and result.nodes[0].id == "20000"
    my_cnf = (Path(result.nodes[0].path) / "my.cnf").read_text()
    assert "wsrep_cluster_address=gcomm://10.0.0.1:21000,10.0.0.2:21000" in my_cnf
    assert "wsrep_node_address=10.0.0.2:20001" in my_cnf
    assert "gmcast.listen_addr=tcp://0.0.0.0:20001" in my_cnf
    assert "wsrep_cluster_name=mh_env42" in my_cnf
    assert manifest.get("20000")["topology"] == "galera"


def test_replication_grant_host():
    default_sql = deployment._create_users_sql()
    wide_sql = deployment._create_users_sql("%")
    assert "'mh_repl'@'localhost'" in default_sql
    assert "'mh_repl'@'%'" in wide_sql
    # admin + sst stay local regardless of repl_host
    assert "'myharem'@'localhost'" in wide_sql
    assert "'mh_sst'@'localhost'" in wide_sql


# ---- replication deploy orchestration ----

def test_deploy_replication_records_result(stub_deploy, monkeypatch):
    monkeypatch.setattr(replication, "_wait_for_instance",
                        lambda inst, timeout=30: None)
    monkeypatch.setattr(deployment, "create_service_users",
                        lambda inst, retries=5, repl_host='localhost': True)
    monkeypatch.setattr("mh.instance.Instance.start",
                        lambda self, wsrep_new_cluster=False: None)
    monkeypatch.setattr("mh.instance.Instance.run_sql",
                        lambda self, sql, timeout=10: "")

    result = replication.deploy_replication("fake-11.8.6.tar.gz", "3000", slaves=2)
    assert result.topology == "replication"
    roles = [(n.id, n.role) for n in result.nodes]
    assert roles == [("3000", "master"), ("3010", "slave"), ("3020", "slave")]
    assert manifest.get("3000")["topology"] == "replication"

    # Regression: a bare relative relay-log basename left relay_log_index's
    # location to server defaults, which failed at START SLAVE time with
    # "File './relay-bin.index' not found" -- both must be absolute paths
    # inside the slave's own datadir.
    slave_my_cnf = (Path(result.nodes[1].path) / "my.cnf").read_text()
    slave_datadir = Path(result.nodes[1].path) / "data"
    assert f"relay_log={slave_datadir / 'relay-bin.3010'}" in slave_my_cnf
    assert f"relay_log_index={slave_datadir / 'relay-bin.3010.index'}" in slave_my_cnf


def test_deploy_replication_raises_and_rolls_back_when_master_grants_fail(
    stub_deploy, monkeypatch,
):
    """Sibling of the galera bootstrap fix: a replication slave depends on
    REPL_USER existing on the master. Silently continuing past a failed grant
    step would surface later as a confusing CHANGE MASTER/START SLAVE auth
    failure instead of a clear one right at the source."""
    monkeypatch.setattr(replication, "_wait_for_instance",
                        lambda inst, timeout=30: None)
    monkeypatch.setattr(deployment, "create_service_users",
                        lambda inst, retries=5, repl_host='localhost': False)
    monkeypatch.setattr("mh.instance.Instance.start",
                        lambda self, wsrep_new_cluster=False: None)
    rolled_back = []
    monkeypatch.setattr(deployment, "rollback_instances", rolled_back.append)

    with pytest.raises(click.ClickException, match="service users/grants could not be created"):
        replication.deploy_replication("fake-11.8.6.tar.gz", "4000", slaves=1)

    # Rolled back exactly what had actually been deployed (master + slave),
    # not left as orphaned instances.
    assert rolled_back == [["4000", "4010"]]


# ---- instance id uniqueness (the duplicate-.39000 resolution bug) ----

def test_instance_id_resolution_is_unambiguous(basedir):
    from mh.instance import Instance
    insts = basedir / "instances"
    (insts / "mariadb-11.4.7.39000").mkdir(parents=True)
    (insts / "mariadb-11.8.5.39000").mkdir(parents=True)
    # two dirs share id 39000 -> must fail loudly, not pick one arbitrarily
    with pytest.raises(click.ClickException, match="Ambiguous instance id"):
        Instance("39000")
    # a unique id still resolves cleanly
    (insts / "mariadb-11.4.7.19000").mkdir(parents=True)
    assert Instance("19000").path.name == "mariadb-11.4.7.19000"


def test_deploy_instance_refuses_duplicate_id(basedir, monkeypatch):
    (basedir / "instances" / "old-version.39000").mkdir(parents=True)
    monkeypatch.setattr(
        deployment, "resolve_tarball", lambda t: Path("mariadb-new.tar.gz")
    )
    with pytest.raises(click.ClickException, match="already in use"):
        deployment.deploy_instance("mariadb-new.tar.gz", "39000", init_db=False)


def test_purge_removes_directory(basedir):
    d = basedir / "instances" / "v.51000"
    (d / "data").mkdir(parents=True)
    deployment.teardown_instance("51000", purge=True)
    assert not d.exists()


def test_purge_reports_failure_when_nothing_removed(basedir, monkeypatch):
    d = basedir / "instances" / "v.52000"
    (d / "data").mkdir(parents=True)
    # Simulate a delete that can't remove the files (e.g. permission denied)
    # without raising — the old ignore_errors=True path swallowed exactly this.
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    with pytest.raises(click.ClickException, match="Purge removed nothing"):
        deployment.teardown_instance("52000", purge=True)
    assert d.exists()  # untouched — and the caller was told, not misled


# ---- create_service_users reports success/failure instead of only logging ----

def test_create_service_users_returns_true_on_success(basedir, monkeypatch):
    d = basedir / "instances" / "v.58000"
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "mariadb").touch()
    from mh.instance import Instance

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    assert deployment.create_service_users(Instance("58000")) is True


def test_create_service_users_returns_false_after_exhausting_retries(basedir, monkeypatch):
    d = basedir / "instances" / "v.58010"
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "mariadb").touch()
    from mh.instance import Instance

    monkeypatch.setattr(deployment, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="Access denied"),
    )
    assert deployment.create_service_users(Instance("58010"), retries=2) is False


# ---- process-liveness must be PID-based, not DB-auth-based (the orphaned
# mariadbd-after-rollback bug: is_running()/stop() authenticate as the service
# admin user, which a partially-failed deploy may never have created, so they
# falsely report "not running" for a process that is very much alive) ----

def test_find_pid_reads_the_pid_file_and_confirms_liveness(basedir):
    from mh.instance import Instance
    d = basedir / "instances" / "v.53000"
    d.mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        (d / "53000.pid").write_text(str(proc.pid))
        assert Instance("53000").find_pid() == proc.pid
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_find_pid_ignores_a_pid_file_for_an_already_dead_process(basedir):
    from mh.instance import Instance
    d = basedir / "instances" / "v.54000"
    d.mkdir(parents=True)
    proc = subprocess.Popen(["true"])
    proc.wait()
    (d / "54000.pid").write_text(str(proc.pid))
    assert Instance("54000").find_pid() is None


def test_terminate_kills_a_real_running_process(basedir):
    from mh.instance import Instance
    d = basedir / "instances" / "v.55000"
    d.mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "30"])
    (d / "55000.pid").write_text(str(proc.pid))
    Instance("55000").terminate(timeout=5)
    assert proc.wait(timeout=5) is not None  # process actually exited


def test_terminate_is_a_noop_when_nothing_is_running(basedir):
    from mh.instance import Instance
    d = basedir / "instances" / "v.56000"
    d.mkdir(parents=True)
    Instance("56000").terminate()  # must not raise


def test_teardown_kills_an_orphan_even_when_is_running_falsely_reports_stopped(
    basedir, monkeypatch,
):
    """The exact production regression: is_running() (DB-auth-based) says
    "not running" even though mariadbd is alive, because the admin user was
    never created -- teardown must still find and kill the real process
    before deleting its directory, or it orphans a live process holding
    deleted files open."""
    d = basedir / "instances" / "v.57000"
    d.mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "30"])
    (d / "57000.pid").write_text(str(proc.pid))

    monkeypatch.setattr("mh.instance.Instance.is_running", lambda self: False)

    deployment.teardown_instance("57000", purge=True)

    assert proc.wait(timeout=5) is not None  # terminate() killed it despite the lie
    assert not d.exists()


# ---- CLI smoke ----

def test_cli_help_lists_commands():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in ("deploygalera", "deployreplication", "cluster", "erase"):
        assert cmd in result.output


def test_cli_wizard_uses_real_step_constants_not_hardcoded():
    """Regression guard: the interactive wizard once hardcoded the literal
    10000 for galera/replication id spacing instead of importing
    galera.INST_STEP/replication.REPL_STEP, silently desyncing the moment
    those constants changed. Assert it references the real constants."""
    import inspect
    from mh import cli as cli_module

    source = inspect.getsource(cli_module._deploy_wizard)
    assert "galera.INST_STEP" in source
    assert "replication.REPL_STEP" in source
    assert "10000" not in source


# ---- port ceiling ----

def test_compute_node_ids_rejects_topology_exceeding_port_ceiling():
    huge_nodes = (galera.MAX_PORT - galera.SST_STEP) // galera.INST_STEP + 2
    with pytest.raises(click.ClickException, match="exceeds"):
        galera.compute_node_ids(1, huge_nodes)


def test_compute_slave_ids_rejects_topology_exceeding_port_ceiling():
    with pytest.raises(click.ClickException, match="exceeds"):
        replication.compute_slave_ids(galera.MAX_PORT - 5, 3)


# ---- configurable credentials ----

def test_credential_env_overrides(monkeypatch):
    monkeypatch.setenv("MYHAREM_ADMIN_PASSWORD", "sekret")
    monkeypatch.setenv("MYHAREM_SST_PASSWORD", "ssts3cret")
    assert config.get_admin_password() == "sekret"
    assert config.get_sst_password() == "ssts3cret"


def test_credential_defaults(monkeypatch):
    monkeypatch.delenv("MYHAREM_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("MYHAREM_SST_PASSWORD", raising=False)
    monkeypatch.setenv("MYHAREM_CONF", "/nonexistent/myharem.conf")
    assert config.get_admin_password() == ""
    assert config.get_sst_password() == "sstpwd"


def test_version_flag_reports_the_package_version():
    runner = CliRunner()
    result = runner.invoke(main, ['--version'])
    assert result.exit_code == 0
    assert mh.__version__ in result.output


def test_setup_py_version_matches_the_package():
    """setup.py reads __version__ rather than carrying a copy.

    A release that bumps one and not the other ships a wheel whose metadata
    disagrees with what `mh --version` prints — which is precisely the question
    the flag exists to answer.
    """
    import re
    from pathlib import Path

    setup_py = (Path(__file__).resolve().parent.parent / 'setup.py').read_text()
    assert "version=version," in setup_py
    assert not re.search(r"version=['\"]\d", setup_py)


# --------------------------------------------------------------------------
# mh download — catalog parsing, secret handling, staging, CLI
# --------------------------------------------------------------------------

CS_INDEX = json.dumps({
    "major_releases": [
        {"release_id": "13.2", "release_status": "Preview"},
        {"release_id": "11.4", "release_status": "Stable",
         "release_support_type": "Long Term Support",
         "release_eol_date": "2029-05-29"},
        {"release_id": "10.6", "release_status": "Stable"},
    ]
})

CS_SERIES_11_4 = json.dumps({
    "releases": {"11.4.9": {}, "11.4.13": {}, "11.4.10": {}}
})

CS_VERSION_11_4_13 = json.dumps({
    "release_data": {
        "11.4.13": {
            "files": [
                {"file_name": "mariadb-11.4.13.tar.gz",
                 "package_type": "gzipped tar file", "os": "Source", "cpu": None,
                 "checksum": {"sha256sum": "source-hash"},
                 "file_download_url": "http://example.org/src.tar.gz"},
                {"file_name": "mariadb-11.4.13-winx64.msi",
                 "package_type": "MSI Package", "os": "Windows", "cpu": "x86_64",
                 "checksum": {}, "file_download_url": "http://example.org/w.msi"},
                {"file_name": "yum/", "package_type": None, "os": None,
                 "cpu": None, "checksum": {}, "file_download_url": None},
                {"file_name": "mariadb-11.4.13-linux-systemd-x86_64.tar.gz",
                 "package_type": "gzipped tar file", "os": "Linux", "cpu": "x86_64",
                 "checksum": {"sha256sum": "ABCDEF"},
                 "file_download_url":
                     "http://downloads.mariadb.org/x/mariadb-11.4.13-linux-systemd-x86_64.tar.gz"},
            ]
        }
    }
})

ES_RELEASES_TEXT = "10.6.28-24 11.4.13-10 11.8.9-6 12.3.3-0"

ES_SERIES_HTML = """
<html><body>
 <a href="/browse/TOK/mariadb_enterprise_server/11.4.13-10/">11.4.13-10</a>
 <a href="/browse/TOK/mariadb_enterprise_server/11.4.9-6/">11.4.9-6</a>
 <a href="/browse/TOK/mariadb_enterprise_server/11.4.12-9/">11.4.12-9</a>
</body></html>
"""

ES_BINTAR_HTML = """
<html><body>
 <a href="https://dlm.mariadb.com/TOK/9911/es/mariadb-enterprise-11.4.13-10-ubuntu-2204-x86_64.tar.gz">other</a>
 <a href="https://dlm.mariadb.com/TOK/9912/es/mariadb-enterprise-11.4.13-10-rhel-9-x86_64.tar.gz">this one</a>
</body></html>
"""


def _canned_fetch(mapping):
    """A catalog._fetch_url stand-in dispatching on a URL fragment."""
    def fake(url, timeout=30):
        for fragment, body in mapping.items():
            if fragment in url:
                return body
        raise AssertionError(f"unexpected URL fetched: {url}")
    return fake


CS_ROUTES = {
    "rest-api/mariadb/11.4/": CS_SERIES_11_4,
    "rest-api/mariadb/11.4.13/": CS_VERSION_11_4_13,
    "rest-api/mariadb/": CS_INDEX,
}


# ---- Community catalog ----

def test_cs_series_puts_stable_before_preview(monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    series = catalog.list_cs_series()
    assert [s.id for s in series] == ['11.4', '10.6', '13.2']
    assert series[-1].status == 'Preview'


def test_cs_releases_sort_numerically_not_lexically(monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    assert catalog.list_cs_releases('11.4') == ['11.4.13', '11.4.10', '11.4.9']


def test_cs_file_selection_excludes_the_source_tarball(monkeypatch):
    """The bug that would silently deploy a source tree instead of a bindist."""
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    artifact = catalog.resolve_cs_artifact('11.4.13')
    assert artifact.filename == 'mariadb-11.4.13-linux-systemd-x86_64.tar.gz'


def test_cs_file_selection_prefers_linux_systemd_over_generic():
    """linux-systemd is the flavour that bundles the Galera provider."""
    files = [
        {"file_name": "mariadb-11.4.13-linux-x86_64.tar.gz",
         "package_type": "gzipped tar file", "os": "Linux", "cpu": "x86_64"},
        {"file_name": "mariadb-11.4.13-linux-systemd-x86_64.tar.gz",
         "package_type": "gzipped tar file", "os": "Linux", "cpu": "x86_64"},
    ]
    chosen = catalog._pick_cs_file(files, want_arch='x86_64')
    assert chosen['file_name'] == 'mariadb-11.4.13-linux-systemd-x86_64.tar.gz'


def test_cs_artifact_upgrades_http_to_https_and_takes_sha256(monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    artifact = catalog.resolve_cs_artifact('11.4.13')
    assert artifact.url.startswith('https://')
    assert artifact.sha256 == 'ABCDEF'


def test_cs_version_resolution_takes_the_newest_in_a_series(monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    assert catalog.resolve_cs_version('11.4') == '11.4.13'
    assert catalog.resolve_cs_version('11.4.10') == '11.4.10'


# ---- Enterprise catalog ----

def test_es_series_parses_whitespace_separated_text(monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url',
                        _canned_fetch({"rest/releases": ES_RELEASES_TEXT}))
    assert catalog.list_es_series() == ['12.3', '11.8', '11.4', '10.6']


def test_es_release_listing_is_scraped_and_sorted(monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url',
                        _canned_fetch({"/browse/": ES_SERIES_HTML}))
    assert catalog.list_es_releases('11.4', 'TOK') == [
        '11.4.13-10', '11.4.12-9', '11.4.9-6']


def test_es_build_suffix_sorts_numerically():
    """A string sort would put 11.4.9-6 above 11.4.12-9."""
    assert catalog.sort_releases_desc(['11.4.9-6', '11.4.12-9'])[0] == '11.4.12-9'


def test_es_partial_version_resolves_to_the_newest_build(monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url',
                        _canned_fetch({"/browse/": ES_SERIES_HTML}))
    assert catalog.resolve_es_version('11.4', 'TOK') == '11.4.13-10'
    assert catalog.resolve_es_version('11.4.12', 'TOK') == '11.4.12-9'
    assert catalog.resolve_es_version('11.4.12-9', 'TOK') == '11.4.12-9'


def test_es_artifact_ignores_a_decoy_for_another_target(monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url',
                        _canned_fetch({"/bintar/": ES_BINTAR_HTML}))
    artifact = catalog.resolve_es_artifact('11.4.13-10', 'rhel-9-x86_64', 'TOK')
    assert artifact.filename.endswith('rhel-9-x86_64.tar.gz')
    assert '9912' in artifact.url
    assert artifact.sha256 is None


def test_es_missing_artifact_names_the_release_and_target(monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url',
                        _canned_fetch({"/bintar/": "<html></html>"}))
    with pytest.raises(click.ClickException) as excinfo:
        catalog.resolve_es_artifact('11.4.13-10', 'debian-12-x86_64', 'TOK')
    assert 'debian-12-x86_64' in str(excinfo.value)
    assert 'listing format may have changed' in str(excinfo.value)


# ---- secrets ----

def test_report_redact_strips_urls():
    assert 'SEKRET' not in report.redact(
        'boom https://dlm.mariadb.com/browse/SEKRET/x refused')


def test_es_failure_never_leaks_the_tokenised_url(monkeypatch):
    def exploding(url, timeout=30):
        raise OSError(
            "connection reset for "
            "https://dlm.mariadb.com/browse/SEKRET-TOKEN/mariadb_enterprise_server/")
    monkeypatch.setattr(catalog, '_fetch_url', exploding)
    with pytest.raises(click.ClickException) as excinfo:
        catalog.list_es_releases('11.4', 'SEKRET-TOKEN')
    assert 'SEKRET-TOKEN' not in str(excinfo.value)
    assert 'dlm.mariadb.com' not in str(excinfo.value)


def test_download_command_never_prints_the_token(basedir, monkeypatch):
    def exploding(url, timeout=30):
        raise OSError(
            "refused: https://dlm.mariadb.com/browse/SEKRET-TOKEN/x/")
    monkeypatch.setattr(catalog, '_fetch_url', exploding)
    monkeypatch.setenv('MYHAREM_ES_TOKEN', 'SEKRET-TOKEN')
    monkeypatch.setenv('MYHAREM_ES_BINTAR_TARGET', 'rhel-9-x86_64')
    result = CliRunner().invoke(main, ['download', '-e', 'ES', '-v', '11.4'])
    assert result.exit_code != 0
    assert 'SEKRET' not in result.output
    assert 'SEKRET' not in str(result.exception)


def test_es_refuses_before_any_network_when_no_token(basedir, monkeypatch):
    def must_not_be_called(url, timeout=30):
        raise AssertionError("no network before the token is checked")
    monkeypatch.delenv('MYHAREM_ES_TOKEN', raising=False)
    # list_es_series is allowed to fail; the refusal must still name the variable
    monkeypatch.setattr(catalog, '_fetch_url', must_not_be_called)
    result = CliRunner().invoke(main, ['download', '-e', 'ES', '-v', '11.4'])
    assert result.exit_code != 0
    assert 'MYHAREM_ES_TOKEN' in result.output


def test_download_has_no_token_flag():
    """A --token flag would put the secret in shell history and in `ps`."""
    result = CliRunner().invoke(main, ['download', '--help'])
    assert '--token' not in result.output


def test_world_readable_config_holding_the_token_warns(tmp_path, monkeypatch):
    conf = tmp_path / 'myharem.conf'
    conf.write_text("[DEFAULT]\nes_token=SEKRET\n")
    conf.chmod(0o644)
    monkeypatch.setenv('MYHAREM_CONF', str(conf))
    monkeypatch.delenv('MYHAREM_ES_TOKEN', raising=False)
    warnings = []
    monkeypatch.setattr(report, 'warn', warnings.append)
    config.warn_if_config_is_world_readable()
    assert warnings and 'chmod 600' in warnings[0]
    assert 'SEKRET' not in warnings[0]


# ---- staging + checksum ----

def test_stage_tarball_verifies_a_matching_sha256(basedir, monkeypatch):
    payload = b'fake-tarball-bytes'
    monkeypatch.setattr(deployment, '_download',
                        lambda url, dest, timeout=300: Path(dest).write_bytes(payload))
    dest = deployment.stage_tarball(
        'https://example.org/x.tar.gz', 'x.tar.gz',
        sha256=hashlib.sha256(payload).hexdigest())
    assert dest.read_bytes() == payload


def test_stage_tarball_discards_a_checksum_mismatch(basedir, monkeypatch):
    monkeypatch.setattr(deployment, '_download',
                        lambda url, dest, timeout=300: Path(dest).write_bytes(b'x'))
    with pytest.raises(click.ClickException, match='Checksum mismatch'):
        deployment.stage_tarball('https://example.org/x.tar.gz', 'x.tar.gz',
                                 sha256='0' * 64)
    assert not (basedir / 'local' / 'x.tar.gz').exists()
    assert not (basedir / 'local' / 'x.tar.gz.part').exists()


def test_stage_tarball_errors_name_the_label_not_the_url(basedir, monkeypatch):
    def failing(url, dest, timeout=300):
        raise OSError("refused by https://dlm.mariadb.com/browse/SEKRET/x")
    monkeypatch.setattr(deployment, '_download', failing)
    with pytest.raises(click.ClickException) as excinfo:
        deployment.stage_tarball('https://dlm.mariadb.com/browse/SEKRET/x',
                                 'es.tar.gz', label='es.tar.gz')
    assert 'SEKRET' not in str(excinfo.value)
    assert 'es.tar.gz' in str(excinfo.value)


def test_stage_tarball_rejects_a_staged_file_with_the_wrong_checksum(basedir):
    local = basedir / 'local'
    local.mkdir(parents=True, exist_ok=True)
    (local / 'x.tar.gz').write_bytes(b'truncated')
    with pytest.raises(click.ClickException, match='already staged'):
        deployment.stage_tarball('https://example.org/x.tar.gz', 'x.tar.gz',
                                 sha256='0' * 64)


# ---- bintar target detection ----

def test_dlm_target_maps_the_rhel_family_by_major():
    assert catalog.dlm_target_from_os_release(
        'ID=rocky\nVERSION_ID="9.4"\n', 'x86_64') == 'rhel-9-x86_64'
    assert catalog.dlm_target_from_os_release(
        'ID=rhel\nVERSION_ID="8.10"\n', 'aarch64') == 'rhel-8-aarch64'


def test_dlm_target_compacts_the_ubuntu_version():
    assert catalog.dlm_target_from_os_release(
        'ID=ubuntu\nVERSION_ID="22.04"\n', 'x86_64') == 'ubuntu-2204-x86_64'


def test_dlm_target_is_none_for_an_unmapped_distro():
    assert catalog.dlm_target_from_os_release(
        'ID=fedora\nVERSION_ID=42\n', 'x86_64') is None


def test_es_target_resolution_prefers_flag_then_config(tmp_path):
    os_release = tmp_path / 'os-release'
    os_release.write_text('ID=rocky\nVERSION_ID="9.4"\n')
    assert catalog.resolve_es_target(
        explicit='ubuntu-2204-x86_64', configured='debian-12-x86_64',
        os_release=str(os_release))[0] == 'ubuntu-2204-x86_64'
    assert catalog.resolve_es_target(
        configured='debian-12-x86_64', os_release=str(os_release))[0] == 'debian-12-x86_64'
    assert catalog.resolve_es_target(os_release=str(os_release)) == (
        'rhel-9-x86_64', 'detected')


def test_es_target_refusal_names_the_flag_and_known_targets(tmp_path):
    os_release = tmp_path / 'os-release'
    os_release.write_text('ID=fedora\nVERSION_ID=42\n')
    with pytest.raises(click.ClickException) as excinfo:
        catalog.resolve_es_target(os_release=str(os_release))
    assert '--target' in str(excinfo.value)
    assert 'rhel-9-x86_64' in str(excinfo.value)


# ---- CLI ----

def test_download_requires_flags_when_there_is_no_terminal(basedir):
    result = CliRunner().invoke(main, ['download'])
    assert result.exit_code == 2
    assert '--edition' in result.output


def test_download_json_mode_never_prompts(basedir):
    result = CliRunner().invoke(main, ['--json', 'download'])
    assert result.exit_code == 2


def test_download_rejects_target_for_community(basedir):
    result = CliRunner().invoke(
        main, ['download', '-e', 'CS', '-v', '11.4', '--target', 'rhel-9-x86_64'])
    assert result.exit_code == 2
    assert 'Enterprise only' in result.output


def test_download_non_interactive_stages_a_community_tarball(basedir, monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    monkeypatch.setattr(deployment, '_download',
                        lambda url, dest, timeout=300: Path(dest).write_bytes(b'tar'))
    result = CliRunner().invoke(
        main, ['--json', 'download', '-e', 'CS', '-v', '11.4', '--no-verify'])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload['filename'] == 'mariadb-11.4.13-linux-systemd-x86_64.tar.gz'
    assert payload['verified'] is False
    assert 'url' not in payload


def test_download_wizard_walks_edition_series_release(basedir, monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    monkeypatch.setattr(deployment, '_download',
                        lambda url, dest, timeout=300: Path(dest).write_bytes(b'tar'))
    monkeypatch.setattr(cli_module, '_stdin_is_tty', lambda: True)
    result = CliRunner().invoke(main, ['download', '--no-verify'],
                                input="1\n1\n1\ny\n")
    assert result.exit_code == 0, result.output
    assert '→ 11.4' in result.output
    assert '→ 11.4.13' in result.output
    assert (basedir / 'local' /
            'mariadb-11.4.13-linux-systemd-x86_64.tar.gz').exists()


def test_download_wizard_aborts_without_downloading(basedir, monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    monkeypatch.setattr(deployment, '_download', _never_download)
    monkeypatch.setattr(cli_module, '_stdin_is_tty', lambda: True)
    result = CliRunner().invoke(main, ['download'], input="1\n1\n1\nn\n")
    assert result.exit_code == 0
    assert 'Aborted.' in result.output


def _never_download(url, dest, timeout=300):
    raise AssertionError("should not download")


def test_download_list_emits_json(basedir, monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    result = CliRunner().invoke(main, ['--json', 'download', '--list', '-e', 'CS'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['series'][0]['id'] == '11.4'


def test_download_is_idempotent_when_already_staged(basedir, monkeypatch):
    monkeypatch.setattr(catalog, '_fetch_url', _canned_fetch(CS_ROUTES))
    local = basedir / 'local'
    local.mkdir(parents=True, exist_ok=True)
    (local / 'mariadb-11.4.13-linux-systemd-x86_64.tar.gz').write_bytes(b'tar')
    monkeypatch.setattr(deployment, '_download', _never_download)
    result = CliRunner().invoke(
        main, ['--json', 'download', '-e', 'CS', '-v', '11.4.13', '--no-verify'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['already_staged'] is True
