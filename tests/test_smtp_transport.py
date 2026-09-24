"""Tests for broker_guard.smtp_transport.

Everything here runs against a recording fake, never a socket: the point is
to pin the transport's CONTRACT (what it calls, in what order, what it
refuses) without needing a Bridge, which is the one piece only Penn can set
up. A test that needed the real Bridge could not run in CI and so would not
catch anything before deploy.
"""

import ssl

import pytest

from broker_guard import smtp_transport as st
from broker_guard.smtp_transport import (SMTPTransport, SMTPTransportError,
                                         is_container_local,
                                         transport_from_config)
from email.message import EmailMessage


class FakeSMTP:
    """Records the call sequence an SMTPTransport makes."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        self.sent = []
        self.tls_context = None
        self.quit_raises = False
        self.login_raises = False
        self.send_raises = False
        FakeSMTP.instances.append(self)

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.tls_context = context

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.login_raises:
            raise RuntimeError("auth failed")

    def send_message(self, message):
        self.calls.append("send_message")
        if self.send_raises:
            raise RuntimeError("550 rejected")
        self.sent.append(message)

    def quit(self):
        self.calls.append("quit")
        if self.quit_raises:
            raise RuntimeError("connection reset on quit")


@pytest.fixture(autouse=True)
def _clear_instances():
    FakeSMTP.instances = []
    yield
    FakeSMTP.instances = []


def make_message(to="privacy@broker.com", frm="data@example.com"):
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = frm
    msg["Subject"] = "Request to delete my personal information"
    msg.set_content("Please delete my personal information.")
    return msg


def make_transport(**kw):
    kw.setdefault("host", "127.0.0.1")
    kw.setdefault("port", 1025)
    kw.setdefault("smtp_factory", FakeSMTP)
    return SMTPTransport(**kw)


# --------------------------------------------------------------------------
# The constraint this module exists to hold
# --------------------------------------------------------------------------

_BANNED_MODULES = {"imaplib", "poplib", "imapclient", "imaplib2"}


def _imported_modules(source):
    """Top-level module names this source imports, via AST.

    Deliberately AST and not a substring scan: the module header talks about
    imaplib at length to explain why it is absent, and a grep-style test that
    the documentation itself trips is a test nobody keeps.
    """
    import ast
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
        elif (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            names.add(node.args[0].value.split(".")[0])
    return names


def test_module_imports_no_imap_or_pop_client():
    """SMTP-send-only is a design constraint, so it gets a test, not just a
    comment. A comment does not fail CI when someone adds imaplib."""
    import inspect
    found = _imported_modules(inspect.getsource(st)) & _BANNED_MODULES
    assert not found, (
        "%s imported by smtp_transport; broker-guard is SMTP-send-only and "
        "the mailbox is deliberately unreadable from this repo (see the "
        "module header before changing this)" % sorted(found))


def test_no_imap_client_anywhere_in_the_package():
    """The constraint is package-wide, not module-local."""
    import pathlib
    import broker_guard
    root = pathlib.Path(broker_guard.__file__).parent
    offenders = {}
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8", errors="replace")
        found = _imported_modules(source) & _BANNED_MODULES
        if found:
            offenders[path.name] = sorted(found)
    assert not offenders, (
        "IMAP/POP client found in %s -- broker-guard is send-only" % offenders)


# --------------------------------------------------------------------------
# Construction refuses bad wiring before it can connect
# --------------------------------------------------------------------------

def test_empty_host_refused():
    with pytest.raises(SMTPTransportError, match="no SMTP host"):
        SMTPTransport(host="", smtp_factory=FakeSMTP)


def test_whitespace_only_host_refused():
    with pytest.raises(SMTPTransportError, match="no SMTP host"):
        SMTPTransport(host="   ", smtp_factory=FakeSMTP)


@pytest.mark.parametrize("port", [0, -1, 65536, 99999])
def test_out_of_range_port_refused(port):
    with pytest.raises(SMTPTransportError, match="out of range"):
        make_transport(port=port)


def test_password_without_username_refused():
    with pytest.raises(SMTPTransportError, match="without a username"):
        make_transport(password="bridge-secret")


def test_construction_opens_no_connection():
    make_transport(username="u", password="p")
    assert FakeSMTP.instances == []


# --------------------------------------------------------------------------
# insecure_tls is scoped to container-local hosts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1",
                                  "LOCALHOST", " 127.0.0.1 ",
                                  "host.docker.internal"])
def test_container_local_hosts_may_waive_verification(host):
    assert is_container_local(host)
    t = make_transport(host=host, insecure_tls=True)
    ctx = t._ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


@pytest.mark.parametrize("host", [
    "smtp.proton.me",
    "protonmail-bridge",          # compose service name: still a wire
    "10.0.0.5",
    "192.168.1.20",
    "127.0.0.1.evil.net",         # suffix trick must not pass
])
def test_remote_hosts_may_not_waive_verification(host):
    assert not is_container_local(host)
    with pytest.raises(SMTPTransportError, match="container-local"):
        make_transport(host=host, insecure_tls=True)


def test_remote_host_is_fine_with_verification_on():
    t = make_transport(host="smtp.proton.me", port=587, insecure_tls=False)
    ctx = t._ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


# --------------------------------------------------------------------------
# ca_file -- the intended production answer to the self-signed certificate
# --------------------------------------------------------------------------

def _ca_file(tmp_path):
    """A real, parseable CA certificate: ssl rejects a dummy file, so a
    placeholder here would test the error path by accident."""
    import ssl as _ssl
    try:
        import certifi
        src = certifi.where()
    except ImportError:
        src = _ssl.get_default_verify_paths().cafile
    if not src:
        pytest.skip("no system CA bundle to copy")
    path = tmp_path / "bridge-ca.pem"
    path.write_bytes(open(src, "rb").read())
    return str(path)


def test_ca_file_keeps_verification_on(tmp_path):
    ca = _ca_file(tmp_path)
    t = make_transport(host="protonmail-bridge", ca_file=ca)
    ctx = t._ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_ca_file_works_for_a_remote_host(tmp_path):
    """This is the whole point: a verified cert lets the Bridge live on the
    compose network instead of forcing loopback."""
    t = make_transport(host="protonmail-bridge", port=25,
                       ca_file=_ca_file(tmp_path))
    t(make_message())
    assert FakeSMTP.instances[0].host == "protonmail-bridge"


def test_missing_ca_file_refused_at_construction(tmp_path):
    with pytest.raises(SMTPTransportError, match="does not exist"):
        make_transport(ca_file=str(tmp_path / "nope.pem"))
    assert FakeSMTP.instances == []


def test_ca_file_and_insecure_tls_together_refused(tmp_path):
    with pytest.raises(SMTPTransportError, match="opposite"):
        make_transport(ca_file=_ca_file(tmp_path), insecure_tls=True)


def test_ca_file_from_config(tmp_path):
    ca = _ca_file(tmp_path)
    t = transport_from_config(
        Cfg(host="protonmail-bridge", ca_file=ca), smtp_factory=FakeSMTP)
    assert t.ca_file == ca
    assert t._ssl_context().verify_mode == ssl.CERT_REQUIRED


def test_default_context_verifies():
    ctx = make_transport()._ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED


# --------------------------------------------------------------------------
# The send sequence
# --------------------------------------------------------------------------

def test_full_bridge_sequence():
    t = make_transport(username="alias@pm.me", password="bridge-pw",
                       starttls=True)
    msg = make_message()
    t(msg)
    fake = FakeSMTP.instances[0]
    assert fake.host == "127.0.0.1" and fake.port == 1025
    assert fake.timeout == 30
    assert fake.calls == [
        "ehlo", "starttls", "ehlo",
        ("login", "alias@pm.me", "bridge-pw"),
        "send_message", "quit",
    ]
    assert fake.sent == [msg]


def test_starttls_disabled_skips_tls_but_still_sends():
    t = make_transport(starttls=False)
    t(make_message())
    assert FakeSMTP.instances[0].calls == ["ehlo", "send_message", "quit"]


def test_no_username_means_no_login():
    t = make_transport()
    t(make_message())
    assert not any(isinstance(c, tuple) and c[0] == "login"
                   for c in FakeSMTP.instances[0].calls)


def test_starttls_receives_the_transports_context():
    t = make_transport(insecure_tls=True)
    t(make_message())
    ctx = FakeSMTP.instances[0].tls_context
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_NONE


def test_timeout_is_passed_through():
    make_transport(timeout_s=7)(make_message())
    assert FakeSMTP.instances[0].timeout == 7


def test_each_call_opens_a_fresh_connection():
    t = make_transport()
    t(make_message())
    t(make_message())
    assert len(FakeSMTP.instances) == 2
    assert all(f.calls[-1] == "quit" for f in FakeSMTP.instances)


# --------------------------------------------------------------------------
# Failure handling -- a false "failed" invites a duplicate send
# --------------------------------------------------------------------------

def test_quit_failure_after_successful_send_is_swallowed():
    t = make_transport()
    msg = make_message()

    class QuitBreaks(FakeSMTP):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.quit_raises = True

    t._smtp_factory = QuitBreaks
    t(msg)                       # must not raise: the message really went out
    assert FakeSMTP.instances[0].sent == [msg]


def test_send_failure_propagates():
    """send_request records OUTCOME_FAILED from the raised exception, so the
    transport must not swallow a genuine send failure."""
    t = make_transport()

    class SendBreaks(FakeSMTP):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.send_raises = True

    t._smtp_factory = SendBreaks
    with pytest.raises(RuntimeError, match="550 rejected"):
        t(make_message())
    assert "quit" in FakeSMTP.instances[0].calls   # connection still closed


def test_login_failure_propagates_and_closes():
    t = make_transport(username="u", password="p")

    class LoginBreaks(FakeSMTP):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.login_raises = True

    t._smtp_factory = LoginBreaks
    with pytest.raises(RuntimeError, match="auth failed"):
        t(make_message())
    assert "quit" in FakeSMTP.instances[0].calls
    assert "send_message" not in FakeSMTP.instances[0].calls


# --------------------------------------------------------------------------
# The transport takes finished messages only
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["a string", 42, None, {"to": "x"}])
def test_non_message_refused(bad):
    with pytest.raises(SMTPTransportError, match="finished EmailMessage"):
        make_transport()(bad)


def test_message_without_recipient_refused():
    msg = EmailMessage()
    msg["From"] = "data@example.com"
    msg.set_content("x")
    with pytest.raises(SMTPTransportError, match="missing From or To"):
        make_transport()(msg)
    assert FakeSMTP.instances == []


def test_message_without_sender_refused():
    msg = EmailMessage()
    msg["To"] = "privacy@broker.com"
    msg.set_content("x")
    with pytest.raises(SMTPTransportError, match="missing From or To"):
        make_transport()(msg)


# --------------------------------------------------------------------------
# transport_from_config
# --------------------------------------------------------------------------

class Cfg:
    def __init__(self, **kw):
        self.optout_email_smtp_host = kw.get("host", "")
        self.optout_email_smtp_port = kw.get("port", 25)
        self.optout_email_smtp_username = kw.get("username", "")
        self.optout_email_smtp_password = kw.get("password", "")
        self.optout_email_smtp_starttls = kw.get("starttls", True)
        self.optout_email_smtp_ca_file = kw.get("ca_file", "")
        self.optout_email_smtp_insecure_tls = kw.get("insecure_tls", False)
        self.optout_email_smtp_timeout_s = kw.get("timeout_s", 30)


def test_unconfigured_config_yields_no_transport():
    assert transport_from_config(Cfg()) is None


def test_config_builds_a_transport():
    t = transport_from_config(
        Cfg(host="127.0.0.1", port=1025, username="u", password="p",
            insecure_tls=True),
        smtp_factory=FakeSMTP)
    assert isinstance(t, SMTPTransport)
    assert (t.host, t.port, t.username, t.insecure_tls) == (
        "127.0.0.1", 1025, "u", True)


def test_config_with_remote_host_and_insecure_tls_still_refused():
    """The guard cannot be reached around by going through config."""
    with pytest.raises(SMTPTransportError, match="container-local"):
        transport_from_config(
            Cfg(host="smtp.proton.me", insecure_tls=True),
            smtp_factory=FakeSMTP)


def test_real_config_defaults_ship_unconfigured():
    """Shipping defaults must not produce a live transport."""
    from broker_guard.config import Config
    cfg = Config()
    assert cfg.optout_email_smtp_host == ""
    assert cfg.optout_email_smtp_insecure_tls is False
    assert cfg.optout_email_smtp_ca_file == ""
    assert transport_from_config(cfg) is None


def test_env_wiring_round_trips(monkeypatch):
    from broker_guard import config as config_mod
    env = {
        "BG_OPTOUT_EMAIL_SMTP_HOST": "127.0.0.1",
        "BG_OPTOUT_EMAIL_SMTP_PORT": "1025",
        "BG_OPTOUT_EMAIL_SMTP_USERNAME": "alias@pm.me",
        "BG_OPTOUT_EMAIL_SMTP_PASSWORD": "bridge-pw",
        "BG_OPTOUT_EMAIL_SMTP_INSECURE_TLS": "true",
        "BG_OPTOUT_EMAIL_SMTP_TIMEOUT_S": "12",
        "BG_OPTOUT_EMAIL_FROM": "data@penndalton.com",
    }
    cfg = config_mod.load_config(env)
    assert cfg.optout_email_smtp_host == "127.0.0.1"
    assert cfg.optout_email_smtp_port == 1025
    assert cfg.optout_email_smtp_username == "alias@pm.me"
    assert cfg.optout_email_smtp_insecure_tls is True
    assert cfg.optout_email_smtp_timeout_s == 12
    assert cfg.optout_email_from == "data@penndalton.com"
    # and the two send gates are untouched by any of the above
    assert cfg.optout_email_enabled is False
    assert cfg.optout_email_dry_run is True


# --------------------------------------------------------------------------
# Integration with optout_email's gates -- the transport must not bypass them
# --------------------------------------------------------------------------

def test_transport_is_never_consulted_while_dry_run(tmp_path):
    """The dry-run branch returns before any transport call. If that ever
    stops being true, a "draft" run would really send."""
    from broker_guard import optout_email
    from broker_guard.config import Config

    calls = []
    cfg = Config(optout_email_enabled=True, optout_email_dry_run=True,
                 optout_email_from="data@example.com")
    identity = _identity()
    optout_email.send_request(
        "acme-com", "acme.com", "privacy@acme.com", identity, cfg,
        transport=lambda m: calls.append(m), directory=tmp_path)
    assert calls == []


def test_ineligible_address_never_reaches_the_transport(tmp_path):
    from broker_guard import optout_email
    from broker_guard.config import Config

    calls = []
    cfg = Config(optout_email_enabled=True, optout_email_dry_run=False,
                 optout_email_from="data@example.com")
    with pytest.raises(optout_email.EmailRefused):
        optout_email.send_request(
            "acme-com", "acme.com", "tapster13@gmail.com", _identity(), cfg,
            transport=lambda m: calls.append(m), directory=tmp_path)
    assert calls == []


def _identity():
    return _TestIdentity()


class _TestIdentity:
    identity_key = "test-identity"
    full_name = "Test Person"
    first_name = "Test"
    last_name = "Person"
    email = "test@example.com"
    phone = "555-0100"
    address = "1 Test St"
    street = "1 Test St"
    city = "Testville"
    state = "Nevada"
    state_code = "NV"
    zip = "89000"
    postal_code = "89000"
    country = "United States"
