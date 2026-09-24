"""The email opt-out channel: eligibility, drafting, and the send interlocks.

NOTHING here sends mail. ``send_request`` is driven with a fake transport that
records the message, and the default every test starts from is
enabled=False/dry_run=True -- the state a fresh Config is in.

The eligibility tests carry real addresses observed in the dataset during the
mapping sweep, because the point of the check is to reject the ones that are
actually there. ``tapster13@gmail.com`` is a broker's real recorded opt-out
channel; if a future change makes that address pass, one of these tests fails
before Penn's name, postal address and phone reach a stranger's inbox.

All identity data is FAKE -- see tests/conftest.py.
"""
import pytest

from broker_guard import optout_email, review
from broker_guard.config import Config
from broker_guard.profile import Identity
from conftest import FAKE_EMAIL, FAKE_FIRST, FAKE_LAST


@pytest.fixture
def identity():
    return Identity(
        first_name=FAKE_FIRST,
        last_name=FAKE_LAST,
        emails=[FAKE_EMAIL],
        addresses=["742 Evergreen Terrace, Springfield, IL 62704"],
    )


@pytest.fixture
def cfg(tmp_path):
    return Config(review_dir=str(tmp_path / "review"))


class Transport:
    """A stand-in for anything that could really send."""

    def __init__(self, explode=False):
        self.sent = []
        self.explode = explode

    def __call__(self, message):
        if self.explode:
            raise OSError("connection refused")
        self.sent.append(message)


# ---------------------------------------------------------------- eligibility

@pytest.mark.parametrize("domain,email", [
    ("idm.us.com", "ccpa@idm.us.com"),
    ("giantpartners.com", "privacy@giantpartners.com"),
    ("homeownersmarketingservices.com", "dnm@homeownersmarketingservices.com"),
    ("freewheel.com", "legalnotices@freewheel.com"),
    ("broker.com", "privacy@mail.broker.com"),          # subdomain is fine
    ("broker.com", "privacy.requests@broker.com"),      # dotted role tokens
    ("www.Broker.com ", " PRIVACY@broker.com"),         # normalisation
])
def test_eligible_addresses_pass(domain, email):
    ok, reason = optout_email.eligibility(domain, email)
    assert ok, reason
    assert reason == "ok"


@pytest.mark.parametrize("domain,email,expected", [
    # The real one. A broker's statutory opt-out channel is a gmail account.
    ("uspeoplesearch.net", "tapster13@gmail.com", "offdomain"),
    # Right domain, but a named human being.
    ("gumgum.com", "talbert@gumgum.com", "personal"),
    ("giantpartners.com", "nikki@giantpartners.com", "personal"),
    ("lighthouselist.com", "robert@lighthouselist.com", "personal"),
    # Role address, wrong company -- the acquisition cases.
    ("force.com", "privacy@erepublic.com", "offdomain"),
    ("heartbeat.ai", "contact@swordfish.ai", "offdomain"),
    # A helpdesk is not a designated privacy channel.
    ("ileads.com", "inquiries@ileads.com", "ambiguous_localpart"),
    ("fushiamedia.com", "support@fushiamedia.com", "ambiguous_localpart"),
    # Nothing recorded at all.
    ("idengine.com", "", "no_address"),
    ("idengine.com", None, "no_address"),
    # Not parseable.
    ("broker.com", "privacy-at-broker.com", "malformed"),   # no '@' at all
    ("broker.com", "a@b@broker.com", "malformed"),
    ("broker.com", "privacy@broker.com extra", "malformed"),
])
def test_ineligible_addresses_are_rejected_with_a_reason(domain, email, expected):
    ok, reason = optout_email.eligibility(domain, email)
    assert not ok
    assert reason == expected


def test_lookalike_domain_is_not_accepted_by_substring():
    """``broker.com.evil.net`` contains the broker's domain and is not it."""
    assert not optout_email.domain_matches("broker.com", "broker.com.evil.net")
    ok, reason = optout_email.eligibility("broker.com", "privacy@broker.com.evil.net")
    assert not ok and reason == "offdomain"


def test_role_word_does_not_launder_a_personal_localpart():
    assert not optout_email.localpart_is_role("privacy.tapster13")
    ok, reason = optout_email.eligibility("broker.com", "privacy.tapster13@broker.com")
    assert not ok and reason == "personal"


# ---------------------------------------------------------------- composition

def test_compose_refuses_before_building_anything(identity):
    """The precondition is upstream of the body, not a check on the way out."""
    with pytest.raises(optout_email.EmailRefused) as excinfo:
        optout_email.compose_request(
            "gumgum-com", "gumgum.com", "talbert@gumgum.com",
            identity, "me@example.org")
    # The refusal names the problem and does NOT carry the person's details.
    text = str(excinfo.value)
    assert "personal" in text
    assert FAKE_LAST not in text
    assert "742 Evergreen Terrace" not in text


def test_compose_builds_a_request_for_an_eligible_recipient(identity):
    message = optout_email.compose_request(
        "idm-us-com", "idm.us.com", "ccpa@idm.us.com", identity, "me@example.org")
    assert message["To"] == "ccpa@idm.us.com"
    assert message["From"] == "me@example.org"
    assert message["Reply-To"] == FAKE_EMAIL
    body = message.get_content()
    # The statutory hooks a broker needs to route it correctly.
    assert "opt out of the sale and sharing" in body
    assert "California Consumer Privacy Act" in body
    # The identifying details a broker needs to find the record.
    assert identity.full_name in body
    assert "742 Evergreen Terrace, Springfield, IL 62704" in body


def test_compose_requires_a_from_address(identity):
    with pytest.raises(optout_email.EmailRefused):
        optout_email.compose_request(
            "idm-us-com", "idm.us.com", "ccpa@idm.us.com", identity, "")


# --------------------------------------------------------------------- drafts

def test_draft_is_a_mailto_with_subject_and_body(identity):
    draft = optout_email.prepare_draft(
        "idm-us-com", "idm.us.com", "ccpa@idm.us.com", identity)
    assert draft["mailto"].startswith("mailto:ccpa@idm.us.com?")
    assert "subject=" in draft["mailto"]
    assert draft["to"] == "ccpa@idm.us.com"
    assert identity.full_name in draft["body"]


def test_draft_refuses_an_ineligible_recipient(identity):
    """A draft addressed to a stranger is still one Send press from a leak."""
    with pytest.raises(optout_email.EmailRefused):
        optout_email.prepare_draft(
            "uspeoplesearch-net", "uspeoplesearch.net", "tapster13@gmail.com",
            identity)


def test_overlong_mailto_drops_the_body_rather_than_truncating_it(identity):
    """A half-body would say something different from what was composed."""
    wordy = Identity(
        first_name=FAKE_FIRST, last_name=FAKE_LAST, emails=[FAKE_EMAIL],
        addresses=["%d Very Long Street Name, Springfield, IL" % n
                   for n in range(60)])
    draft = optout_email.prepare_draft(
        "idm-us-com", "idm.us.com", "ccpa@idm.us.com", wordy)
    assert draft["body_truncated"] is True
    assert "body=" not in draft["mailto"]
    assert "subject=" in draft["mailto"]
    # Nothing is lost -- the full text is still there to paste.
    assert "59 Very Long Street Name" in draft["body"]


def test_prepare_drafts_separates_the_worklist_from_the_ready(identity):
    result = optout_email.prepare_drafts([
        ("idm-us-com", "idm.us.com", "ccpa@idm.us.com"),
        ("gumgum-com", "gumgum.com", "talbert@gumgum.com"),
        ("idengine-com", "idengine.com", ""),
    ], identity)
    assert [d["broker_id"] for d in result["drafts"]] == ["idm-us-com"]
    assert {s["broker_id"]: s["reason"] for s in result["skipped"]} == {
        "gumgum-com": "personal", "idengine-com": "no_address"}


# ------------------------------------------------------------------ interlocks

def test_send_refuses_while_disabled(identity, cfg):
    transport = Transport()
    with pytest.raises(optout_email.EmailRefused):
        optout_email.send_request("idm-us-com", "idm.us.com", "ccpa@idm.us.com",
                                  identity, cfg, transport=transport)
    assert transport.sent == []


def test_enabled_alone_still_does_not_send(identity, tmp_path):
    """Two separate acts. Turning the feature on is only the first."""
    cfg = Config(review_dir=str(tmp_path / "review"), optout_email_enabled=True,
                 optout_email_from="me@example.org")
    transport = Transport()
    record = optout_email.send_request(
        "idm-us-com", "idm.us.com", "ccpa@idm.us.com", identity, cfg,
        transport=transport)
    assert record["outcome"] == review.OUTCOME_DRY_RUN
    assert record["dry_run"] is True
    assert transport.sent == []
    # The composed text is kept so it can be read before the flags move.
    assert identity.full_name in record["body"]


def test_both_flags_open_really_hands_it_to_the_transport(identity, tmp_path):
    cfg = Config(review_dir=str(tmp_path / "review"), optout_email_enabled=True,
                 optout_email_dry_run=False, optout_email_from="me@example.org")
    transport = Transport()
    record = optout_email.send_request(
        "idm-us-com", "idm.us.com", "ccpa@idm.us.com", identity, cfg,
        transport=transport)
    assert record["outcome"] == review.OUTCOME_SUBMITTED
    assert len(transport.sent) == 1
    assert transport.sent[0]["To"] == "ccpa@idm.us.com"


def test_live_send_without_a_transport_is_refused_not_guessed(identity, tmp_path):
    cfg = Config(review_dir=str(tmp_path / "review"), optout_email_enabled=True,
                 optout_email_dry_run=False, optout_email_from="me@example.org")
    with pytest.raises(optout_email.EmailRefused):
        optout_email.send_request("idm-us-com", "idm.us.com", "ccpa@idm.us.com",
                                  identity, cfg)


def test_transport_failure_is_recorded_not_raised(identity, tmp_path):
    cfg = Config(review_dir=str(tmp_path / "review"), optout_email_enabled=True,
                 optout_email_dry_run=False, optout_email_from="me@example.org")
    record = optout_email.send_request(
        "idm-us-com", "idm.us.com", "ccpa@idm.us.com", identity, cfg,
        transport=Transport(explode=True))
    assert record["outcome"] == review.OUTCOME_FAILED
    assert "OSError" in record["reason"]


def test_ineligible_send_is_recorded_as_needing_a_human(identity, tmp_path):
    """The 44 unvalidated rows surface as work, not as silence."""
    cfg = Config(review_dir=str(tmp_path / "review"), optout_email_enabled=True,
                 optout_email_dry_run=False, optout_email_from="me@example.org")
    transport = Transport()
    with pytest.raises(optout_email.EmailRefused):
        optout_email.send_request(
            "uspeoplesearch-net", "uspeoplesearch.net", "tapster13@gmail.com",
            identity, cfg, transport=transport)
    assert transport.sent == []
    records = review.load_attempts(str(tmp_path / "review"))
    assert len(records) == 1
    assert records[0]["outcome"] == review.OUTCOME_NEEDS_MANUAL
    assert records[0]["eligibility"] == "offdomain"


def test_eligibility_is_not_overridable_by_any_flag(identity, tmp_path):
    """No combination of configuration reaches a non-role recipient."""
    for enabled in (True, False):
        for dry in (True, False):
            cfg = Config(review_dir=str(tmp_path / "review"),
                         optout_email_enabled=enabled, optout_email_dry_run=dry,
                         optout_email_from="me@example.org")
            transport = Transport()
            with pytest.raises(optout_email.EmailRefused):
                optout_email.send_request(
                    "gumgum-com", "gumgum.com", "talbert@gumgum.com",
                    identity, cfg, transport=transport)
            assert transport.sent == []
