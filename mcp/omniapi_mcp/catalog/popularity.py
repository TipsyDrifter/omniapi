"""Popularity: where a model stands on the Artificial Analysis leaderboards
(``popularity.json``, built by ``mcp/scripts/refresh_popularity.py``).

Only data: the service never reorders a list by it; the GUI sorts by the
``rank`` each model row carries. A model row gets

* ``popularity`` — ``{board: rank}`` for every board of its modality it is on;
* ``rank`` — its best rank there (the sort key);
* ``rank_badge`` — ``{board, label, rank, score}`` of its best rank whose id
  mapping is not a guess (a guessed mapping only orders the list).

A model on no board gets none of the three.

Ids are matched in a normalised form: case, a leading ``~``, a ``:batch``
style suffix and dots versus dashes do not matter (``claude-opus-5-5`` and
``anthropic/claude-opus-5.5:batch`` both find their rows).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: board key -> the modality it ranks and its name on the page
BOARDS: dict[str, dict[str, str]] = {
    "text": {"modality": "text", "label": "智慧指數"},
    "image_t2i": {"modality": "image", "label": "文生圖"},
    "image_edit": {"modality": "image", "label": "改圖"},
    "video_t2v": {"modality": "video", "label": "文生影片"},
    "video_i2v": {"modality": "video", "label": "圖生影片"},
    "speech": {"modality": "speech", "label": "語音合成"},
    "music_vocals": {"modality": "music", "label": "音樂・人聲"},
    "music_instrumental": {"modality": "music", "label": "音樂・純音樂"},
}
CONFIDENCES = ("sure", "likely", "guess")
#: an id of a live roster (OpenRouter): vendor/name
ROSTER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*$")


def norm_id(model_id: str) -> str:
    """The form two spellings of one model share."""
    s = model_id.strip().lower().lstrip("~")
    s = s.split(":", 1)[0]
    return s.replace(".", "-")


def validate(doc: dict[str, Any], curated: dict[str, set[str]] | None = None) -> list[str]:
    """Problems in a ``popularity.json`` document, one line each. ``curated``
    (modality -> curated ids, aliases included) checks that a direct id (no
    ``/``) is a model the catalog has; an OpenRouter id must at least have
    the roster's ``vendor/name`` shape (the roster is live)."""
    problems: list[str] = []
    boards = doc.get("boards")
    if not isinstance(boards, dict):
        return ["popularity.json: boards is missing"]
    for key, b in boards.items():
        where = f"popularity.boards.{key}"
        if key not in BOARDS:
            problems.append(f"{where}: unknown board (known: {', '.join(BOARDS)})")
            continue
        if b.get("modality") != BOARDS[key]["modality"]:
            problems.append(f"{where}: modality {b.get('modality')!r} is not {BOARDS[key]['modality']!r}")
        last = 0
        for r in b.get("rows") or []:
            rank = r.get("rank")
            if not isinstance(rank, int) or rank <= last:
                problems.append(f"{where}: rank {rank!r} is not a whole number above {last}")
            else:
                last = rank
            for i in r.get("ids") or []:
                mid, conf = i.get("id"), i.get("confidence")
                if conf not in CONFIDENCES:
                    problems.append(f"{where} #{rank}: {mid!r} has confidence {conf!r} (known: {', '.join(CONFIDENCES)})")
                if not isinstance(mid, str) or not mid or mid != mid.strip():
                    problems.append(f"{where} #{rank}: bad id {mid!r}")
                elif "/" in mid:
                    if not ROSTER_ID.match(mid):
                        problems.append(f"{where} #{rank}: {mid!r} is not a vendor/name roster id")
                elif curated is not None and mid not in curated.get(BOARDS[key]["modality"], set()):
                    problems.append(f"{where} #{rank}: {mid!r} is not a curated {BOARDS[key]['modality']} model")
    return problems


class Popularity:
    """The leaderboard ranks, indexed by (modality, normalised id)."""

    def __init__(self, path: Path | None = None):
        self.path = path or Path(__file__).with_name("popularity.json")
        self.doc: dict[str, Any] = {}
        self._index: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self.load()

    def load(self) -> None:
        try:
            self.doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:  # a missing or broken file costs the ordering, nothing else
            logger.error("popularity.json unreadable: %s", e)
            self.doc = {}
        self._index.clear()
        for key, b in (self.doc.get("boards") or {}).items():
            meta = BOARDS.get(key)
            if not meta:
                continue
            for r in b.get("rows") or []:
                for i in r.get("ids") or []:
                    if not isinstance(i.get("id"), str):
                        continue
                    slot = self._index.setdefault((meta["modality"], norm_id(i["id"])), {})
                    cur = slot.get(key)
                    sure = i.get("confidence") != "guess"
                    if cur is None:
                        cur = slot[key] = {"rank": r["rank"], "badge": None}
                    else:
                        cur["rank"] = min(cur["rank"], r["rank"])
                    if sure and (cur["badge"] is None or r["rank"] < cur["badge"]["rank"]):
                        cur["badge"] = {"board": key, "label": meta["label"], "rank": r["rank"], "score": r.get("score")}

    def boards(self) -> dict[str, dict[str, Any]]:
        """Each board's label, source and version (no rows)."""
        return {k: {kk: v for kk, v in b.items() if kk != "rows"} for k, b in (self.doc.get("boards") or {}).items()}

    def lookup(self, model_id: str, modality: str, aliases: list[str] | tuple[str, ...] = ()) -> dict[str, Any] | None:
        """``{popularity, rank, rank_badge?}`` for a model, or ``None`` when it is on no board."""
        ranks: dict[str, int] = {}
        badges: list[dict[str, Any]] = []
        for mid in (model_id, *aliases):
            for key, v in (self._index.get((modality, norm_id(mid))) or {}).items():
                ranks[key] = min(ranks.get(key, v["rank"]), v["rank"])
                if v["badge"]:
                    badges.append(v["badge"])
        if not ranks:
            return None
        out: dict[str, Any] = {"popularity": ranks, "rank": min(ranks.values())}
        if badges:
            out["rank_badge"] = dict(min(badges, key=lambda b: b["rank"]))
        return out


#: process-wide; read once (the file ships with the package)
popularity = Popularity()
