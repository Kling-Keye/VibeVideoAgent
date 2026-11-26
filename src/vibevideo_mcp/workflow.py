"""Prompt-grounded production plan and evidence tracking for the video production."""

from pathlib import Path
import time
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from .io import inside, write_json
from .media_service import MediaService
from .media_tools import Media
from .schema import Model

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Modality = Literal["visual", "audio"]


class Requirement(Model):
    id: Text
    prompt_quote: Text = Field(description="Exact public prompt words supporting this requirement.")
    observable: Text = Field(description="What the viewer must actually see or hear.")
    evidence: list[Modality] = Field(min_length=1, max_length=2)


class Beat(Model):
    action: Text = Field(description="What to create or edit in this shot, sequence or production unit.")
    requirements: list[Text] = Field(min_length=1)
    assets: list[str] = Field(
        default_factory=list, description="Workspace-relative supplied inputs needed by this unit."
    )


class Plan(Model):
    goal: Text
    deliverables: list[Text] = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=1)
    beats: list[Beat] = Field(
        min_length=1, description="Ordered production units. Group related shots as needed."
    )
    creative_choices: list[Text] = Field(default_factory=list)
    first_requirement: Text
    preview: Text = Field(description="Smallest representative render that tests first_requirement.")
    risk: Text = Field(description="Which later production decision depends on this preview?")

    @model_validator(mode="after")
    def linked(self):
        keys = {r.id for r in self.requirements}
        used = {key for beat in self.beats for key in beat.requirements}
        if len(keys) != len(self.requirements) or used != keys or self.first_requirement not in keys:
            raise ValueError(
                "Give unique requirements; link every one to production beats and choose an existing first_requirement"
            )
        return self


class PlannedCheck(Model):
    requirement_id: Text
    media: Media
    stage: Literal["preview", "assembly", "final"]


class WorkflowService(MediaService):
    tool_names = MediaService.tool_names | {"plan_production"}

    def plan_production(self, **args):
        plan = Plan.model_validate(args)
        prompt = " ".join(self.state["task"]["prompt"].split())
        for req in plan.requirements:
            if " ".join(req.prompt_quote.split()) not in prompt:
                raise ValueError(f"{req.id}: quote must appear in the supplied prompt")
        for beat in plan.beats:
            for name in beat.assets:
                inside(self.workspace, name)
        existing = self.state.get("production_plan")
        if existing and existing["plan"] != plan.model_dump():
            raise ValueError("Initial decomposition is already recorded; adapt execution using findings")
        if not existing:
            self.state["production_plan"] = {
                "plan": plan.model_dump(),
                "created_at": time.time(),
                "delivery_spec": self.state["task"]["delivery"],
            }
        return {
            **self.state["production_plan"],
            "next": f"Resolve asset questions, render {plan.preview}, then check {plan.first_requirement} at stage=preview.",
        }

    def check_requirement(self, media, requirement_id, stage):
        plan = self.state.get("production_plan", {}).get("plan")
        if not plan:
            raise ValueError("Call plan_production first")
        req = next((r for r in plan["requirements"] if r["id"] == requirement_id), None)
        if not req:
            raise ValueError("Unknown planned requirement")
        inputs = {
            inside(self.workspace, name, exists=False) for beat in plan["beats"] for name in beat["assets"]
        }
        if inside(self.workspace, media["path"]) in inputs:
            raise ValueError(
                "Check a produced preview or delivery. Inspect input assets with the source tools."
            )
        return self._single(
            "check_requirement",
            media,
            req["observable"],
            requirement_source=req["prompt_quote"],
            required_evidence=req["evidence"],
            requirement_id=requirement_id,
            stage=stage,
        )

    def _observe(self, folder, operation, question, media, **links):
        result = super()._observe(folder, operation, question, media, **links)
        checks = self.state.setdefault("workflow_checks", {})
        if operation == "check_requirement":
            checks[result["finding_id"]] = {
                "requirement_id": links["requirement_id"],
                "stage": links["stage"],
                "sha256": media[-1]["sha256"],
                "verdict": result["verdict"],
            }
        elif operation == "compare_media" and links.get("previous") in checks:
            parent = checks[links["previous"]]
            checks[result["finding_id"]] = {
                "requirement_id": parent["requirement_id"],
                "stage": "revision",
                "sha256": media[-1]["sha256"],
                "verdict": result["verdict"],
                "previous": links["previous"],
            }
        if operation == "check_requirement":
            result["next"] = {
                "pass": "Continue producing the linked beats. Revisit this requirement if later edits affect it.",
                "fail": "Use the evidence to locate the problem, edit the project, render again, then compare_media with this finding_id.",
                "uncertain": "Identify missing coverage. Narrow the interval, adjust sampling or supply the missing modality before judging.",
            }[result["verdict"]]
        return result

    def status(self):
        return {
            **super().status(),
            "production_plan": self.state.get("production_plan"),
            "workflow_checks": self.state.get("workflow_checks", {}),
        }

    def submit_delivery(self, artifact_id=None, notes=""):
        plan = self.state.get("production_plan", {}).get("plan")
        if not plan:
            raise ValueError("Record the prompt decomposition before delivery")
        checks = self.state.get("workflow_checks", {})
        preview_checked = any(
            c["stage"] == "preview" and c["requirement_id"] == plan["first_requirement"]
            for c in checks.values()
        )
        if not preview_checked and not notes.strip():
            return {
                "accepted": False,
                "reason": "Check the planned preview or explain in notes why this checkpoint was skipped.",
            }
        result = super().submit_delivery(artifact_id, notes)
        if result.get("accepted"):
            result["workflow"] = {
                "first_requirement": plan.get("first_requirement"),
                "planned_preview_checked": preview_checked,
                "preview_exception": None if preview_checked else notes,
                "final_evidence": {
                    r["id"]: {
                        fid: c["verdict"]
                        for fid, c in checks.items()
                        if c["requirement_id"] == r["id"] and c["sha256"] == result["sha256"]
                    }
                    for r in plan.get("requirements", [])
                },
            }
            self.state["delivery"] = result
            write_json(Path(result["path"]) / "verification.json", result)
        return result
