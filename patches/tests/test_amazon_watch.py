"""Amazon watcher: detection, per-creator tagging and the real-post audit.

The promise to a creator is one sentence — *every Amazon link in a deal goes out
signed with your own Associates tag* — and these tests pin it from both ends: a
source post is read link by link before anything is sent, and the posts that
were really sent are read back to prove whose tag is on them.
"""
from __future__ import annotations

import json

import pytest

from influencer_hub import amazon_watch, config, db, link_router

SOURCE_POST = """🔥 AMAZON LOOT ALERT 🔥
boAt Airdopes 141 TWS at ₹999 (MRP ₹2999)
Buy here: https://www.amazon.in/dp/B09N3ZNHTY/ref=sr_1_3?tag=someoneelse-21&th=1
Short link: https://amzn.to/3xYzAbC
Also on: amazon.in/dp/B0BDHWDR12
Search: https://www.amazon.in/s?k=earbuds
Flipkart offer: https://www.flipkart.com/boat-earbuds/p/itm123
Join https://t.me/somesource for more
"""


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "amazon.sqlite3")
    monkeypatch.setattr(config, "AMAZON_ASSOCIATE_TAG", "mama086-21")
    db.init()
    return db


def _creator(name="Ravi Loots", tag="ravi-21", *, channels=2):
    inf_id = db.add_influencer(name, tag)
    main = db.add_channel(inf_id, "telegram", f"@{tag.split('-')[0]}_loots", status="ready",
                          role="broadcast")
    if channels > 1:
        db.add_channel(inf_id, "whatsapp_channel", "120363012345678901@newsletter",
                       status="ready", role="whatsapp")
    return inf_id, main


# --------------------------------------------------------------------------
# 1. Detection: every shape a source can write
# --------------------------------------------------------------------------
def test_scan_finds_every_shape_of_amazon_link(hub):
    found = amazon_watch.scan(SOURCE_POST)

    assert found["has_amazon"] is True
    kinds = [link["kind"] for link in found["links"]]
    assert kinds == ["product", "short", "product", "search"]      # Flipkart/t.me ignored
    assert found["counts"] == {"product": 2, "short": 1, "search": 1}
    product = found["links"][0]
    assert product["asin"] == "B09N3ZNHTY"
    assert product["source_tag"] == "someoneelse-21"               # whose tag is on it now
    assert product["action"] == "retag"
    assert found["links"][1]["action"] == "resolve"                # amzn.to short code
    # A link written without https:// is still a link, not prose.
    assert found["links"][2]["url"].startswith("https://amazon.in/dp/B0BDHWDR12")
    assert found["foreign_source_tags"] == ["someoneelse-21"]


def test_scan_is_honest_about_a_post_without_amazon_links(hub):
    found = amazon_watch.scan("Flipkart loot ₹499 https://www.flipkart.com/x/p/itm1")

    assert found["has_amazon"] is False
    assert found["links"] == [] and found["counts"] == {}


# --------------------------------------------------------------------------
# 2. One creator's post
# --------------------------------------------------------------------------
def test_a_creators_post_never_carries_the_sources_own_tag(hub):
    plan = amazon_watch.plan(SOURCE_POST, tag="ravi-21")

    assert plan["effective_tag"] == "ravi-21" and plan["usable_tag"] is True
    assert "someoneelse-21" not in plan["rendered"]
    assert "tag=ravi-21" in plan["rendered"]
    assert "https://www.amazon.in/dp/B09N3ZNHTY?th=1&tag=ravi-21" in plan["rendered"]
    # The scheme-less link is promoted and signed too.
    assert "https://www.amazon.in/dp/B0BDHWDR12?tag=ravi-21" in plan["rendered"]
    # The short code is reported as resolved-before-send, not silently trusted.
    assert any("short code" in issue for issue in plan["issues"])
    assert plan["ok"] is True


def test_an_unusable_tag_falls_back_and_says_so(hub):
    plan = amazon_watch.plan(SOURCE_POST, tag="not a tag!")

    assert plan["usable_tag"] is False
    assert plan["effective_tag"] == config.AMAZON_ASSOCIATE_TAG
    assert f"tag={config.AMAZON_ASSOCIATE_TAG}" in plan["rendered"]
    assert any("not a usable Associates tag" in issue for issue in plan["issues"])


def test_the_approval_channel_keeps_its_disclosure(hub):
    plan = amazon_watch.plan(SOURCE_POST, tag="ravi-21", role="approval")

    assert "#ad" in plan["rendered"].lower()
    assert "tag=ravi-21" in plan["rendered"]


# --------------------------------------------------------------------------
# 3. The fleet: a thousand creators, one pass
# --------------------------------------------------------------------------
def test_a_thousand_creators_each_get_their_own_tag(hub):
    for index in range(1000):
        db.add_influencer(f"Creator {index:04d}", f"creator{index:04d}-21")

    fleet = amazon_watch.fleet_plan(SOURCE_POST, db.list_influencers())

    assert fleet["creators"] == 1000
    assert fleet["own_tag"] == 1000
    assert fleet["fallback_or_missing"] == 0
    assert fleet["all_own_tags"] is True
    assert fleet["details"] == []                    # nothing to warn about
    assert fleet["ms"] < 30_000                      # a preflight, not a batch job


def test_the_fleet_plan_names_a_creator_whose_tag_is_broken(hub):
    db.add_influencer("Good Creator", "good-21")
    db.add_influencer("Broken Creator", "spaces are not allowed")

    fleet = amazon_watch.fleet_plan(SOURCE_POST, db.list_influencers())

    assert fleet["all_own_tags"] is False
    assert fleet["fallback_or_missing"] == 1
    assert fleet["details"][0]["creator"] == "Broken Creator"
    assert fleet["details"][0]["effective_tag"] == config.AMAZON_ASSOCIATE_TAG


# --------------------------------------------------------------------------
# 4. The posts we really sent
# --------------------------------------------------------------------------
def test_the_attribution_report_proves_the_posts_that_went_out(hub):
    inf_id, main = _creator()
    for index in range(3):
        db.record_post(inf_id, main, f"deal-{index}", status="posted",
                       deal_text=f"Deal {index}: https://www.amazon.in/dp/B09N3ZNHTY?tag=ravi-21")
    other, other_main = _creator("Sita Deals", "sita-21")
    db.record_post(other, other_main, "sita-1", status="posted",
                   deal_text="Sita: https://www.amazon.in/dp/B0BDHWDR12?tag=sita-21")
    db.record_post(inf_id, main, "mentions-only", status="posted",
                   deal_text="No link in this one.")

    report = amazon_watch.attribution_report(hours=24)

    assert report["posts_scanned"] == 5
    assert report["posts_with_amazon"] == 4
    assert (report["own_tag"], report["foreign_tag"], report["untagged"]) == (4, 0, 0)
    assert report["ok"] is True
    assert "own tag" in amazon_watch.render_report(report)


def test_a_link_with_someone_elses_tag_is_a_problem_with_a_name(hub):
    inf_id, main = _creator("Divya Offers", "divya-21")
    db.record_post(inf_id, main, "leak", status="posted",
                   deal_text="Deal: https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21")

    report = amazon_watch.attribution_report(hours=24)

    assert report["ok"] is False and report["foreign_tag"] == 1
    problem = report["problems"][0]
    assert problem["creator"] == "Divya Offers"
    assert problem["found_tag"] == "someoneelse-21"
    assert problem["expected_tag"] == "divya-21"
    assert "⛔" in amazon_watch.render_report(report)


def test_an_untagged_link_and_a_short_code_are_reported_not_hidden(hub):
    inf_id, main = _creator()
    db.record_post(inf_id, main, "bare", status="posted",
                   deal_text="Bare: https://www.amazon.in/dp/B09N3ZNHTY")
    db.record_post(inf_id, main, "short", status="posted",
                   deal_text="Short: https://amzn.to/3xYzAbC")

    report = amazon_watch.attribution_report(hours=24)

    assert (report["untagged"], report["short_links"]) == (1, 1)
    assert report["ok"] is True                      # informational, not a leak
    line = amazon_watch.render_report(report)
    assert "untagged" in line and "short code" in line


# --------------------------------------------------------------------------
# 5. The surfaces an operator uses
# --------------------------------------------------------------------------
def test_the_cli_scan_check_and_report(hub, tmp_path, capsys):
    from influencer_hub import cli

    inf_id, main = _creator()
    db.record_post(inf_id, main, "deal", status="posted",
                   deal_text="Deal: https://www.amazon.in/dp/B09N3ZNHTY?tag=ravi-21")
    source = tmp_path / "source.txt"
    source.write_text(SOURCE_POST)

    assert cli.main(["amazon-scan", "--file", str(source)]) == 0
    out = capsys.readouterr().out
    assert "Amazon links in this source post: 4" in out
    assert "source tag: someoneelse-21" in out

    assert cli.main(["amazon-scan", "--file", str(source), "--fleet", "--check"]) == 0
    assert "every creator gets their OWN tag" in capsys.readouterr().out

    assert cli.main(["amazon-scan", "--report", "--check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)          # --json stays machine-readable
    assert payload["check"]["ok"] is True
    assert payload["report"]["posts_scanned"] == 1
    assert payload["report"]["own_tag"] == 1 and payload["report"]["foreign_tag"] == 0

    # A leak makes --check fail, with the creator named in the output.
    db.record_post(inf_id, main, "leak", status="posted",
                   deal_text="Deal: https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21")
    assert cli.main(["amazon-scan", "--report", "--check"]) == 1
    out = capsys.readouterr().out
    assert "check failed" in out and "someoneelse-21" in out

    assert cli.main(["amazon-scan", "--creator", str(inf_id), "--file", str(source)]) == 0
    assert "tag=ravi-21" in capsys.readouterr().out

    assert cli.main(["amazon-scan", "--file", str(tmp_path / "missing.txt")]) == 1
    assert "could not read" in capsys.readouterr().out


def test_the_dashboard_serves_the_report_and_the_scan(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "http.sqlite3")
    db.init()
    inf_id, main = _creator()
    db.record_post(inf_id, main, "deal", status="posted",
                   deal_text="Deal: https://www.amazon.in/dp/B09N3ZNHTY?tag=ravi-21")
    from dashboard.app import app

    app.config.update(TESTING=True)
    client = app.test_client()

    report = client.get("/api/amazon-report?hours=24").get_json()
    assert report["ok"] is True and report["own_tag"] == 1
    assert "own tag" in report["card"]

    scan = client.get("/api/amazon-scan", query_string={"text": SOURCE_POST}).get_json()
    assert scan["ok"] is True and len(scan["scan"]["links"]) == 4

    one = client.get("/api/amazon-scan", query_string={"text": SOURCE_POST,
                                                      "creator": inf_id}).get_json()
    assert one["plan"]["effective_tag"] == "ravi-21"
    assert "tag=ravi-21" in one["plan"]["rendered"]

    fleet = client.get("/api/amazon-scan", query_string={"text": SOURCE_POST,
                                                        "fleet": "1"}).get_json()
    assert fleet["fleet"]["creators"] == 1 and fleet["fleet"]["all_own_tags"] is True

    assert client.get("/api/amazon-scan").status_code == 400      # no text
    assert client.get("/api/amazon-scan", query_string={"text": "x",
                                                        "creator": 999999}).status_code == 404

    before = db.post_stats()
    client.get("/api/amazon-scan", query_string={"text": SOURCE_POST, "fleet": "1"})
    assert db.post_stats() == before                    # absolutely read-only


def test_the_ops_card_shows_the_amazon_proof(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "card.sqlite3")
    db.init()
    inf_id, main = _creator()
    db.record_post(inf_id, main, "deal", status="posted",
                   deal_text="Deal: https://www.amazon.in/dp/B09N3ZNHTY?tag=ravi-21")
    from influencer_hub import ops

    data = ops.digest(hours=24)
    page = __import__("dashboard.app", fromlist=["app"]).app.test_client().get("/").get_data(as_text=True)

    assert data["amazon"]["own_tag"] == 1 and data["amazon"]["ok"] is True
    assert "Amazon: 1 link(s) with the creator's own tag" in ops.render_digest(data)
    assert 'data-live-ops="amazon"' in page
    assert 'id="amazon-preview"' in page and "Check every creator (fleet)" in page
    assert "amazon-scan --file /tmp/post.txt --fleet --check" in page


def test_the_self_check_reports_the_promise_and_fails_on_a_leak(hub, monkeypatch):
    from influencer_hub import selfcheck

    empty = selfcheck.run_checks()
    check = next(c for c in empty["checks"] if c["id"] == "amazon_links")
    assert check["state"] == "skip"                     # nothing sent yet

    inf_id, main = _creator()
    db.record_post(inf_id, main, "deal", status="posted",
                   deal_text="Deal: https://www.amazon.in/dp/B09N3ZNHTY?tag=ravi-21")
    good = next(c for c in selfcheck.run_checks()["checks"] if c["id"] == "amazon_links")
    assert good["state"] == "ok"
    assert "own tag" in good["detail"]

    db.record_post(inf_id, main, "leak", status="posted",
                   deal_text="Deal: https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21")
    bad = next(c for c in selfcheck.run_checks()["checks"] if c["id"] == "amazon_links")
    assert bad["state"] == "fail"
    assert "someoneelse-21" in bad["detail"] and "pays someone else" in bad["fix"]


def test_the_amazon_check_never_runs_a_network_call(monkeypatch, hub):
    """Detection is text work; only the pipeline resolves short codes at send time."""
    import urllib.request

    def explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the amazon watcher must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    monkeypatch.setattr(link_router, "_default_resolve", explode)

    amazon_watch.scan(SOURCE_POST)
    amazon_watch.plan(SOURCE_POST, tag="ravi-21")
    amazon_watch.attribution_report(hours=24)
