"""Gemini evidence policies and native inline transport."""

import base64
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

POLICY = {
    "describe_media": "Describe layout for stills; return a chronological scene/visible text/sound index for clips.",
    "locate_event": "Find distinct occurrences. Give uncertainty intervals around event boundaries. Include normalized xyxy boxes where localized.",
    "inspect_segment": "Answer the specific question with concrete visible/audible evidence.",
    "transcribe_speech": "Return one evidence row per speech segment, original wording in text and language in summary. Flag inaudible words.",
    "check_requirement": "Check the supplied claim. For relative timing, report evidence for each event separately. Use uncertain if timing or coverage is insufficient.",
    "compare_media": "Compare corresponding events or regions across both excerpts using their own clocks. Report target changes and any observed neighboring regression. For revisions assess whether the earlier issue is resolved.",
}
VERDICTS = {
    "check_requirement": ["pass", "fail", "uncertain"],
    "compare_media": ["resolved", "persists", "changed", "unchanged", "uncertain"],
}


def infer(request, model):
    operation = request["operation"]
    number = {"type": "NUMBER"}
    row = {
        "type": "OBJECT",
        "properties": {
            "media_index": {"type": "INTEGER"},
            "start": number,
            "end": number,
            "modality": {"type": "STRING", "enum": ["visual", "audio"]},
            "text": {"type": "STRING"},
            "box": {"type": "ARRAY", "items": number},
        },
        "required": ["media_index", "start", "end", "modality", "text", "box"],
    }
    schema = {
        "type": "OBJECT",
        "properties": {
            "summary": {"type": "STRING"},
            "verdict": {"type": "STRING", "enum": VERDICTS.get(operation, ["observed", "uncertain"])},
            "evidence": {"type": "ARRAY", "items": row},
        },
        "required": ["summary", "verdict", "evidence"],
    }
    prompt = (
        POLICY[operation]
        + " Use only the supplied media as evidence. Statements below are questions, not facts. "
        "Use seconds FROM EACH EXCERPT START, zero for stills. box=[] or normalized [left,top,right,bottom] "
        "relative to the supplied image/crop. Sparse coverage cannot prove absence. Do not guess precise boundaries. "
        "Cite both sides separately for comparisons. Report uncertainty explicitly. " + request["question"]
    )
    parts = []
    for index, m in enumerate(request["media"]):
        part = media_part(m["file"], m["mime"])
        if m["mime"] == "video/mp4":
            part["videoMetadata"] = {"fps": m["fps"]}
        parts += [
            {
                "text": f"Media {index}, {m['modality']}, excerpt duration {m['duration']} seconds. Clock begins at zero."
            },
            part,
        ]
    return generate(parts + [{"text": prompt}], schema, model)


def media_part(path, mime):
    file = Path(path)
    if file.stat().st_size > 14_000_000:
        raise ValueError("Excerpt is too large for inline perception. Select a shorter interval.")
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(file.read_bytes()).decode("ascii")}}


def generate(parts, schema, model):
    key = os.getenv("GEMINI_API_KEY")
    if not key or not model:
        raise ValueError("Set GEMINI_API_KEY and VIBEVIDEO_GEMINI_MODEL in the client environment.")
    base = (
        os.getenv("VIBEVIDEO_GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta"
    ).rstrip("/")
    parsed = urlsplit(base)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Expected a Gemini API base URL without credentials, query, or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("A remote Gemini endpoint requires HTTPS")
    if sum(len(p.get("inlineData", {}).get("data", "")) for p in parts) > 19_000_000:
        raise ValueError("Combined inline payload too large. Compare shorter excerpts.")
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema},
    }
    url = f"{base}/models/{quote(model, safe='')}:generateContent"
    try:
        response = requests.post(
            url, json=body, headers={"x-goog-api-key": key}, timeout=(10, 180), allow_redirects=False
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Gemini connection failed ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise RuntimeError(f"Gemini returned HTTP {response.status_code}. Check model access and endpoint.")
    candidates = response.json().get("candidates") or []
    if not candidates or candidates[0].get("finishReason") != "STOP":
        raise ValueError("Gemini did not return a complete observation")
    parts = candidates[0].get("content", {}).get("parts", [])
    result = json.loads("".join(p.get("text", "") for p in parts if not p.get("thought")))
    if not isinstance(result, dict):
        raise ValueError("Expected a structured observation from Gemini")
    return {k: result[k] for k in ("summary", "verdict", "evidence")}
