"""The compose half of the SMTP-send-only constraint.

broker_guard/smtp_transport.py holds the code half: no IMAP client exists in
this package, and two AST tests keep it that way.

The other half used to be a Bridge service in this file. It is not any more --
the Bridge is installed from the Unraid Community Apps template, as its own
container outside this compose project, with its SMTP host port published and
its IMAP host port left blank. So there is no `protonmail-bridge` service here
to make assertions about.

That does NOT leave nothing to test, and the reason is the thing that is easy
to get wrong about Docker:

    PUBLISHING A PORT GOVERNS ACCESS FROM THE HOST. IT HAS NO BEARING ON
    WHETHER ANOTHER CONTAINER CAN REACH THAT PORT.

The Bridge process listens on 143 inside its own container whether or not a
host port is mapped to it. "Leave the IMAP host-port field blank" means the
host cannot reach IMAP. It does not mean a *container* cannot: any container
on the same docker network reaches it directly at the Bridge's container IP.

What actually keeps broker-guard away from it is network separation. The CA
template puts the Bridge on Docker's default bridge (docker0). This compose
project declares no `networks:` and no `network_mode:`, so Compose creates a
private per-project network, and Docker's inter-network isolation rules mean
the two cannot talk at all -- broker-guard reaches SMTP through the published
host port instead, which is exactly the asymmetry we want, since IMAP has no
published host port to reach.

That separation is one uncommented line away from gone. Line 20 of
docker-compose.yml offers `network_mode: bridge`, described as the default,
and uncommenting it would put broker-guard on docker0 alongside the Bridge --
at which point the unpublished IMAP port becomes reachable by container IP and
the containment is over, silently, with nothing in the diff that looks like a
security change.

So that is what these tests guard: not a service block that no longer exists,
but the network placement that the whole arrangement now rests on.
"""

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

COMPOSE = pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"

# Network modes that would put this container on Docker's default bridge or
# on the host's own stack -- either way, sharing a network with the Bridge
# container that the Unraid CA template installs.
UNSAFE_NETWORK_MODES = {"bridge", "host", "default"}


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}


@pytest.fixture
def broker_guard(compose):
    return compose["services"]["broker-guard"]


def test_broker_guard_is_not_on_the_default_docker_bridge(broker_guard):
    """The one line that would undo the IMAP containment."""
    mode = str(broker_guard.get("network_mode", "") or "").strip()
    assert mode.lower() not in UNSAFE_NETWORK_MODES, (
        "broker-guard sets network_mode: %s, which puts it on the same docker "
        "network as the Proton Bridge container installed from the Unraid CA "
        "template. The Bridge listens on IMAP:143 inside its container "
        "regardless of whether a host port is published, so from there "
        "broker-guard can reach it directly at the Bridge's container IP and "
        "the send-only containment is gone.\n\n"
        "Leaving the IMAP host-port field blank in the CA template protects "
        "the HOST, not other containers. What protects broker-guard is that "
        "it sits on its own Compose-created network. Keep it there: reach "
        "SMTP through the published host port instead." % mode)


def test_broker_guard_declares_no_shared_external_network(broker_guard):
    """A deliberately attached external/default network is the other route
    to the same place -- and unlike network_mode it can be added without
    touching the commented-out line the test above watches."""
    nets = broker_guard.get("networks")
    if not nets:
        return
    names = list(nets) if isinstance(nets, (dict, list)) else [nets]
    offenders = [n for n in names if str(n).lower() in {"bridge", "default"}]
    assert not offenders, (
        "broker-guard attaches to %s. See the header of this file: sharing a "
        "network with the Bridge container exposes its unpublished IMAP port."
        % offenders)


def test_no_bridge_service_was_reintroduced_here(compose):
    """If someone later does add the Bridge to this compose project, every
    rule in the deleted version of this file becomes relevant again, and none
    of them is being checked any more. Fail loudly rather than let it land
    under the old file's reassuring name."""
    assert "protonmail-bridge" not in (compose.get("services") or {}), (
        "A protonmail-bridge service has been added to this compose project. "
        "The Bridge is supposed to be installed from the Unraid CA template "
        "as a separate container, and these tests no longer check the port "
        "and network rules that an in-project Bridge service would need "
        "(SMTP reachable, IMAP on loopback only, no shared network, opt-in "
        "profile, persistent volume). Either revert this, or restore those "
        "checks -- see git history for tests/test_compose_bridge.py.")


def test_no_proton_credentials_are_literals_in_the_compose_file():
    """Same rule as BG_CRYPTO_KEY: secrets come from the gitignored .env by
    interpolation, never inline, because this file is tracked."""
    import re
    text = COMPOSE.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^\s*(BG_OPTOUT_EMAIL_SMTP_PASSWORD|BG_OPTOUT_EMAIL_SMTP_USERNAME"
        r"|BG_OPTOUT_EMAIL_FROM)\s*:\s*(.+)$", re.M)
    for name, value in pattern.findall(text):
        value = value.strip().strip('"').strip("'")
        assert not value or value.startswith("${"), (
            "%s has a literal value in the tracked compose file; use "
            "${%s:-} and put the real value in .env" % (name, name))
