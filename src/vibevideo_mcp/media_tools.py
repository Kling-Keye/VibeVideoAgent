"""Production-facing VibeVideo tools and their MCP contracts."""

from typing import Literal

from pydantic import Field, model_validator
from .schema import Model


class Media(Model):
    path: str = Field(min_length=1, description="Workspace-relative asset, draft, still or audio file.")
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)
    timeline_offset: float | None = Field(
        default=None, description="Final timeline seconds at source time zero, if known."
    )
    modality: Literal["auto", "visual", "audio", "audiovisual"] = "auto"
    fps: float | None = Field(default=None, gt=0, le=16)
    crop: list[int] | None = Field(
        default=None, min_length=4, max_length=4, description="Source pixels [x,y,width,height]."
    )
    source_files: list[str] = Field(
        default_factory=list, max_length=32, description="Optional editable files to fingerprint."
    )


class Describe(Model):
    media: Media
    focus: str = "Describe the structure and major contents."


class Locate(Model):
    media: Media
    query: str = Field(min_length=1)
    refine: bool = Field(
        default=True, description="Densely revisit up to two candidate windows in a sparse video search."
    )


class Inspect(Model):
    media: Media
    question: str = Field(min_length=1)
    evidence_frames: bool = False


class Transcript(Model):
    media: Media


class Compare(Model):
    media: Media
    reference: Media | None = None
    finding_id: str | None = None
    focus: str = ""

    @model_validator(mode="after")
    def one_baseline(self):
        if bool(self.reference) == bool(self.finding_id):
            raise ValueError("Provide exactly one of reference and finding_id")
        if self.reference and not self.focus.strip():
            raise ValueError("Specify the reference attributes to compare")
        return self


class Probe(Model):
    path: str
    measurements: list[Literal["decode", "motion", "loudness", "silence"]] = Field(default_factory=list)


TOOLS = {
    "describe_media": (
        Describe,
        "Describe actual asset layout or sparse audiovisual timeline. For navigation; coverage is sampled.",
    ),
    "locate_event": (
        Locate,
        "Search for occurrences, uncertain time boundaries and visual regions; refine sparse video candidates.",
    ),
    "inspect_segment": (
        Inspect,
        "Examine an actual draft, clip, still or crop with the configured perception model; optionally return representative evidence frames.",
    ),
    "transcribe_speech": (
        Transcript,
        "Transcribe speech in original language with time ranges. Sends only audio.",
    ),
    "compare_media": (
        Compare,
        "Compare reference and draft on requested attributes, or revisit a finding against a new draft. Each side has its own clock.",
    ),
    "probe_media": (
        Probe,
        "Read stream metadata and optional decode, repeated-frame/seam, loudness or silence measurements locally.",
    ),
}
