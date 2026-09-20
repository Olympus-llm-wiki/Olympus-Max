"""Versioned provider observations. A parsed observation is not verified truth."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import ReelError


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Profile(Model):
    schema_version: int = 1
    speech_model: str = "gemini-3.8-flash-high"
    visual_model: str = "gemini-3.8-flash-high"
    reconcile_model: str = "gemini-3.8-flash-high"
    backend: Literal["antigravity"] = "antigravity"
    timeout_s: int = Field(default=600, ge=30, le=1800)
    min_quota: float = Field(default=0.1, ge=0, le=1)
    language: str = "ru"
    video_fps: float = Field(default=4, ge=1, le=25)
    target_output_tokens: int = Field(default=12000, ge=512, le=32000)
    max_refinements: int = Field(default=2, ge=0, le=4)
    max_duration_s: float = Field(default=180, gt=0, le=300)
    max_bytes: int = Field(default=100 * 1024 * 1024, gt=0, le=200 * 1024 * 1024)

    @model_validator(mode="after")
    def gemini_models_only(self):
        for model in (self.speech_model, self.visual_model, self.reconcile_model):
            if not model.startswith("gemini-") or not all(c.isalnum() or c in ".-_" for c in model):
                raise ValueError("use_a_gemini_cli_model_profile")
        return self


class Span(Model):
    start_s: float | None
    end_s: float | None

    @model_validator(mode="after")
    def ordered(self):
        if (self.start_s is None) != (self.end_s is None):
            raise ValueError("both_timestamps_or_neither")
        if self.start_s is not None and (self.start_s < 0 or self.end_s < self.start_s):
            raise ValueError("invalid_interval")
        return self


class Gap(Span):
    track: Literal["speech", "visual", "text", "links", "course"]
    reason: str


class TimedObservation(Span):
    id: str
    timing_uncertainty_s: float | None


class SpeechSegment(TimedObservation):
    text: str


class Speech(Model):
    status: Literal["observed", "no_speech_detected", "unavailable"]
    segments: list[SpeechSegment]
    gaps: list[Gap]


class Layer(Model):
    id: str
    role: Literal["background", "speaker", "speaker_frame", "heading", "captions", "graphic", "graphic_text", "insert", "branding", "other"]
    description: str


class Scene(TimedObservation):
    description: str


class Box(Model):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=0, le=1)
    height: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def inside(self):
        if self.x + self.width > 1.001 or self.y + self.height > 1.001:
            raise ValueError("bbox_outside_frame")
        return self


class TextEvent(TimedObservation):
    layer_id: str
    text: str | None
    appearance: str
    bbox: Box | None


class VisualEvent(TimedObservation):
    layer_id: str
    kind: Literal["cut", "transition", "entrance", "exit", "motion", "layout", "hold", "other"]
    description: str


class Visual(Model):
    summary: str
    layers: list[Layer]
    scenes: list[Scene]
    text_events: list[TextEvent]
    visual_events: list[VisualEvent]
    gaps: list[Gap]


class Link(Model):
    speech_id: str
    event_id: str
    relation: str


class Reconcile(Model):
    summary: str
    links: list[Link]
    gaps: list[Gap]


class CourseInterpretation(Model):
    event_id: str
    source_id: str
    explanation: str


class Course(Model):
    interpretations: list[CourseInterpretation]
    gaps: list[Gap]


SCHEMAS = {"speech": Speech, "visual": Visual, "reconcile": Reconcile, "course": Course}


def validate_observation(kind, data, duration, context=None):
    value = SCHEMAS[kind].model_validate(data).model_dump()
    ids = []
    for key in ("segments", "scenes", "text_events", "visual_events", "gaps"):
        for event in value.get(key, []):
            if event.get("end_s") is not None and event["end_s"] > duration + .02:
                raise ReelError("interval_outside_input")
            if "id" in event:
                ids.append(event["id"])
    if len(ids) != len(set(ids)):
        raise ReelError("duplicate_event_id")
    if kind == "visual":
        layer_ids = [x["id"] for x in value["layers"]]
        if len(layer_ids) != len(set(layer_ids)):
            raise ReelError("duplicate_layer_id")
        if any(x["layer_id"] not in layer_ids for x in value["text_events"] + value["visual_events"]):
            raise ReelError("unknown_layer_id")
    if kind in ("reconcile", "course"):
        context = context or {}
        events = {e["id"] for e in context.get("visual", {}).get("visual_events", []) + context.get("visual", {}).get("text_events", []) + context.get("visual", {}).get("scenes", [])}
        speech = {e["id"] for e in context.get("speech", {}).get("segments", [])}
        if kind == "reconcile" and any(e["event_id"] not in events or e["speech_id"] not in speech for e in value["links"]):
            raise ReelError("unknown_link_id")
        if kind == "course" and any(e["event_id"] not in events or e["source_id"] not in context.get("course_ids", []) for e in value["interpretations"]):
            raise ReelError("unknown_course_reference")
    # Deliberately isolated passes cannot assess coverage of another modality.
    # Keep the provider's untouched answer in raw, but do not turn e.g. a visual
    # reader's "I did not hear audio" into a request to retranscribe speech.
    tracks = {"speech": {"speech"}, "visual": {"visual", "text"}, "course": {"course"}}
    if kind in tracks:
        value["gaps"] = [g for g in value["gaps"] if g["track"] in tracks[kind]]
    return value
