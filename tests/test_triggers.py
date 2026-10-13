import textwrap

import pytest

from simgold.triggers import load_rules, match_headline


def test_fed_rate_cut_with_cpi_wakes():
    r = match_headline("Fed signals rate cut as CPI cools")
    assert r["wake"] is True
    assert r["urgency"] == 4
    assert set(r["categories"]) == {"monetary_policy", "inflation_data"}
    assert r["symbols"] == ["GOLDBEES", "SILVERBEES"]


def test_chile_copper_mine_strike_affects_hindcopper_only_and_does_not_wake():
    r = match_headline("Chile copper mine strike halts output")
    assert r["symbols"] == ["HINDCOPPER"]
    assert r["categories"] == ["copper_specific"]
    assert r["urgency"] == 3
    assert r["wake"] is False


def test_fed_does_not_match_federal_or_feds():
    assert match_headline("Federal reserve minutes released today")["categories"] == []
    assert match_headline("Feds hint at a pause")["categories"] == []


def test_war_does_not_match_toward_award_or_warning():
    assert match_headline("Stocks drift toward record highs")["categories"] == []
    assert match_headline("Analyst wins industry award")["categories"] == []
    assert match_headline("Regulator issues warning on margins")["categories"] == []


def test_war_matches_as_a_whole_word_and_wakes():
    r = match_headline("War escalates in the region")
    assert r["categories"] == ["geopolitics"]
    assert r["urgency"] == 4
    assert r["wake"] is True


def test_ignore_list_beats_keyword_matches():
    r = match_headline("Akshaya Tritiya gold jewellery offer: customs duty waived")
    assert r == {"urgency": 0, "categories": [], "symbols": [], "wake": False}


def test_two_categories_wake_even_below_urgency_four():
    r = match_headline("CPI data and jobless claims both surprise")
    assert set(r["categories"]) == {"inflation_data", "jobs"}
    assert r["urgency"] == 3
    assert r["wake"] is True


def test_single_low_urgency_category_does_not_wake():
    r = match_headline("CPI cools further")
    assert r["categories"] == ["inflation_data"]
    assert r["wake"] is False


def test_market_halt_has_urgency_five_and_all_symbols():
    r = match_headline("NSE circuit breaker triggered on volatility")
    assert r["urgency"] == 5
    assert r["symbols"] == ["GOLDBEES", "HINDCOPPER", "SILVERBEES"]
    assert r["wake"] is True


def test_company_action_on_hindustan_copper_wakes_via_two_categories():
    r = match_headline("Government announces offer for sale in Hindustan Copper")
    assert set(r["categories"]) == {"company_actions", "hindcopper"}
    assert r["symbols"] == ["HINDCOPPER"]
    assert r["wake"] is True


def test_import_duty_maps_to_india_duty_budget():
    r = match_headline("Union budget raises import duty on bullion")
    assert r["categories"] == ["india_duty_budget"]
    assert r["urgency"] == 4
    assert r["wake"] is True


def test_fed_chair_replaces_powell():
    assert match_headline("Powell speaks at Jackson Hole")["categories"] == []
    assert match_headline("Fed chair speaks at Jackson Hole")["categories"] == ["monetary_policy"]


def test_case_insensitive_and_whitespace_tolerant_phrases():
    r = match_headline("FOMC   MINUTES show doubts")
    assert r["categories"] == ["monetary_policy"]


def test_empty_and_unrelated_headlines_return_no_match():
    empty = {"urgency": 0, "categories": [], "symbols": [], "wake": False}
    assert match_headline("") == empty
    assert match_headline("Sensex closes flat on thin volumes") == empty


def test_load_rules_is_cached():
    assert load_rules("config/news_triggers.yaml") is load_rules("config/news_triggers.yaml")


def test_symbols_drop_ns_suffix(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        textwrap.dedent(
            """
            categories:
              test_cat:
                keywords: [alpha signal]
                affects: [GOLDBEES.NS, SILVERBEES]
                urgency: 2
            ignore: []
            """
        ),
        encoding="utf-8",
    )
    r = match_headline("alpha signal seen", path=str(rules))
    assert r["symbols"] == ["GOLDBEES", "SILVERBEES"]
    assert r["wake"] is False


def test_bad_config_is_rejected(tmp_path):
    rules = tmp_path / "bad.yaml"
    rules.write_text("categories:\n  broken:\n    urgency: 3\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_rules(str(rules))
