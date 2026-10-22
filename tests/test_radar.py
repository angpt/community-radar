from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import radar
from app.main import app

GH_URL = "https://api.github.com/repos/acme/widget"
HN_URL = "https://hn.algolia.com/api/v1/search"

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_cache():
    radar._cache.clear()


def gh_repo(stars=5000, days_ago=3):
    pushed = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "full_name": "acme/widget",
        "html_url": "https://github.com/acme/widget",
        "stargazers_count": stars,
        "forks_count": 120,
        "open_issues_count": 42,
        "pushed_at": pushed.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def hn_body(points=(300, 50)):
    hits = [
        {"objectID": str(i), "title": f"Story {i} about acme/widget", "points": p, "num_comments": 10}
        for i, p in enumerate(points)
    ]
    return {"nbHits": len(hits), "hits": hits}


def mock(gh=None, hn=None):
    respx.get(GH_URL).mock(return_value=httpx.Response(200, json=gh or gh_repo()))
    respx.get(HN_URL).mock(return_value=httpx.Response(200, json=hn or hn_body()))


@respx.mock
def test_happy_path():
    mock()
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "real_traction"
    assert body["warnings"] == []
    assert body["github"]["stars"] == 5000
    assert body["hn"] == {
        "stories": 2,
        "points": 350,
        "comments": 20,
        "top_story": {
            "title": "Story 0 about acme/widget",
            "points": 300,
            "url": "https://news.ycombinator.com/item?id=0",
        },
    }


@respx.mock
def test_hn_hits_without_exact_repo_text_are_dropped():
    hits = [
        {"objectID": "1", "title": "Show HN: Widget", "url": "https://github.com/ACME/Widget",
         "points": 40, "num_comments": 5},
        {"objectID": "2", "title": "Ask HN", "story_text": "I use acme/widget daily",
         "points": 10, "num_comments": 2},
        {"objectID": "3", "title": "Acme widgets and more", "url": "https://acme.example",
         "points": 900, "num_comments": 300},
        {"objectID": "4", "title": "acme/widgetry", "points": 500, "num_comments": 80},
        {"objectID": "5", "title": "Notes on acme/widget.", "url": "https://github.com/acme/widget/issues/1",
         "points": None, "num_comments": None},
    ]
    mock(hn={"nbHits": 250, "hits": hits})
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    hn = r.json()["hn"]
    assert hn["stories"] == 3
    assert hn["points"] == 50
    assert hn["comments"] == 7
    assert hn["top_story"]["url"] == "https://news.ycombinator.com/item?id=1"


@pytest.mark.parametrize(
    "text, matches",
    [
        ("Show HN: facebook/react 19", True),
        ("https://github.com/facebook/react/pull/1", True),
        ("git clone https://github.com/facebook/react.git", True),
        ("I like facebook/react.", True),
        ("(facebook/react)", True),
        ("FACEBOOK/REACT", True),
        ("facebook/react-native", False),
        ("facebook/react_dom", False),
        ("facebook/reactor", False),
        ("facebook/react.js", False),
        ("notfacebook/react", False),
        ("my-facebook/react", False),
    ],
)
def test_repo_mention_pattern(text, matches):
    assert bool(radar.repo_mention_pattern("facebook/react").search(text)) is matches


@respx.mock
def test_second_call_is_cached():
    mock()
    client.get("/api/radar", params={"repo": "acme/widget"})
    r = client.get("/api/radar", params={"repo": "ACME/widget"})
    assert r.json()["cached"] is True
    assert respx.calls.call_count == 2  # one GitHub + one HN call total


@respx.mock
def test_hn_down_returns_github_data_with_warning():
    respx.get(GH_URL).mock(return_value=httpx.Response(200, json=gh_repo()))
    hn_route = respx.get(HN_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 200
    body = r.json()
    assert body["warnings"] == ["hn_unavailable"]
    assert body["hn"] is None
    assert body["github"]["stars"] == 5000
    assert hn_route.call_count == 2  # one retry


@respx.mock
def test_upstream_5xx_is_retried_once():
    respx.get(GH_URL).mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=gh_repo())]
    )
    respx.get(HN_URL).mock(return_value=httpx.Response(200, json=hn_body()))
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 200


@respx.mock
def test_github_rate_limited():
    respx.get(GH_URL).mock(
        return_value=httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"}
        )
    )
    respx.get(HN_URL).mock(return_value=httpx.Response(200, json=hn_body()))
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 429
    assert r.json()["error"] == "github_rate_limited"
    assert r.json()["reset_at"] == "2023-11-14T22:13:20+00:00"


@pytest.mark.parametrize(
    "status, headers",
    [
        (403, {"retry-after": "60", "x-ratelimit-remaining": "4000"}),
        (429, {"retry-after": "60"}),
    ],
)
@respx.mock
def test_github_secondary_rate_limit(status, headers):
    respx.get(GH_URL).mock(return_value=httpx.Response(status, headers=headers))
    respx.get(HN_URL).mock(return_value=httpx.Response(200, json=hn_body()))
    before = datetime.now(timezone.utc)
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 429
    assert r.json()["error"] == "github_rate_limited"
    reset_at = datetime.fromisoformat(r.json()["reset_at"])
    assert before + timedelta(seconds=59) <= reset_at <= before + timedelta(seconds=65)


@respx.mock
def test_github_429_without_headers_is_rate_limited():
    respx.get(GH_URL).mock(return_value=httpx.Response(429))
    respx.get(HN_URL).mock(return_value=httpx.Response(200, json=hn_body()))
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 429
    assert r.json()["reset_at"] is None


@respx.mock
def test_plain_github_403_is_unavailable():
    respx.get(GH_URL).mock(return_value=httpx.Response(403))
    respx.get(HN_URL).mock(return_value=httpx.Response(200, json=hn_body()))
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 502
    assert r.json()["error"] == "github_unavailable"


@respx.mock
def test_renamed_repo_follows_redirect():
    new_url = "https://api.github.com/repositories/123"
    respx.get(GH_URL).mock(return_value=httpx.Response(301, headers={"location": new_url}))
    respx.get(new_url).mock(
        return_value=httpx.Response(200, json={**gh_repo(), "full_name": "acme/gadget"})
    )
    respx.get(HN_URL).mock(return_value=httpx.Response(200, json=hn_body()))
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 200
    assert r.json()["repo"] == "acme/gadget"


@respx.mock
def test_repo_not_found():
    respx.get(GH_URL).mock(return_value=httpx.Response(404))
    respx.get(HN_URL).mock(return_value=httpx.Response(200, json=hn_body()))
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 404


@respx.mock
def test_bad_github_token():
    respx.get(GH_URL).mock(return_value=httpx.Response(401))
    respx.get(HN_URL).mock(return_value=httpx.Response(200, json=hn_body()))
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.status_code == 502
    assert r.json() == {
        "error": "github_unavailable",
        "message": "GITHUB_TOKEN is invalid or expired.",
    }


@pytest.mark.parametrize(
    "repo", ["", "acme", "acme/", "/widget", "acme/widget/extra", "ac me/widget", "-acme/widget", "acme/wid;get", "acme/.", "acme/.."]
)
def test_invalid_input(repo):
    r = client.get("/api/radar", params={"repo": repo})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "stars, days_ago, points, expected",
    [
        (5000, 3, (300,), "real_traction"),
        (50, 3, (300,), "hype"),
        (5000, 3, (20,), "quiet_builder"),
        (5000, 400, (300,), "dormant"),
    ],
)
@respx.mock
def test_verdicts(stars, days_ago, points, expected):
    mock(gh=gh_repo(stars=stars, days_ago=days_ago), hn=hn_body(points))
    r = client.get("/api/radar", params={"repo": "acme/widget"})
    assert r.json()["verdict"] == expected
