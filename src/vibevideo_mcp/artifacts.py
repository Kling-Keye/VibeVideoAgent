"""Register and verify immutable video and source snapshots."""

import shutil
import tempfile
import time
import uuid
from pathlib import Path

from . import audio_metrics, video_metrics
from .io import digest, inside, probe, run_command, write_json


def visual_image(source, target, crop=None, max_edge=1600):
    from PIL import Image, ImageOps

    with Image.open(source) as original:
        if original.width * original.height > 50_000_000:
            raise ValueError("Image exceeds 50 megapixels; render a smaller preview")
        im = ImageOps.exif_transpose(original).convert("RGB")
        original_size = list(im.size)
        if crop:
            x, y, w, h = crop
            if min(x, y) < 0 or min(w, h) <= 0 or x + w > im.width or y + h > im.height:
                raise ValueError("Crop must lie inside the source image")
            im = im.crop((x, y, x + w, y + h))
        im.thumbnail((max_edge, max_edge))
        im.save(target, quality=90)
        return {"original_size": original_size, "returned_size": list(im.size), "crop": crop}


class ProductionService:
    def __init__(self, workspace, root, state):
        self.workspace, self.root, self.state = workspace, root, state

    @staticmethod
    def timeout(seconds=45):
        return seconds

    def status(self):
        return {
            key: self.state.get(key)
            for key in ("task", "budget", "current_artifact", "delivery", "findings", "revision_checks")
        }

    def audio_metrics(self, path):
        return audio_metrics.measure(inside(self.workspace, path))

    def video_metrics(self, path):
        return video_metrics.measure(inside(self.workspace, path))

    def register_artifact(self, video, source_files, render_command):
        source = inside(self.workspace, video)
        paths = {name: inside(self.workspace, name) for name in source_files}
        cfg = self.state["config"]
        if source.stat().st_size > cfg["max_video_bytes"]:
            raise ValueError("Video exceeds the artifact size limit")
        if sum(p.stat().st_size for p in paths.values()) > cfg["max_snapshot_bytes"]:
            raise ValueError("Source files exceed the snapshot size limit")
        aid = "v-" + uuid.uuid4().hex[:16]
        base = self.root / "artifacts" / aid
        base.parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base.parent) as stage:
            staged = Path(stage) / aid
            staged.mkdir()
            # Keep the source extension so the snapshot also supports other video containers.
            final = staged / ("final" + source.suffix.lower())
            shutil.copyfile(source, final)
            info = probe(final)
            manifest = {}
            for name, path in paths.items():
                target = staged / "project" / name
                target.parent.mkdir(exist_ok=True, parents=True)
                shutil.copyfile(path, target)
                target.chmod(0o444)
                manifest[name] = digest(target)
            final.chmod(0o444)
            record = {
                "artifact_id": aid,
                "video": final.name,
                "sha256": digest(final),
                "sources": manifest,
                "render_command": render_command,
                "info": info,
            }
            write_json(staged / "manifest.json", record)
            staged.rename(base)
        self.state["artifacts"][aid] = record
        self.state["current_artifact"] = aid
        return {"artifact_id": aid, "sha256": record["sha256"], "info": info}

    def submit_delivery(self, artifact_id=None, notes=""):
        aid = artifact_id or self.state.get("current_artifact")
        if aid not in self.state["artifacts"]:
            raise ValueError("Register the rendered output before submitting")
        item = self.state["artifacts"][aid]
        folder = self.root / "artifacts" / aid
        video = folder / item["video"]
        if digest(video) != item["sha256"]:
            raise ValueError("Registered video changed")
        if any(digest(folder / "project" / name) != sha for name, sha in item["sources"].items()):
            raise ValueError("Registered project changed")
        info, spec = item["info"], self.state["task"]["delivery"]
        checks = {k: info[k] == spec[k] for k in ("width", "height") if spec[k] is not None}
        if spec["fps"] is not None:
            checks["fps"] = abs(info["fps"] - spec["fps"]) < 0.01
        if spec["min_duration"] is not None:
            checks["min_duration"] = info["duration"] + 0.02 >= spec["min_duration"]
        if spec["max_duration"] is not None:
            checks["max_duration"] = info["duration"] - 0.02 <= spec["max_duration"]
        if spec["audio_required"]:
            checks["audio_required"] = info["has_audio"]
        if not all(checks.values()):
            return {"accepted": False, "reason": "Delivery specification mismatch", "spec_checks": checks}
        run_command(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-threads",
                "1",
                "-i",
                str(video),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-f",
                "null",
                "-",
            ],
            timeout=300,
        )
        return {
            "accepted": True,
            "path": str(folder),
            "video": str(video),
            "sha256": item["sha256"],
            "artifact_id": aid,
            "file_decode": "passed",
            "spec_checks": checks,
            "notes": notes,
            "render_command": item["render_command"],
            "submitted_at": time.time(),
        }
