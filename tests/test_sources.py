import json
from datetime import date, timedelta

import httpx
import pytest

from conftest import at, fixture_text
from job_tracking.config import ArbeitsagenturConfig, GoogleConfig, LinkedInConfig
from job_tracking.sources import arbeitsagentur, google, linkedin
from job_tracking.sources.base import RateLimited, SourceError

NOW = at("03.11.2026 19:00")


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- LinkedIn ---------------------------------------------------------------


def test_linkedin_parses_cards():
    jobs = linkedin.parse_search_page(fixture_text("linkedin_search.html"), NOW, "Germany")
    assert [j.source_id for j in jobs] == ["1000000006", "1000000001", "1000000002", "1000000099"]

    first = jobs[0]
    assert first.title == "Graduate AI Engineer – NLP"
    assert first.company == "Weserlicht Software"
    assert (first.location, first.country) == ("Bremen", "Germany")
    assert first.posted_text == "14 minutes ago"
    assert first.posted_date == date(2026, 11, 3)
    assert first.posted_at == NOW - timedelta(minutes=14)
    assert first.url == "https://www.linkedin.com/jobs/view/1000000006"
    assert first.source == "LinkedIn"

    # A card that is itself the link, with no data-entity-urn: id comes from the href.
    third = jobs[2]
    assert third.company == "Rheinblick AI"
    assert third.work_type == "Hybrid"
    assert third.location == "Munich"

    # Country-only location.
    assert (jobs[3].location, jobs[3].country) == ("", "Germany")


def test_linkedin_request_params_and_stops_paging_when_all_known():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, text=fixture_text("linkedin_search.html"))

    config = LinkedInConfig(max_pages=3, delay_seconds=0, experience_levels=(1, 2))
    source = linkedin.LinkedInSource(config, "Germany", client_for(handler), sleep=lambda s: None)
    result = source.fetch(["Data Analyst"], NOW, is_known=lambda _id: False)

    # Fixture has 4 cards (< page size of 10), so one request is enough.
    assert len(requests) == 1
    params = requests[0].url.params
    assert params["keywords"] == "Data Analyst"
    assert params["location"] == "Germany"
    assert params["f_TPR"] == "r86400"
    assert params["f_E"] == "1,2"
    assert params["sortBy"] == "DD"
    assert len(result.jobs) == 4


def test_linkedin_full_pages_continue_until_known():
    card = fixture_text("linkedin_search.html").split("<li>")[2]  # the 1000000001 card
    full_page = "".join(card.replace("1000000001", f"20000000{i:02d}") for i in range(10))
    calls = []

    def handler(request):
        calls.append(request.url.params["start"])
        return httpx.Response(200, text=full_page)

    source = linkedin.LinkedInSource(
        LinkedInConfig(max_pages=5, delay_seconds=0), "Germany", client_for(handler), sleep=lambda s: None
    )
    # Every card is already stored -> stop after the first page.
    source.fetch(["x"], NOW, is_known=lambda _id: True)
    assert calls == ["0"]

    calls.clear()
    source.fetch(["x"], NOW, is_known=lambda _id: False)
    assert calls == ["0", "10", "20", "30", "40"]


def test_linkedin_rate_limit_with_nothing_fetched_fails_the_source():
    source = linkedin.LinkedInSource(
        LinkedInConfig(delay_seconds=0),
        "Germany",
        client_for(lambda r: httpx.Response(429)),
        sleep=lambda s: None,
    )
    with pytest.raises(RateLimited):
        source.fetch(["Data Analyst", "Data Scientist"], NOW, is_known=lambda _id: False)


def test_linkedin_rate_limit_after_some_results_keeps_them():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text=fixture_text("linkedin_search.html"))
        return httpx.Response(429)

    source = linkedin.LinkedInSource(
        LinkedInConfig(delay_seconds=0), "Germany", client_for(handler), sleep=lambda s: None
    )
    result = source.fetch(["Data Analyst", "Data Scientist"], NOW, is_known=lambda _id: False)
    assert len(result.jobs) == 4
    assert result.warnings and "rate limited" in result.warnings[0]


# --- Arbeitsagentur ---------------------------------------------------------


def test_arbeitsagentur_fetch():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json=json.loads(fixture_text("arbeitsagentur_search.json")))

    config = ArbeitsagenturConfig(offer_types=(1,), published_within_days=1)
    source = arbeitsagentur.ArbeitsagenturSource(config, "Germany", client_for(handler))
    result = source.fetch(["Data Analyst"], NOW, is_known=lambda _id: False)

    request = requests[0]
    assert request.url.path.endswith("/pc/v6/jobs")
    assert request.headers["X-API-Key"] == "jobboerse-jobsuche"
    assert request.url.params["was"] == "Data Analyst"
    assert request.url.params["angebotsart"] == "1"
    assert request.url.params["veroeffentlichtseit"] == "1"

    by_id = {j.source_id: j for j in result.jobs}
    hamburg = by_id["10000-0000000001-S"]
    assert hamburg.title == "Datenanalyst (m/w/d)"
    assert hamburg.company == "Stadtwerke Elbtal"
    assert (hamburg.location, hamburg.country) == ("Hamburg", "Germany")
    assert hamburg.work_type == "On-site"
    assert hamburg.posted_text == "Today"
    assert hamburg.url == "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-0000000001-S"

    munich = by_id["10000-0000000002-S"]
    assert munich.location == "Munich"
    assert munich.work_type == "Hybrid"
    assert munich.posted_text == "1 day ago"
    assert munich.posted_date == date(2026, 11, 2)

    anonymous = by_id["10000-0000000003-S"]
    assert anonymous.company == ""
    assert anonymous.work_type == ""


def test_arbeitsagentur_all_searches_failing_raises():
    source = arbeitsagentur.ArbeitsagenturSource(
        ArbeitsagenturConfig(offer_types=(1, 34)), "Germany", client_for(lambda r: httpx.Response(403))
    )
    with pytest.raises(SourceError):
        source.fetch(["Data Analyst"], NOW, is_known=lambda _id: False)


# --- Google (SerpApi) -------------------------------------------------------


def test_google_fetch():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=json.loads(fixture_text("serpapi_google_jobs.json")))

    source = google.GoogleJobsSource(GoogleConfig(), "secret-key", "Germany", client_for(handler))
    result = source.fetch(["Data Scientist"], NOW, is_known=lambda _id: False)

    params = requests[0].url.params
    assert params["engine"] == "google_jobs"
    assert params["q"] == "Data Scientist"
    assert params["api_key"] == "secret-key"

    first, second = result.jobs
    assert first.title == "Praktikum Data Science (m/w/d)"
    assert (first.location, first.country) == ("Munich", "Germany")
    assert first.work_type == "Hybrid"
    assert first.posted_text == "9 hours ago"
    assert first.posted_at == NOW - timedelta(hours=9)
    assert first.url == "https://www.google.com/search?q=dummy-job-1&ibp=htl;jobs"

    assert second.work_type == "Remote"
    assert second.location == ""
    assert second.posted_date == date(2026, 11, 2)


def test_google_no_results_is_not_an_error():
    body = {"error": "Google hasn't returned any results for this query."}
    source = google.GoogleJobsSource(
        GoogleConfig(), "k", "Germany", client_for(lambda r: httpx.Response(200, json=body))
    )
    assert source.fetch(["x"], NOW, is_known=lambda _id: False).jobs == []
