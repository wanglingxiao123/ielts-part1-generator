#!/usr/bin/env python3
"""Run the web tier locally with no AWS: in-memory stores, seeded with real records.

    python3 web/scripts/run_local.py \\
        --questions qti_export/tests/fixtures \\
        --candidates ielts-json-to-qti22/samples/input/_candidates \\
        --port 8000

Then open http://127.0.0.1:8000/ , register any email, and open a material at
`/materials/<material_id>`. What this tier can do without AWS:

  * login / register (in-memory user store, every domain allowed)
  * the reader page for every seeded material (`--candidates` records → batch history)
  * the 题目预览 tab (`--questions` documents → `_questions/`)
  * QTI 2.2 export, including a seeded V2 revision so the version selector has two entries

What it cannot do: generate anything (`AGENT_RUNTIME_ARN` is not set, so `generate` answers 503),
synthesize audio, or persist -- restart and everything is gone. It exists so the export feature,
the reader page and the comment panel can be exercised end to end on a laptop.

`--seed-revision` (default on) stores a fake V2 of the first seeded material: Q1's answer changed
on both sides so the exported v1 and v2 packages visibly differ. Pass `--no-seed-revision` to see a
material with a single version.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audio_storage.object_store import InMemoryObjectStore  # noqa: E402
from web.app import WebTier  # noqa: E402
from web.auth import AuthService, MemoryUserStore, SessionSigner  # noqa: E402
from web.batch_history import BatchHistory  # noqa: E402
from web.batch_store import InMemoryBatchStore  # noqa: E402
from web.comment_store import CommentService, InMemoryCommentStore  # noqa: E402
from web.question_versions import QuestionVersionService  # noqa: E402
from web.runtime_client import AgentCoreRuntimeClient  # noqa: E402
from web.slot_state import SlotStateReader  # noqa: E402

BATCH_ID = "local-seed"
REVISION_ID = "local-revision-v2"


def seed_questions(objects: InMemoryObjectStore, directory: Path) -> list:
    ids = []
    for path in sorted(directory.glob("*.json")):
        objects.put(f"_questions/{path.stem}.json", path.read_bytes())
        ids.append(path.stem)
    return ids


def seed_candidates(batches: InMemoryBatchStore, directory: Path, ids: list) -> list:
    """Candidate records carry material / blueprint / audit / cross_check: exactly what the reader
    page needs. Written as batch-history material sidecars under one synthetic batch."""
    seeded = []
    materials = []
    for material_id in ids:
        path = directory / f"{material_id}.json"
        if not path.exists():
            print(f"  (no candidate record for {material_id}; reader page will 404 for it)")
            continue
        candidate = json.loads(path.read_text(encoding="utf-8"))
        summary = {
            "material_id": material_id,
            "scenario_key": candidate.get("scenario_key") or "",
            "slot_id": candidate.get("slot_id") or "slot-1",
            "index": len(materials),
            "verdict": (candidate.get("audit") or {}).get("verdict") or candidate.get("verdict") or "",
            "degraded": bool(candidate.get("degraded")),
            "created_at": float(candidate.get("created_at") or time.time()),
        }
        artifacts = dict(summary, batch_id=BATCH_ID)
        for key in ("material", "blueprint", "audit", "cross_check", "validation_findings",
                    "group_key", "degraded_reason"):
            if candidate.get(key) is not None:
                artifacts[key] = candidate[key]
        batches.save_material(BATCH_ID, material_id, artifacts)
        materials.append(summary)
        seeded.append(material_id)
    batches.save_index(BATCH_ID, {
        "batch_id": BATCH_ID,
        "created_at": time.time(),
        "updated_at": time.time(),
        "status": "completed",
        "requested_total": len(materials),
        "scenarios": sorted({m["scenario_key"] for m in materials}),
        "materials": materials,
        "submitted_material_ids": [],
    })
    return seeded


def seed_revision(objects: InMemoryObjectStore, material_id: str) -> None:
    """A V2 with Q1's answer changed on both the writer's and the auditor's side."""
    doc = json.loads(objects.get(f"_questions/{material_id}.json"))
    package = copy.deepcopy(doc["package"])
    cross = copy.deepcopy(doc["cross_check"])
    original = str(package["answer_key"][0]["canonical"])
    changed = original + "-Local"
    package["answer_key"][0]["canonical"] = changed
    cross["items"][0]["writer_answer"] = changed
    cross["items"][0]["auditor_answer"] = changed
    version = {
        "id": REVISION_ID,
        "material_id": material_id,
        "created_at": "2026-09-03T00:00:00Z",
        "based_on_version_id": "original",
        "source_comment_ids": [],
        "status": "ready",
        "operation": "revise_questions",
        "package": package,
        "quality": {
            "label": "revised-local",
            "package": package,
            "review": doc["review"],
            "cross_check": cross,
            "validation": doc["validation"],
            "status": doc["status"],
        },
        "baseline_advisories": [],
        "changed_questions": [1],
        "created_by": "local seed",
    }
    objects.put(f"_question_versions/{material_id}/versions/{REVISION_ID}.json",
                json.dumps(version, ensure_ascii=False).encode("utf-8"))
    print(f"  V2 of {material_id}: Q1 {original!r} -> {changed!r} (not adopted; V1 stays active)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--questions", type=Path, default=ROOT / "qti_export" / "tests" / "fixtures",
                    help="directory of _questions/ documents ({material_id}.json)")
    ap.add_argument("--candidates", type=Path, default=None,
                    help="directory of candidate records for the same ids (reader page)")
    ap.add_argument("--static", type=Path, default=ROOT / "frontend" / "dist",
                    help="frontend build (npm run build)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-seed-revision", dest="seed_revision", action="store_false")
    args = ap.parse_args()

    if not (args.static / "index.html").exists():
        print(f"no frontend build at {args.static}; run `cd frontend && npm run build`", file=sys.stderr)
        return 2

    objects = InMemoryObjectStore()
    batches = InMemoryBatchStore()
    ids = seed_questions(objects, args.questions)
    print(f"seeded {len(ids)} question set(s) from {args.questions}")
    if args.candidates:
        seeded = seed_candidates(batches, args.candidates, ids)
        print(f"seeded {len(seeded)} material record(s) from {args.candidates}")
    if args.seed_revision and ids:
        seed_revision(objects, ids[0])

    tier = WebTier(
        AuthService(MemoryUserStore(), SessionSigner(os.urandom(32)), ["*"]),
        AgentCoreRuntimeClient(),
        str(args.static),
        history=BatchHistory(batches),
        slot_state=SlotStateReader(objects),
        comments=CommentService(InMemoryCommentStore()),
        question_versions=QuestionVersionService(objects),
    )
    print("open:")
    for material_id in ids:
        print(f"  http://{args.host}:{args.port}/materials/{material_id}")

    import uvicorn

    uvicorn.run(tier.app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
