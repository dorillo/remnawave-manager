"""Snapshot RUTUBE's anonymous home recommendations, without thematic filtering."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src/remnawave_manager/data/disguises/02-aster-observatory/data/catalog.json"
CATEGORIES = {64: "culture", 57: "entertainment", 42: "kids", 8: "news", 5: "series", 6: "music", 19: "entertainment", 43: "entertainment", 3: "movies", 7: "sport"}
CHANNEL_VIDEO_FIELDS = ("id", "title", "thumbnail", "duration", "published", "topic", "views", "publicLikes", "age", "language")


def compact_comment(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict) or not re.fullmatch(r"\d{1,24}", str(raw.get("id", ""))):
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    author = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    return {
        "id": str(raw["id"]), "text": text.strip()[:5000],
        "created_ts": str(raw.get("created_ts", ""))[:40],
        "parent_id": str(raw["parent_id"]) if re.fullmatch(r"\d{1,24}", str(raw.get("parent_id", ""))) else None,
        "user": {
            "id": author.get("id"),
            "name": str(author.get("name", "RUTUBE"))[:100],
            "avatar": str(author.get("avatar_url", ""))[:1000],
        },
        "likes_number": raw.get("likes_number", 0), "dislikes_number": raw.get("dislikes_number", 0),
        "state": raw.get("state", 1), "is_deleted": raw.get("is_deleted", False), "is_pinned": raw.get("is_pinned", False),
    }


def compact_video(raw: object, *, comments: bool = False) -> dict[str, object] | None:
    if not isinstance(raw, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(raw.get("id", ""))):
        return None
    if any(raw.get(key) for key in ("is_adult", "is_paid", "is_club", "is_deleted", "is_hidden", "is_livestream")):
        return None
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    public_comments = []
    if comments:
        public_comments = [
            selected
            for item in raw.get("_public_comments", [])[:100]
            if (selected := compact_comment(item)) is not None
        ]
    likes = raw.get("positive_votes")
    return {
        "id": raw["id"], "title": str(raw.get("title", "RUTUBE"))[:300],
        "description": str(raw.get("description", ""))[:12000],
        "thumbnail": raw.get("thumbnail_url", ""), "duration": raw.get("duration", 0),
        "published": raw.get("publication_ts") or raw.get("created_ts", ""),
        "channelId": str(author.get("id", "")), "channel": author.get("name", "RUTUBE"),
        "avatar": author.get("avatar_url", ""),
        "topic": CATEGORIES.get((raw.get("category") or {}).get("id"), "general"),
        "views": raw.get("hits"),
        "publicLikes": likes if isinstance(likes, int) and likes >= 0 else None,
        "commentsCount": raw.get("comments_count", 0) if comments else 0,
        "publicComments": public_comments,
        "age": (raw.get("pg_rating") or {}).get("age"), "language": "ru",
    }


def compact_channel_video(video: dict[str, object]) -> dict[str, object]:
    return {key: video[key] for key in CHANNEL_VIDEO_FIELDS if key in video}


def compact_existing() -> None:
    data = json.loads(TARGET.read_text(encoding="utf-8"))
    for channel in data.get("channels", []):
        channel["videos"] = [compact_channel_video(video) for video in channel.get("videos", [])]
    temporary = TARGET.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(TARGET)
    print(f"Compacted {sum(len(channel['videos']) for channel in data.get('channels', []))} channel videos.")


def refresh(input_path: Path | None = None) -> None:
    if input_path:
        data = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
            raise ValueError("Input must be the enriched output of fetch_aster_feed.cjs")
    else:
        # A first-party browser is required for the related public counters,
        # discussions and channel pages. No keys or production service are used.
        result = subprocess.run(["node", str(ROOT / "scripts/fetch_aster_feed.cjs")], capture_output=True, timeout=300, check=True)
        data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("Invalid recommendation response; catalog unchanged")
    videos = {}
    for raw in data["results"][:500]:
        selected = compact_video(raw, comments=True)
        if selected:
            videos.setdefault(selected["id"], selected)
    if not videos:
        raise ValueError("No public recommendations received; catalog unchanged")
    channels = []
    for raw_channel in data.get("channels", [])[:100]:
        if not isinstance(raw_channel, dict) or not re.fullmatch(r"\d{1,20}", str(raw_channel.get("id", ""))):
            continue
        channel_videos = []
        known = set()
        for raw_video in raw_channel.get("videos", [])[:500]:
            selected = compact_video(raw_video)
            if selected and selected["id"] not in known:
                channel_videos.append(compact_channel_video(selected))
                known.add(selected["id"])
        if channel_videos:
            channels.append({
                "id": str(raw_channel["id"]),
                "name": str(raw_channel.get("name", "RUTUBE"))[:200],
                "avatar": str(raw_channel.get("avatar", ""))[:1000],
                "videos": channel_videos,
            })
    result = {"version": 1, "updated": datetime.now(timezone.utc).isoformat(), "source": "rutube-home", "channels": channels, "videos": list(videos.values())}
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGET.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(TARGET)
    print(f"Saved {len(videos)} home recommendations and {sum(len(c['videos']) for c in channels)} channel videos.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Previously saved output of fetch_aster_feed.cjs")
    parser.add_argument("--compact-existing", action="store_true", help="Remove repeated channel metadata from the current snapshot")
    arguments = parser.parse_args()
    compact_existing() if arguments.compact_existing else refresh(arguments.input)
