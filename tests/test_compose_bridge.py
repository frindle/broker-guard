"""The compose half of the SMTP-send-only constraint.

broker_guard/smtp_transport.py holds the code half: no IMAP client exists in
this package, and two AST tests keep it that way. That alone is not the whole
containment. If the Proton Bridge's IMAP port were published onto a network
this container sits on, the only thing standing between broker-guard and a
readable mailbox would be the absence of a few lines of Python -- and "nobody
has written that yet" is not a security boundary.

So the other half is enforced here, against the actual docker-compose.yml:
the Bridge's SMTP port is reachable from broker-guard, its IMAP port is not,
and the difference is structural rather than a matter of what the code
happens to do today.

These tests parse the compose file rather than running `docker compose`,
which keeps them runnable in CI on a host with no Docker and no Bridge.
"""

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"

BRIDGE_SERVICE = "protonmail-bridge"
IMAP_PORTS = {143, 993, 1143}
SMTP_PORTS = {25, 465, 587, 1025}

# The tests below describe a service that is not wired yet. Until it is, they
# skip rather than fail, so the Bridge work does not hold the rest of the
# suite red -- but the moment the service appears, every rule applies.
pytestmark = pytest.mark.usefixtures("_bridge_or_skip")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}


@pytest.fixture
def _bridge_or_skip(compose):
    if BRIDGE_SERVICE not in (compose.get("services") or {}):
        pytest.skip("%s service not wired into docker-compose.yml yet"
                    % BRIDGE_SERVICE)


@pytest.fixture
def bridge(compose):
    return compose["services"][BRIDGE_SERVICE]


def parse_port(entry):
    """Return (host_ip, host_port, container_port) for one `ports:` entry.

    Handles both the short string form ("127.0.0.1:1143:143") and the long
    mapping form, because which one is used is a style choice and the rule
    must not depend on it.
    """
    if isinstance(entry, dict):
        return (str(entry.get("host_ip", "")),
                int(entry.get("published", 0) or 0),
                int(entry.get("target", 0) or 0))
    parts = str(entry).split("/")[0].split(":")
    if len(parts) == 3:
        return parts[0], int(parts[1]), int(parts[2])
    if len(parts) == 2:
        return "", int(parts[0]), int(parts[1])
    return "", 0, int(parts[0])


def loopback(ip):
    return ip in {"127.0.0.1", "::1", "localhost"}


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------

def test_imap_ports_are_bound_to_host_loopback_only(bridge):
    """An IMAP port with no host_ip publishes on 0.0.0.0 -- every interface
    on the box. It must be pinned to loopback so the only thing that can
    reach it is a human on the host."""
    offenders = []
    for entry in bridge.get("ports") or []:
        host_ip, _, target = parse_port(entry)
        if target in IMAP_PORTS and not loopback(host_ip):
            offenders.append(entry)
    assert not offenders, (
        "IMAP port(s) %s are published beyond host loopback. broker-guard is "
        "SMTP-send-only; bind these to 127.0.0.1 (see the header of "
        "broker_guard/smtp_transport.py)." % offenders)


def test_bridge_shares_no_network_with_broker_guard_unless_smtp_only(compose,
                                                                    bridge):
    """If the two containers share a network, every published-or-not port on
    the Bridge is reachable by service name, loopback binding included --
    host port bindings do not apply to container-to-container traffic. So on
    a shared network the Bridge must expose SMTP and nothing else."""
    bg = compose["services"]["broker-guard"]
    shared = set(_networks(bg)) & set(_networks(bridge))
    if not shared:
        pytest.skip("no shared network; reachability is decided by host ports")

    exposed = {int(str(p).split("/")[0]) for p in (bridge.get("expose") or [])}
    for entry in bridge.get("ports") or []:
        exposed.add(parse_port(entry)[2])

    leaked = exposed & IMAP_PORTS
    assert not leaked, (
        "Bridge exposes IMAP port(s) %s on network(s) %s that broker-guard is "
        "attached to. A loopback HOST binding does not help here: host port "
        "bindings govern traffic arriving from the host, not traffic between "
        "containers, so on a shared network broker-guard reaches the Bridge "
        "as %s:143 directly.\n\n"
        "This is not a bug in the test -- the Bridge process listens on IMAP "
        "inside its own container no matter how the ports are published, so "
        "ANY shared network defeats the containment. Give the two containers "
        "no common network, publish SMTP on the docker gateway address "
        "(e.g. 172.17.0.1:1025:25) and IMAP on 127.0.0.1:1143:143, and let "
        "broker-guard reach SMTP via host-gateway. Then the IMAP port has no "
        "route from any container at all."
        % (sorted(leaked), sorted(shared), BRIDGE_SERVICE))


def test_smtp_is_actually_reachable(compose, bridge):
    """The flip side: the constraint is 'no IMAP', not 'no mail'. If SMTP is
    not reachable either, the wiring is broken rather than safe."""
    bg = compose["services"]["broker-guard"]
    shared = set(_networks(bg)) & set(_networks(bridge))
    exposed = {int(str(p).split("/")[0]) for p in (bridge.get("expose") or [])}
    host_bound = set()
    for entry in bridge.get("ports") or []:
        host_ip, _, target = parse_port(entry)
        exposed.add(target)
        if loopback(host_ip) or not host_ip:
            host_bound.add(target)
    assert (exposed & SMTP_PORTS), (
        "Bridge exposes no SMTP port; the send path has nothing to connect to")
    gateway_bound = set()
    for entry in bridge.get("ports") or []:
        host_ip, _, target = parse_port(entry)
        if host_ip and not loopback(host_ip):
            gateway_bound.add(target)
    assert shared or (gateway_bound & SMTP_PORTS) or (
            host_bound & SMTP_PORTS and _has_host_gateway(bg)), (
        "Bridge shares no network with broker-guard and its SMTP port is not "
        "reachable from a container: publish SMTP on the docker gateway "
        "address, or give broker-guard an extra_hosts host-gateway entry")


def _has_host_gateway(service):
    entries = service.get("extra_hosts") or []
    if isinstance(entries, dict):
        entries = ["%s:%s" % kv for kv in entries.items()]
    return any("host-gateway" in str(e) for e in entries)


def test_bridge_is_opt_in_via_a_compose_profile(bridge):
    """Without a profile the Bridge starts on every `docker compose up`,
    including on hosts that have never been logged in, where it sits there
    as an unconfigured mail relay."""
    assert bridge.get("profiles"), (
        "%s has no `profiles:`; it must be opt-in" % BRIDGE_SERVICE)


def test_bridge_has_a_persistent_volume(bridge):
    """The Bridge login is a manual, interactive, one-time step only Penn can
    perform. Without persistence it is a manual step on every restart."""
    assert bridge.get("volumes"), (
        "%s has no volume; its vault and keyring would not survive a restart "
        "and the manual login would have to be repeated" % BRIDGE_SERVICE)


def test_no_proton_credentials_are_literals_in_the_compose_file():
    """Same rule as BG_CRYPTO_KEY: secrets come from the gitignored .env by
    interpolation, never inline, because this file is tracked."""
    import re
    text = COMPOSE.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^\s*(BG_OPTOUT_EMAIL_SMTP_PASSWORD|BG_OPTOUT_EMAIL_SMTP_USERNAME)"
        r"\s*:\s*(.+)$", re.M)
    for name, value in pattern.findall(text):
        value = value.strip().strip('"').strip("'")
        assert not value or value.startswith("${"), (
            "%s has a literal value in the tracked compose file; use "
            "${%s:-} and put the real value in .env" % (name, name))


def _networks(service):
    """Network names a service is attached to, in either compose form."""
    nets = service.get("networks")
    if nets is None:
        return []
    if isinstance(nets, dict):
        return list(nets)
    return list(nets)
