"""Parity score and the manual-tail packets."""
import json

import pytest
from fastapi.testclient import TestClient

from broker_guard import manual_packets, optout_forms, parity, webui
from broker_guard.config import Config
from broker_guard.profile import Identity


def _row(domain, **kw):
    base = {"name": domain.split(".")[0].title(), "domain": domain, "category": "x",
            "opt_out_url": "https://%s/optout" % domain, "opt_out_method": "web-form",
            "opt_out_email": "", "required_fields": "", "verification_step": "",
            "opt_out": "known", "source": "t", "last_verified": None, "notes": ""}
    base.update(kw)
    return base


@pytest.fixture
def me():
    return Identity(first_name="Pat", last_name="Example", emails=["pat@example.test"],
                    addresses=["12 Main St, Reno, NV 89501"], phones=["775-555-0100"])


def test_a_live_recipe_row_is_automated():
    assert parity.classify_row(_row("consumercanvas.com"), live_recipes={"consumercanvas-com": 1}) == (
        parity.AUTOMATED, parity.RECIPE)


def test_email_needs_a_role_address_on_the_brokers_own_domain():
    ok = _row("acme.com", opt_out_email="privacy@acme.com")
    other_domain = _row("acme.com", opt_out_email="privacy@elsewhere.com")
    person = _row("acme.com", opt_out_email="jsmith@acme.com")
    assert parity.classify_row(ok) == (parity.AUTOMATED, parity.EMAIL)
    assert parity.classify_row(other_domain)[0] != parity.AUTOMATED
    assert parity.classify_row(person)[0] != parity.AUTOMATED
    assert parity.classify_row(ok, email_channel=False)[0] != parity.AUTOMATED


@pytest.mark.parametrize("step,expected", [
    ("phone verification (call / code) required", parity.PHONE),
    ("government ID / notarization may be required", parity.NOTARY),
    ("no online submission form -- the page instructs removal by email, fax, or mail", parity.MAIL),
    ("identity verified after submission via dynamically-generated knowledge questions", parity.KBA),
])
def test_manual_tail_kinds_come_from_the_datasets_own_words(step, expected):
    assert parity.classify_row(_row("zzz-nothing.test", verification_step=step)) == (
        parity.NEEDS_YOU, expected)


def test_ssn_and_unredacted_id_demands_are_needs_you_never_automated():
    for bid, demand in optout_forms.OUT_OF_SCOPE_ID_DEMAND.items():
        if demand == "redacted_id":
            continue
        domain = bid.rsplit("-", 1)[0] + "." + bid.rsplit("-", 1)[1]
        bucket, _ = parity.classify_row(_row(domain))
        assert bucket == parity.NEEDS_YOU, bid


def test_redacted_id_rows_are_pipeline_until_a_recipe_exists():
    assert parity.classify_row(_row("warmly.ai")) == (parity.PIPELINE, parity.ID_RECIPE_PENDING)


def test_every_row_lands_in_exactly_one_bucket_and_counts_add_up():
    rows = parity.load_source()
    score = parity.compute(rows)
    assert sum(score.counts.values()) == score.total == len({parity.row_id(r) for r in rows})
    assert sum(score.detail.values()) == score.total
    d = score.as_dict()
    assert 0 < d["automated_share"] < 1 and d["needs_you_share"] > 0


def test_the_score_moves_when_a_channel_is_switched_off():
    """Mutation guard: if email were never counted the share would collapse; the
    test proves the score actually depends on the channels it claims."""
    rows = parity.load_source()
    with_email = parity.compute(rows).counts[parity.AUTOMATED]
    without = parity.compute(rows, email_channel=False).counts[parity.AUTOMATED]
    assert with_email > without + 100
    assert parity.compute(rows, live_recipes={}, email_channel=False).counts[parity.AUTOMATED] == 0


# --- packets -----------------------------------------------------------------

def test_packets_exist_for_every_needs_you_row_with_steps_and_prefill(me):
    rows = parity.load_source()
    packets = manual_packets.build_packets(rows, me)
    assert len(packets) == parity.compute(rows).counts[parity.NEEDS_YOU]
    for p in packets:
        assert p["steps"] and p["prefilled"]["Name"] == "Pat Example"
    kinds = {p["kind"] for p in packets}
    assert {parity.PHONE, parity.NOTARY} <= kinds


def test_notarized_letter_carries_the_notary_block_and_state_statute(me):
    row = _row("lawco.test", name="LawCo", verification_step="notarization required")
    text = manual_packets.render_letter_text(row, me, parity.NOTARY)
    assert "LawCo" in text and "Pat Example" in text and "12 Main St" in text
    assert "Notary acknowledgment" in text and "NRS 603A.345" in text
    assert "{" not in text
    plain = manual_packets.render_letter_text(row, me, parity.MAIL)
    assert "Notary acknowledgment" not in plain


# --- web ---------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, profile_file, brokers_file):
    cfg = Config(profile_path=profile_file, brokers_path=brokers_file,
                 state_path=str(tmp_path / "s.sqlite"), log_dir=str(tmp_path / "logs"),
                 profiles_path=str(tmp_path / "p.json"), settings_path=str(tmp_path / "set.json"))
    webui.app.dependency_overrides[webui.get_config] = lambda: cfg
    yield TestClient(webui.app)
    webui.app.dependency_overrides.clear()


def test_dashboard_shows_the_parity_score(client):
    page = client.get("/").text
    assert "handled automatically" in page and "needs you" in page


def test_manual_page_lists_groups_and_links_letters(client):
    page = client.get("/manual")
    assert page.status_code == 200
    assert "Phone or SMS verification" in page.text and "Printable letter" in page.text


def test_letter_route_serves_only_letter_kinds(client):
    rows = parity.load_source()
    me = Identity(first_name="x", last_name="y")
    letter_ids = [p["broker_id"] for p in manual_packets.build_packets(rows, me) if p["has_letter"]]
    other = next(p["broker_id"] for p in manual_packets.build_packets(rows, me) if not p["has_letter"])
    assert client.get("/manual/%s/letter" % letter_ids[0]).status_code == 200
    assert client.get("/manual/%s/letter" % other).status_code == 404
    assert client.get("/manual/not-a-broker/letter").status_code == 404
