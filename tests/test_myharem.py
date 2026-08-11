"""Unit tests for myharem's pure logic and deploy orchestration.

These run without a real MariaDB tarball or root: the tar-extract, DB-init,
process start, and SQL steps are stubbed, so we validate port math, the
structured results, manifest recording, and rollback — not a live server.
"""

import getpass
import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from mh import config, deployment, galera, manifest, model, replication, service
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
                        lambda inst, retries=5: None)
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
