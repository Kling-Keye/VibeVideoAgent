"""Versioned audiovisual findings for a video production task."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from .io import digest, inside, read_json, run_command, write_json
from .media_tools import TOOLS
from .perception import VERDICTS, infer
from .artifacts import ProductionService, visual_image


def streams(path, timeout):
    return json.loads(
        run_command(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            timeout=timeout,
        )
    )


def fingerprint(path):
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class MediaService(ProductionService):
    tool_names = set(TOOLS) | {"register_artifact", "submit_delivery", "status"}

    def _folder(self):
        folder = self.root / "perception" / uuid.uuid4().hex
        folder.mkdir(parents=True)
        return folder

    def _excerpt(self, folder, spec, *, default_fps=2, audio_only=False):
        source = inside(self.workspace, spec["path"])
        stamp = fingerprint(source)
        if stamp[1] > self.state["config"]["max_video_bytes"]:
            raise ValueError("Source exceeds configured size limit")
        paths = {name: inside(self.workspace, name) for name in spec.get("source_files", [])}
        if sum(p.stat().st_size for p in paths.values()) > self.state["config"]["max_snapshot_bytes"]:
            raise ValueError("Selected source files exceed fingerprint budget")
        source_hashes = {name: digest(p) for name, p in paths.items()}
        spec_key = hashlib.sha256(
            json.dumps([spec, stamp, source_hashes, default_fps, audio_only], sort_keys=True).encode()
        ).hexdigest()
        excerpts = self.state.setdefault("media_excerpts", {})
        if spec_key in excerpts:
            saved = excerpts[spec_key]
            if Path(saved["file"]).is_file() and digest(Path(saved["file"])) == saved["excerpt_sha256"]:
                return saved
        source_hash = digest(source)
        metadata = streams(source, self.timeout())
        tracks = metadata["streams"]
        video = next(
            (
                s
                for s in tracks
                if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")
            ),
            None,
        )
        has_audio = any(s["codec_type"] == "audio" for s in tracks)
        fmt = metadata["format"]
        still = video and (fmt["format_name"].endswith("_pipe") or fmt["format_name"] == "image2")
        modality = "audio" if audio_only else spec.get("modality", "auto")
        if modality == "auto":
            modality = "audiovisual" if video and has_audio else "visual" if video else "audio"
        if modality != "visual" and not has_audio:
            raise ValueError("Requested audio modality but source has no audio stream")
        if modality != "audio" and not video:
            raise ValueError("Requested visual modality but source has no visual stream")
        start = spec.get("start", 0)
        crop = spec.get("crop")
        if crop:
            x, y, w, h = crop
            if (
                modality == "audio"
                or min(x, y) < 0
                or min(w, h) < 1
                or x + w > video["width"]
                or y + h > video["height"]
            ):
                raise ValueError("Invalid source pixel crop")
        fps = None
        if still:
            if start or spec.get("end") is not None:
                raise ValueError("Still images have no time interval")
            end = 0
            target = folder / "still.jpg"
            visual_image(source, target, crop, max_edge=2048)
            mime = "image/jpeg"
        else:
            duration = float(fmt.get("duration") or 0)
            end = spec.get("end") if spec.get("end") is not None else duration
            if not 0 <= start < end <= duration:
                raise ValueError("Interval lies outside media duration")
            cfg = self.state["config"]
            command = [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-threads",
                "1",
                "-ss",
                str(start),
                "-i",
                str(source),
                "-t",
                str(end - start),
                "-map_metadata",
                "-1",
            ]
            if modality == "audio":
                target, mime = folder / "audio.ogg", "audio/ogg"
                command += ["-map", "0:a:0", "-vn", "-c:a", "libopus", "-b:a", "64k"]
            else:
                target, mime = folder / "clip.mp4", "video/mp4"
                fps = min(
                    spec.get("fps") or default_fps, cfg["max_fps"], cfg["frames_per_call"] / (end - start)
                )
                rate = video.get("avg_frame_rate", "0/0").split("/")
                if len(rate) == 2 and float(rate[1]) and float(rate[0]):
                    fps = min(fps, float(rate[0]) / float(rate[1]))
                filters = [f"crop={w}:{h}:{x}:{y}:exact=1"] if crop else []
                filters += [
                    f"fps={fps}:round=up",
                    "scale=w='min(iw,1280)':h='min(ih,1280)':force_original_aspect_ratio=decrease",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                ]
                command += [
                    "-map",
                    f"0:{video['index']}",
                    "-vf",
                    ",".join(filters),
                    "-threads",
                    "1",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                ]
                command += (
                    ["-map", "0:a:0", "-ac", "2", "-c:a", "aac"] if modality == "audiovisual" else ["-an"]
                )
            run_command(command + [str(target)], timeout=self.timeout(100))
        if fingerprint(source) != stamp:
            raise ValueError("Draft changed while sampling; inspect again after rendering")
        excerpt = {
            "file": str(target),
            "mime": mime,
            "path": spec["path"],
            "sha256": source_hash,
            "excerpt_sha256": digest(target),
            "start": start,
            "end": end,
            "duration": end - start,
            "fps": fps,
            "crop": crop,
            "modality": modality,
            "timeline_offset": spec.get("timeline_offset"),
            "source_hashes": source_hashes,
        }

        excerpts[spec_key] = excerpt
        return excerpt

    def _infer(self, folder, request):
        cfg = self.state["config"]
        if not os.getenv("GEMINI_API_KEY") or not cfg["perception_model"]:
            raise ValueError("Set GEMINI_API_KEY and VIBEVIDEO_GEMINI_MODEL in the client environment.")
        budget = self.state["budget"]
        frames = sum(
            math.ceil(m["duration"] * m["fps"]) if m["fps"] else int(m["mime"] == "image/jpeg")
            for m in request["media"]
        )
        seconds = sum(m["duration"] for m in request["media"] if m["modality"] != "visual")
        if (
            budget["calls"] >= cfg["max_observation_calls"]
            or frames + budget["frames"] > cfg["max_frames"]
            or seconds + budget["audio_seconds"] > cfg["max_audio_seconds"]
        ):
            raise ValueError("Observation budget exhausted")
        budget["calls"] += 1
        budget["frames"] += frames
        budget["audio_seconds"] += seconds
        return infer(request, cfg["perception_model"])

    def _observe(self, folder, operation, question, media, **links):
        instruction = question
        if links.get("required_evidence"):
            instruction += "\nCite separate evidence for each required modality: " + ", ".join(
                links["required_evidence"]
            )
        request = {"operation": operation, "question": instruction, "media": media}
        coverage = [{k: v for k, v in m.items() if k != "file"} for m in media]
        identity = [
            os.getenv("VIBEVIDEO_GEMINI_BASE_URL") or "",
            self.state["config"]["perception_model"],
            operation,
            question,
            coverage,
            links,
        ]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache = self.state.setdefault("finding_cache", {})
        if key in cache:
            previous = read_json(self.root / "perception" / cache[key] / "finding.json")
            if not any(
                Path(m["file"]).parent == folder for m in self.state.get("media_excerpts", {}).values()
            ):
                shutil.rmtree(folder)
            return {k: v for k, v in previous.items() if k != "media"} | {"cache_hit": True}
        result = self._infer(folder, request)
        if not isinstance(result.get("summary"), str) or not isinstance(result.get("evidence"), list):
            raise ValueError("Malformed semantic finding")
        if result["verdict"] not in VERDICTS.get(operation, ["observed", "uncertain"]):
            raise ValueError("Invalid perception verdict")
        for row in result["evidence"]:
            index = row["media_index"]
            if type(index) != int or not 0 <= index < len(media):
                raise ValueError("Evidence references an unknown excerpt")
            m = media[index]
            a, b = row["start"], row["end"]
            box = row["box"]
            if (
                not 0 <= a <= b <= m["duration"] + 0.001
                or row["modality"] not in {"audio", "visual"}
                or m["modality"] not in {row["modality"], "audiovisual"}
                or not row["text"].strip()
                or box
                and (
                    row["modality"] != "visual"
                    or len(box) != 4
                    or not 0 <= box[0] < box[2] <= 1
                    or not 0 <= box[1] < box[3] <= 1
                )
            ):
                raise ValueError("Evidence exceeds the supplied time, spatial or modality coverage")
            row["source_range"] = [m["start"] + t for t in (a, b)]
            if m["timeline_offset"] is not None:
                row["timeline_range"] = [m["timeline_offset"] + t for t in row["source_range"]]
        if {r["media_index"] for r in result["evidence"]} != set(range(len(media))):
            result["verdict"] = "uncertain"
        if any(
            set(links.get("required_evidence", []))
            - {r["modality"] for r in result["evidence"] if r["media_index"] == index}
            for index in range(len(media))
        ):
            result["verdict"] = "uncertain"
        if (
            operation == "compare_media"
            and links.get("mode") == "reference"
            and result["verdict"] in {"resolved", "persists"}
        ):
            result["verdict"] = "uncertain"
        if (
            operation == "compare_media"
            and result["verdict"] == "resolved"
            and media[0]["sha256"] == media[1]["sha256"]
        ):
            result["verdict"] = "unchanged"
        finding = {
            "perception_model": self.state["config"]["perception_model"],
            "finding_id": folder.name,
            "operation": operation,
            "question": question,
            **result,
            **links,
            "coverage": coverage,
            "created_at": time.time(),
        }
        write_json(folder / "finding.json", {**finding, "media": media})
        cache[key] = folder.name
        self.state.setdefault("findings", {})[folder.name] = {
            "operation": operation,
            "verdict": result["verdict"],
            "modalities": [m["modality"] for m in media],
            "is_still": media[-1]["mime"] == "image/jpeg",
            "created_at": finding["created_at"],
            "previous": links.get("previous"),
            "media_sha256": media[-1]["sha256"],
        }
        if operation == "check_requirement":
            self.state.setdefault("requirement_checks", {})[folder.name] = {
                "requirement": question,
                "source": links["requirement_source"],
                "media_sha256": media[0]["sha256"],
                "verdict": result["verdict"],
                "evidence": result["evidence"],
            }
        if links.get("mode") == "revision":
            before, after = media
            files = set(before["source_hashes"]) | set(after["source_hashes"])
            self.state.setdefault("revision_checks", {})[folder.name] = {
                "previous_finding": links["previous"],
                "before_sha256": before["sha256"],
                "after_sha256": after["sha256"],
                "verdict": result["verdict"],
                "evidence": result["evidence"],
                "changed_declared_sources": sorted(
                    name
                    for name in files
                    if before["source_hashes"].get(name) != after["source_hashes"].get(name)
                ),
            }
        return finding

    def submit_delivery(self, artifact_id=None, notes=""):
        result = super().submit_delivery(artifact_id, notes)
        if result.get("accepted"):
            result["media_checks"] = {
                "requirements_checked": len(self.state.get("requirement_checks", {})),
                "revisions_compared": len(self.state.get("revision_checks", {})),
                "findings_on_delivered_bytes": [
                    fid
                    for fid, f in self.state.get("findings", {}).items()
                    if f["media_sha256"] == result["sha256"]
                ],
                "scope": "Individual findings cover sampled media only. Delivery acceptance checks file specifications.",
            }
            write_json(Path(result["path"]) / "verification.json", result)
        return result

    def _single(self, operation, media, question, *, default_fps=2, **links):
        folder = self._folder()
        excerpt = self._excerpt(
            folder, media, default_fps=default_fps, audio_only=operation == "transcribe_speech"
        )
        return self._observe(folder, operation, question, [excerpt], **links)

    def describe_media(self, media, focus):
        return self._single("describe_media", media, focus, default_fps=1)

    def locate_event(self, media, query, refine=True):
        result = self._single("locate_event", media, query, default_fps=1)
        cover = result["coverage"][0]
        result["refinements"] = []
        if refine and cover["fps"] and cover["fps"] < 8:
            windows = []
            for row in result["evidence"]:
                start, end = row["source_range"]
                window = (
                    max(cover["start"], start - 1 / cover["fps"]),
                    min(cover["end"], end + 1 / cover["fps"]),
                )
                if window not in windows:
                    windows.append(window)
            for start, end in windows[:2]:
                dense = dict(media, start=start, end=end, fps=8)
                if self.state["config"]["frames_per_call"] / (end - start) <= cover["fps"]:
                    continue
                folder = self._folder()
                excerpt = self._excerpt(folder, dense)
                if excerpt["sha256"] != cover["sha256"]:
                    raise ValueError("Source changed between search and refinement")
                result["refinements"].append(
                    self._observe(folder, "locate_event", query, [excerpt], previous=result["finding_id"])
                )
        return result

    def inspect_segment(self, media, question, evidence_frames=False):
        result = self._single("inspect_segment", media, question, default_fps=4)
        if evidence_frames:
            folder = self.root / "perception" / result["finding_id"]
            m = read_json(folder / "finding.json")["media"][0]
            frames = []
            for row in result["evidence"]:
                if row["modality"] != "visual" or len(frames) == 6:
                    continue
                if m["mime"] == "image/jpeg":
                    frames.append({"time": 0, "path": m["file"]})
                    break
                # Seek by sample index on the actual CFR excerpt seen by Gemini.
                index = min(
                    max(0, math.ceil(m["duration"] * m["fps"]) - 1),
                    int((row["start"] + row["end"]) / 2 * m["fps"]),
                )
                path = folder / f"sample-{index}.jpg"
                if any(f["path"] == str(path) for f in frames):
                    continue
                if not path.exists():
                    run_command(
                        [
                            "ffmpeg",
                            "-nostdin",
                            "-v",
                            "error",
                            "-i",
                            m["file"],
                            "-vf",
                            f"select=eq(n\\,{index})",
                            "-frames:v",
                            "1",
                            "-threads",
                            "1",
                            str(path),
                        ],
                        timeout=self.timeout(),
                    )
                if path.is_file():
                    frames.append({"time": m["start"] + index / m["fps"], "path": str(path)})
            result.update(frames=frames, image_paths=[f["path"] for f in frames])
        return result

    def transcribe_speech(self, media):
        source = inside(self.workspace, media["path"])
        meta = streams(source, self.timeout())
        if not any(s["codec_type"] == "audio" for s in meta["streams"]):
            return {
                "provider_called": False,
                "evidence": [],
                "summary": "No audio stream",
                "sha256": digest(source),
            }
        return self._single("transcribe_speech", media, "Transcribe speech and identify the language.")

    def compare_media(self, media, reference=None, finding_id=None, focus=""):
        folder = self._folder()
        if finding_id:
            if finding_id not in self.state.get("findings", {}):
                raise ValueError("Unknown finding in this task")
            prior = read_json(self.root / "perception" / finding_id / "finding.json")
            old = prior["media"][-1]
            if digest(Path(old["file"])) != old["excerpt_sha256"]:
                raise ValueError("Stored excerpt changed")
            question = f"Recheck original question: {prior['question']}. Earlier observation: {prior['summary']}. {focus}"
            mode = "revision"
        else:
            old = self._excerpt(self._folder(), reference)
            question, mode = "Compare these reference attributes: " + focus, "reference"
        new = self._excerpt(folder, media)
        needed = prior.get("required_evidence", []) if finding_id else []
        return self._observe(
            folder,
            "compare_media",
            question,
            [old, new],
            mode=mode,
            previous=finding_id,
            required_evidence=needed,
        )

    def probe_media(self, path, measurements=None):
        source = inside(self.workspace, path)
        info = streams(source, self.timeout())
        result = {
            "provider_called": False,
            "streams": [
                {
                    k: s[k]
                    for k in (
                        "codec_type",
                        "codec_name",
                        "width",
                        "height",
                        "avg_frame_rate",
                        "sample_rate",
                        "channels",
                    )
                    if k in s
                }
                for s in info["streams"]
            ],
            "duration_seconds": float(info["format"]["duration"]) if "duration" in info["format"] else None,
        }
        for metric in dict.fromkeys(measurements or []):
            if metric == "motion":
                result[metric] = self.video_metrics(path)
            elif metric == "loudness":
                result[metric] = self.audio_metrics(path)
            elif metric == "decode":
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
                        str(source),
                        "-map",
                        "0:v?",
                        "-map",
                        "0:a?",
                        "-f",
                        "null",
                        "-",
                    ],
                    timeout=self.timeout(180),
                )
                result[metric] = "passed"
            elif metric == "silence":
                # ametadata writes detector events to stdout. Trim a trailing open interval to EOF.
                out = run_command(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-v",
                        "error",
                        "-i",
                        str(source),
                        "-vn",
                        "-af",
                        "silencedetect=n=-50dB:d=0.25,ametadata=print:file=-",
                        "-f",
                        "null",
                        "/dev/null",
                    ],
                    timeout=self.timeout(120),
                )
                intervals, start = [], None
                for side, value in re.findall(r"lavfi\.silence_(start|end)=([\d.eE+-]+)", out):
                    if side == "start":
                        start = float(value)
                    elif start is not None:
                        intervals.append([start, float(value)])
                        start = None
                if start is not None:
                    # Some containers (raw streams, fragmented files) omit format duration.
                    end = result["duration_seconds"]
                    if end is None:
                        known = [
                            float(s["duration"])
                            for s in info["streams"]
                            if s.get("duration") not in (None, "N/A")
                        ]
                        end = max(known) if known else None
                    intervals.append([start, end])
                result[metric] = {"threshold_db": -50, "minimum_seconds": 0.25, "intervals": intervals}
        return result
