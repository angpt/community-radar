"""Upstream fetching (GitHub + HN Algolia), verdict logic and caching."""

import asyncio
import os
import time
from datetime import datetime, timezone

import httpx

GITHUB_API = "https://api.github.com"
HN_SEARCH = "https://hn.algolia.com/api/v1/search"
TIMEOUT_S = 5.0
CACHE_TTL_S = 600
HN_WINDOW_DAYS = 90

# Verdict thresholds
DORMANT_DAYS = 180
BUZZ_POINTS = 100
TRACTION_STARS = 1000

_cache: dict[str, tuple[float, dict]] = {}


class GitHubRateLimited(Exception):
    def __init__(self, reset_at: str | None):
        self.reset_at = reset_at


class RepoNotFound(Exception):
    pass


class UpstreamError(Exception):
    pass


async def get_with_retry(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET with one retry on network errors, timeouts and 5xx responses."""
    for attempt in range(2):
        try:
            resp = await client.get(url, **kwargs)
        except httpx.TransportError:
            if attempt == 1:
                raise
            continue
        if resp.status_code >= 500 and attempt == 0:
            continue
        return resp
    raise AssertionError("unreachable")


def rate_limit_reset_at(headers: httpx.Headers) -> str | None:
    """ISO-8601 UTC time when GitHub will accept requests again, if it says.

    Secondary rate limits send retry-after (seconds); primary ones send x-ratelimit-reset (epoch).
    """
    try:
        if retry_after := headers.get("retry-after"):
            reset = time.time() + int(retry_after)
        elif reset_header := headers.get("x-ratelimit-reset"):
            reset = int(reset_header)
        else:
            return None
    except ValueError:
        return None
    return datetime.fromtimestamp(reset, timezone.utc).isoformat()


async def fetch_github(client: httpx.AsyncClient, repo: str) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        # Renamed or transferred repos answer with a 301 to the new location.
        resp = await get_with_retry(
            client, f"{GITHUB_API}/repos/{repo}", headers=headers, follow_redirects=True
        )
    except httpx.TransportError as exc:
        raise UpstreamError(f"GitHub unreachable: {exc.__class__.__name__}") from exc

    retry_after = resp.headers.get("retry-after")
    if resp.status_code == 429 or (
        resp.status_code == 403
        and (resp.headers.get("x-ratelimit-remaining") == "0" or retry_after)
    ):
        raise GitHubRateLimited(rate_limit_reset_at(resp.headers))
    if resp.status_code == 401:
        raise UpstreamError("GITHUB_TOKEN is invalid or expired.")
    if resp.status_code == 404:
        raise RepoNotFound(repo)
    if resp.status_code != 200:
        raise UpstreamError(f"GitHub returned {resp.status_code}")

    data = resp.json()
    return {
        "full_name": data["full_name"],
        "url": data["html_url"],
        "stars": data["stargazers_count"],
        "forks": data["forks_count"],
        "open_issues": data["open_issues_count"],
        "pushed_at": data["pushed_at"],
    }


async def fetch_hn(client: httpx.AsyncClient, repo: str) -> dict:
    since = int(time.time()) - HN_WINDOW_DAYS * 86400
    params = {
        "query": repo,
        "tags": "story",
        "numericFilters": f"created_at_i>{since}",
        "hitsPerPage": 100,
    }
    resp = await get_with_retry(client, HN_SEARCH, params=params)
    resp.raise_for_status()
    body = resp.json()
    # Algolia's keyword search is fuzzy, so keep only stories with the exact owner/name text.
    needle = repo.lower()
    hits = [
        h
        for h in body.get("hits", [])
        if any(needle in (h.get(f) or "").lower() for f in ("title", "url", "story_text"))
    ]

    top = max(hits, key=lambda h: h.get("points") or 0, default=None)
    return {
        "stories": len(hits),
        "points": sum(h.get("points") or 0 for h in hits),
        "comments": sum(h.get("num_comments") or 0 for h in hits),
        "top_story": top and {
            "title": top.get("title"),
            "points": top.get("points") or 0,
            "url": f"https://news.ycombinator.com/item?id={top['objectID']}",
        },
    }


def compute_verdict(github: dict, hn: dict | None, now: datetime | None = None) -> tuple[str, str]:
    """Deterministic verdict. Missing HN data counts as zero buzz."""
    now = now or datetime.now(timezone.utc)
    pushed = datetime.fromisoformat(github["pushed_at"].replace("Z", "+00:00"))
    idle_days = (now - pushed).days
    points = hn["points"] if hn else 0
    stars = github["stars"]

    if idle_days > DORMANT_DAYS:
        return "dormant", f"No push in {idle_days} days."
    if points >= BUZZ_POINTS and stars >= TRACTION_STARS:
        return "real_traction", f"{points} HN points and {stars} stars."
    if points >= BUZZ_POINTS:
        return "hype", f"{points} HN points but only {stars} stars."
    return "quiet_builder", f"Active (last push {idle_days}d ago) with little HN buzz."


async def get_radar(repo: str) -> dict:
    key = repo.lower()
    cached = _cache.get(key)
    if cached and cached[0] > time.monotonic():
        return {**cached[1], "cached": True}

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        github, hn = await asyncio.gather(
            fetch_github(client, repo), fetch_hn(client, repo), return_exceptions=True
        )

    if isinstance(github, Exception):
        raise github
    warnings = []
    if isinstance(hn, Exception):
        hn = None
        warnings.append("hn_unavailable")

    verdict, reason = compute_verdict(github, hn)
    result = {
        "repo": github["full_name"],
        "verdict": verdict,
        "reason": reason,
        "github": github,
        "hn": hn,
        "warnings": warnings,
    }
    # Partial results aren't cached so we recover as soon as HN is back.
    if not warnings:
        _cache[key] = (time.monotonic() + CACHE_TTL_S, result)
    return {**result, "cached": False}
