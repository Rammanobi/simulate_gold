import textwrap

import pytest

from simgold.triggers import load_rules, match_headline


def test_fed_rate_cut_with_cpi_wakes():
    r = match_headline("Fed signals rate cut as CPI cools")
    assert r["wake"] is True                        # two categories at urgency 3
    assert r["urgency"] == 3
    assert set(r["categories"]) == {"monetary_policy", "inflation_data"}
    assert r["symbols"] == ["GOLDBEES", "SILVERBEES"]


def test_chile_copper_mine_strike_affects_hindcopper_only_and_does_not_wake():
    r = match_headline("Chile copper mine strike halts output")
    assert r["symbols"] == ["HINDCOPPER"]
    assert r["categories"] == ["copper_specific"]
    assert r["urgency"] == 3
    assert r["wake"] is False


def test_plural_rate_cuts_match_but_commentary_alone_does_not_wake():
    r = match_headline("Fed hints at two rate cuts")
    assert "monetary_policy" in r["categories"]
    assert r["urgency"] == 3                        # 14 Oct 2026 tuning: Fed/RBI talk is urgency 3
    assert r["wake"] is False


def test_plural_missiles_match_geopolitics():
    r = match_headline("Missiles fired across the border")
    assert r["categories"] == ["geopolitics"]
    assert r["wake"] is True


def test_feds_still_does_not_match_fed_but_rate_cuts_does():
    r = match_headline("Feds hint at rate cuts")
    assert r["categories"] == ["monetary_policy"]
    assert r["wake"] is False                       # commentary, no decision word


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


# ------------------------------------------------------------------ 14 Oct 2026 tuning (docs/proposals/2026-10-14-news-trigger-tuning.md)
@pytest.mark.parametrize("title", [
    "Gold Steady as Fed Official’s Remarks Temper Rate-Hike Bets - Bloomberg.com",
    "Fed's Collins sees another rate hike on the table before end of 2026 - Reuters",
    "Williams says Fed may not have to hike rates in 2027, at odds with traders - CNBC",
    "Dollar languishes as focus turns to CPI, Fed speakers - Reuters",
    "Rupee hits one-month high after RBI backs FX curbs with stability pledge - Reuters",
    "RBI may raise repo rate by 75 bps, maintain tight stance through H1FY28 - The Assam Tribune",
    "Have a ₹50 lakh home loan? Check how much your EMI could rise after the RBI rate hike - Livemint",
    "Gold Holds Steady Ahead Of US Inflation Data, Fed Rate Outlook In Focus - BusinessToday Malaysia",
    "Fed’s Williams Interview With Bloomberg News: Full Transcript - Bloomberg.com",
])
def test_ordinary_fed_rbi_and_dollar_headlines_no_longer_wake(title):
    assert match_headline(title)["wake"] is False


@pytest.mark.parametrize("title", [
    "Fed cuts rates by 50 basis points",
    "Sensex sheds 430 points, Nifty loses 173 points after RBI hikes repo rate - Mid-Day",
    "RBI keeps repo rate unchanged, surprises markets",
    "Fed announces emergency rate cut",
    "FOMC rate decision due tonight",
])
def test_decision_words_escalate_monetary_policy_to_five(title):
    r = match_headline(title)
    assert r["wake"] is True and r["urgency"] == 5 and r["levels"]["monetary_policy"] == 5
    assert r["symbols"] == ["GOLDBEES", "SILVERBEES"]


def test_dollar_mention_does_not_spread_a_block_to_hindcopper():
    r = match_headline("RBI raises repo rate as rupee slides")
    assert set(r["categories"]) == {"monetary_policy", "dollar"}
    assert r["wake"] is True and r["symbols"] == ["GOLDBEES", "SILVERBEES"]
    assert r["wake_categories"] == ["monetary_policy"]


def test_context_phrase_never_beats_urgency_five():
    r = match_headline("NSE trading halt ahead of results season")
    assert r["context"] is True and r["urgency"] == 5 and r["wake"] is True
    assert match_headline("Missiles: what it means for markets")["wake"] is False


def test_real_data_release_with_two_categories_still_wakes():
    r = match_headline("India’s September inflation rises to 5.54%, strengthening case for further RBI rate hikes")
    assert r["wake"] is True and r["urgency"] == 3


def test_cooldown_is_passed_on():
    assert match_headline("Fed cuts rates")["cooldown_minutes"] == 120


def test_an_old_rules_file_without_tuning_keys_behaves_as_before(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(textwrap.dedent("""
        categories:
          monetary_policy: {keywords: [fed, rbi], affects: [GOLDBEES, SILVERBEES], urgency: 4}
          dollar: {keywords: [rupee], affects: [GOLDBEES, SILVERBEES, HINDCOPPER], urgency: 2}
        ignore: []
        """), encoding="utf-8")
    r = match_headline("Rupee gains as RBI ahead of outlook", path=str(rules))
    assert r["wake"] is True and r["urgency"] == 4 and r["symbols"] == ["GOLDBEES", "HINDCOPPER", "SILVERBEES"]
    assert r["cooldown_minutes"] == 0
