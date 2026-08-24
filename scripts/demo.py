"""End-to-end demo walkthrough.

Submits the three required learner queries, polls each job to completion, prints the
state transitions as they happen, and writes the finished videos plus a summary table.

    uv run python -m scripts.demo
    uv run python -m scripts.demo --base http://localhost:8000 --out ./submissions

Serves three purposes at once: the API walkthrough deliverable, the end-to-end smoke
test, and the thing you screen-record for the demo. Standard library plus httpx only,
so it works in Git Bash without jq.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

QUERIES = [
    "How does the pH scale work?",
    "Why do atoms form covalent bonds?",
    "What is the difference between ionic and covalent bonding?",
]

POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 900.0


def submit(client: httpx.Client, query: str) -> str:
    response = client.post("/videos", json={"query": query})
    if response.status_code != 202:
        raise SystemExit(f"submit failed [{response.status_code}]: {response.text}")
    body = response.json()
    print(f"  submitted   job_id={body['job_id']}  concept={body['concept']}")
    return body["job_id"]


def poll(client: httpx.Client, job_id: str) -> dict:
    """Poll until terminal, printing each state change once."""
    seen: str | None = None
    deadline = time.monotonic() + POLL_TIMEOUT_S

    while time.monotonic() < deadline:
        job = client.get(f"/videos/{job_id}").json()
        marker = f"{job['status']}/{job.get('stage')}"

        if marker != seen:
            elapsed = time.monotonic() - (deadline - POLL_TIMEOUT_S)
            print(f"  {elapsed:6.1f}s   {marker}")
            seen = marker

        if job["status"] in ("completed", "failed"):
            return job

        time.sleep(POLL_INTERVAL_S)

    raise SystemExit(f"timed out after {POLL_TIMEOUT_S}s waiting on {job_id}")


def download(client: httpx.Client, job: dict, out_dir: Path) -> Path:
    job_id = job["job_id"]
    target = out_dir / f"{job['concept']}_{job_id[:8]}"
    target.mkdir(parents=True, exist_ok=True)

    video = target / f"{job['concept']}_{job_id[:8]}.mp4"
    with client.stream("GET", f"/videos/{job_id}/artifact") as response:
        response.raise_for_status()
        with video.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)

    manifest = client.get(f"/videos/{job_id}/manifest").json()
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    script = client.get(f"/videos/{job_id}/script").json()
    (target / "script.json").write_text(
        json.dumps(script, indent=2), encoding="utf-8"
    )
    (target / "query.txt").write_text(job["query"] + "\n", encoding="utf-8")

    return video


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--out", default="./submissions", type=Path)
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base, timeout=60.0)

    try:
        health = client.get("/health").json()
    except httpx.ConnectError:
        raise SystemExit(f"no server at {args.base} - start it with `uv run uvicorn app.main:app`")

    if not health.get("ffmpeg"):
        raise SystemExit("server reports ffmpeg unavailable - see SETUP.md section 1")

    rows: list[dict] = []

    for index, query in enumerate(QUERIES, start=1):
        print(f"\n[{index}/{len(QUERIES)}] {query}")
        started = time.monotonic()
        job_id = submit(client, query)
        job = poll(client, job_id)
        wall = time.monotonic() - started

        if job["status"] == "failed":
            failure = job.get("failure") or {}
            print(f"  FAILED      {failure.get('code')} at {failure.get('stage')}")
            print(f"              {failure.get('message')}")
            rows.append({"query": query, "status": "failed",
                         "code": failure.get("code"), "wall": wall})
            continue

        video = download(client, job, args.out)
        artifact = job["artifact"]
        print(f"  saved       {video}")
        rows.append({
            "query": query,
            "status": "completed",
            "degraded": job["degraded"],
            "attempts": job["attempts"],
            "duration_s": artifact["duration_s"],
            "size_mb": artifact["size_bytes"] / 1_048_576,
            "cost_usd": job["cost"]["total_usd"],
            "prod_usd": job["cost"]["production_estimate_usd"],
            "wall": wall,
        })

    print("\n" + "=" * 78)
    print(f"{'concept':<22}{'status':<11}{'deg':<5}{'att':<5}{'dur':<8}{'MB':<7}{'wall':<8}{'prod $'}")
    print("-" * 78)
    for row in rows:
        if row["status"] != "completed":
            print(f"{row['query'][:20]:<22}{'failed':<11}{row.get('code','')}")
            continue
        print(
            f"{row['query'][:20]:<22}"
            f"{'completed':<11}"
            f"{('yes' if row['degraded'] else 'no'):<5}"
            f"{row['attempts']:<5}"
            f"{row['duration_s']:<8.1f}"
            f"{row['size_mb']:<7.1f}"
            f"{row['wall']:<8.1f}"
            f"{row['prod_usd']:.4f}"
        )
    print("=" * 78)

    failures = [r for r in rows if r["status"] != "completed"]
    if failures:
        print(f"\n{len(failures)} of {len(QUERIES)} concepts failed")
        return 1

    degraded = sum(1 for r in rows if r["degraded"])
    print(f"\nall {len(QUERIES)} concepts completed  ({degraded} degraded)")
    print(f"artifacts written to {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
