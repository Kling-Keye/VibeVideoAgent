"""Public production contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DeliverySpec(Model):
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    fps: float | None = Field(default=None, gt=0)
    min_duration: float | None = Field(default=None, ge=0)
    max_duration: float | None = Field(default=None, gt=0)
    audio_required: bool = False

    @model_validator(mode="after")
    def ordered(self):
        if (
            self.min_duration is not None
            and self.max_duration is not None
            and self.min_duration > self.max_duration
        ):
            raise ValueError("min_duration exceeds max_duration")
        return self
