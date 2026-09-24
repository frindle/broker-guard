"""SMTP transport for ``optout_email.send_request``.

==========================================================================
THIS CODEBASE IS SMTP-SEND-ONLY. IMAP IS DELIBERATELY ABSENT.
==========================================================================

There is no IMAP or POP client in broker-guard, no setting for one, and no
feature that needs one. That is a design constraint with the same weight as
the eligibility gate in ``optout_email``, not an unfinished corner:

  * Opt-out requests are fire-and-forget. Nothing in the pipeline reads a
    reply; broker responses are read by a human in their own mail client.
    Adding a reader would buy no capability.
  * The account behind this transport is a real Proton mailbox. An SMTP
    credential can send mail as that alias. An IMAP credential can read the
    entire mailbox. Those are very different blast radii for a process whose
    whole job is talking to hostile third parties, and this one only ever
    needs the first.
  * The containment is layered on purpose. This module is the code half. The
    other half is deployment-side: the Bridge runs as its own container,
    installed from the Unraid Community Apps template, with its SMTP host
    port published and its IMAP host port left blank, on a different docker
    network from this one. broker-guard reaches SMTP through the published
    host port; IMAP has no published port to reach.

    READ THIS BEFORE CHANGING ANY NETWORK SETTING. An unpublished port is
    NOT an unreachable one. Publishing governs access from the HOST; it has
    no bearing on container-to-container traffic. The Bridge listens on 143
    inside its container either way, so any container sharing a network with
    it reaches IMAP directly at its container IP. The only reason
    broker-guard cannot is that Compose puts it on a private per-project
    network. `network_mode: bridge` -- offered, commented out, one line away
    in docker-compose.yml -- would end that, and nothing about such a diff
    would look like a security change. tests/test_compose_bridge.py exists
    to fail if it happens.

If you are here to add "just a small reply-checker", that is the change this
comment exists to interrupt. It needs a deliberate decision by the operator,
a reason that is not covered above, and the compose-side port binding
changed to match -- not a quiet import of ``imaplib``.

WHAT THIS MODULE IS
-------------------
A callable that takes one ``EmailMessage`` and sends it. That is the entire
contract ``send_request`` expects of a transport. It opens a connection per
message and closes it: this sends a handful of messages a day at most, so
connection reuse would add reconnect/timeout state for no benefit.

It does not decide *whether* to send. Every gate -- the two config flags, the
eligibility check, the dry-run branch -- lives in ``optout_email`` and runs
before a transport is ever consulted. A transport that could bypass those
would defeat them, so this one is given a finished message or nothing.

PROTON MAIL BRIDGE
------------------
The intended deployment is the Bridge container on the same host, reached at
its SMTP port with STARTTLS and the Bridge-generated username/password (which
are NOT the Proton account password). The Bridge presents a SELF-SIGNED
certificate, which the default trust store rejects.

There are two ways to deal with that, and the ORDER matters:

  1. ``ca_file`` -- point at the CA certificate the Bridge exports, mounted
     read-only into this container. Verification stays fully ON, including
     hostname checking. This is the intended production wiring, and it is
     what makes it safe for the Bridge to sit on a shared compose network
     rather than on loopback.
  2. ``insecure_tls`` -- turn verification off. Only for a bench test before
     a certificate has been exported, so it is honoured ONLY when the host is
     a loopback/container-local address. Pointed at anything reachable over a
     network it is refused outright, which stops the bench-test setting from
     following the config into a deployment where it would mean a
     man-in-the-middle can read the credential and the request.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)

__all__ = ["SMTPTransport", "SMTPTransportError", "transport_from_config",
           "is_container_local"]


class SMTPTransportError(RuntimeError):
    """Raised for a misconfigured transport, before any connection is made."""


# Hosts where a self-signed certificate is acceptable, because the traffic
# never leaves the host. Compose service names are NOT in here: they resolve
# over a shared bridge network, which is a wire, even if a short one.
_LOCAL_HOSTS = frozenset({
    "127.0.0.1", "::1", "localhost", "localhost.localdomain",
    "host.docker.internal",
})


def is_container_local(host: str) -> bool:
    """True when *host* is a loopback address this container owns.

    Used to decide whether ``insecure_tls`` may be honoured. Deliberately a
    small allow-list rather than a pattern: 127.x.x.x as a range is wider
    than anything the Bridge wiring needs.
    """
    return (host or "").strip().lower() in _LOCAL_HOSTS


class SMTPTransport:
    """Send one ``EmailMessage`` per call over SMTP.

    Parameters mirror the ``optout_email_smtp_*`` settings on ``Config``.
    Construction validates; nothing connects until the instance is called.
    """

    def __init__(self, host: str, port: int = 25, username: str = "",
                 password: str = "", starttls: bool = True,
                 ca_file: str = "", insecure_tls: bool = False,
                 timeout_s: int = 30, smtp_factory=None):
        host = (host or "").strip()
        if not host:
            raise SMTPTransportError(
                "no SMTP host configured (BG_OPTOUT_EMAIL_SMTP_HOST); "
                "refusing to build a transport that cannot send")
        if not (0 < int(port) < 65536):
            raise SMTPTransportError("SMTP port %r is out of range" % (port,))
        if bool(password) and not username:
            raise SMTPTransportError(
                "SMTP password set without a username; Proton Bridge issues "
                "both together, so this is almost certainly a wiring mistake")
        ca_file = (ca_file or "").strip()
        if ca_file and insecure_tls:
            # Not merely redundant: it reads as "we exported a certificate"
            # while actually verifying nothing. Refuse rather than silently
            # pick one, so the operator states which they meant.
            raise SMTPTransportError(
                "ca_file and insecure_tls are both set; these are opposite "
                "answers to the same question. Drop insecure_tls to verify "
                "against the exported certificate.")
        if ca_file and not os.path.exists(ca_file):
            # Loud now, at wiring time. A missing CA discovered mid-send
            # would surface as an opaque TLS error on a batch that has
            # already partly gone out.
            raise SMTPTransportError(
                "SMTP CA file %r does not exist; check the read-only mount "
                "of the Bridge certificate" % (ca_file,))
        if insecure_tls and not is_container_local(host):
            # The whole justification for waiving verification is that the
            # traffic stays on the host. Against a remote host it is a
            # man-in-the-middle away from leaking the credential and the
            # request contents, so this is refused rather than warned about.
            raise SMTPTransportError(
                "insecure_tls is only permitted for a container-local host "
                "(got %r). Point this at the Bridge on loopback, or supply a "
                "certificate the system trusts." % (host,))

        self.host = host
        self.port = int(port)
        self.username = username or ""
        self.password = password or ""
        self.starttls = bool(starttls)
        self.ca_file = ca_file
        self.insecure_tls = bool(insecure_tls)
        self.timeout_s = int(timeout_s)
        # Injected in tests; production uses smtplib.SMTP unchanged.
        self._smtp_factory = smtp_factory or smtplib.SMTP

    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=self.ca_file or None)
        if self.insecure_tls:
            # Reachable only for a container-local host (enforced above).
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def __call__(self, message: EmailMessage) -> None:
        if not isinstance(message, EmailMessage):
            raise SMTPTransportError(
                "transport expects a finished EmailMessage, got %s"
                % type(message).__name__)
        if not message.get("To") or not message.get("From"):
            # send_message would raise later anyway; failing here keeps the
            # error attributable to the message rather than to the server.
            raise SMTPTransportError(
                "message is missing From or To; optout_email builds both, so "
                "an incomplete message here means it was not built there")

        smtp = self._smtp_factory(self.host, self.port, timeout=self.timeout_s)
        try:
            smtp.ehlo()
            if self.starttls:
                smtp.starttls(context=self._ssl_context())
                smtp.ehlo()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)
        finally:
            try:
                smtp.quit()
            except Exception:                      # noqa: BLE001
                # A failed QUIT after a successful send must not turn a sent
                # message into a recorded failure -- email cannot be recalled,
                # so a false "failed" would invite a duplicate send.
                log.debug("SMTP quit failed for %s:%s", self.host, self.port,
                          exc_info=True)

    def __repr__(self) -> str:                     # pragma: no cover - debug
        return "<SMTPTransport %s:%s starttls=%s auth=%s verify=%s>" % (
            self.host, self.port, self.starttls, bool(self.username),
            "off" if self.insecure_tls else (self.ca_file or "system"))


def transport_from_config(cfg, smtp_factory=None):
    """Build a transport from ``Config``, or return None if none is wired.

    Returning None rather than raising is what lets the module stay importable
    and the tests stay runnable on a host with no Bridge: ``send_request``
    already refuses a live send when handed no transport, so an unconfigured
    deployment fails at the point of sending with a clear message instead of
    at import.
    """
    host = (getattr(cfg, "optout_email_smtp_host", "") or "").strip()
    if not host:
        return None
    return SMTPTransport(
        host=host,
        port=getattr(cfg, "optout_email_smtp_port", 25),
        username=getattr(cfg, "optout_email_smtp_username", ""),
        password=getattr(cfg, "optout_email_smtp_password", ""),
        starttls=getattr(cfg, "optout_email_smtp_starttls", True),
        ca_file=getattr(cfg, "optout_email_smtp_ca_file", ""),
        insecure_tls=getattr(cfg, "optout_email_smtp_insecure_tls", False),
        timeout_s=getattr(cfg, "optout_email_smtp_timeout_s", 30),
        smtp_factory=smtp_factory,
    )
