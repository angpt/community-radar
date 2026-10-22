from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator

from app.radar import GitHubRateLimited, RepoNotFound, UpstreamError, get_radar

REPO_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$"


def reject_dot_names(repo: str) -> str:
    # "." and ".." pass REPO_PATTERN but would turn into path segments in the GitHub URL.
    if repo.split("/")[1] in (".", ".."):
        raise ValueError("repo name cannot be '.' or '..'")
    return repo


app = FastAPI(title="Community Radar")


@app.get("/api/radar")
async def radar(
    repo: Annotated[str, Query(pattern=REPO_PATTERN), AfterValidator(reject_dot_names)],
):
    try:
        return await get_radar(repo)
    except GitHubRateLimited as exc:
        return JSONResponse(
            status_code=429,
            content={
                "error": "github_rate_limited",
                "message": "GitHub API rate limit exceeded. Set GITHUB_TOKEN or retry later.",
                "reset_at": exc.reset_at,
            },
        )
    except RepoNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": "repo_not_found", "message": f"Repository {repo} not found."},
        )
    except UpstreamError as exc:
        return JSONResponse(
            status_code=502, content={"error": "github_unavailable", "message": str(exc)}
        )


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
