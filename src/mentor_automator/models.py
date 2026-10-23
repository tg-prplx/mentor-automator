from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class State(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    AWAITING_FINAL_CONFIRMATION = "AWAITING_FINAL_CONFIRMATION"
    SUBMITTING = "SUBMITTING"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    PLATFORM_CLOSED = "PLATFORM_CLOSED"


class Quiz(BaseModel):
    id: str
    course: str
    title: str
    url: str
    available: bool | None = None
    deadline: str = "Не указан в интерфейсе"
    time_limit: str = "Не указан в интерфейсе"
    attempts_remaining: int | None = None
    restrictions: str = ""


class Option(BaseModel):
    id: str  # ephemeral local index, never Moodle value/name or URL
    text: str
    reusable: bool = False
    capacity: int = Field(1, ge=0, le=100)


class Slot(BaseModel):
    id: str
    text: str
    kind: str
    options: list[Option] = Field(default_factory=list)


class Assignment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    slot: str
    choices: list[str]
    text: str


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    choice: str
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class Question(BaseModel):
    id: str
    kind: str
    text: str
    options: list[Option] = Field(default_factory=list)
    slots: list[Slot] = Field(default_factory=list)
    visual: bool = False
    fingerprint: str = ""
    asset_signature: str = ""  # local image/vector change detection; excluded from payload()

    def payload(self):
        return self.model_dump(include={"kind", "text", "options", "slots"})


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    choices: list[str]
    text: str = Field(max_length=2000)
    explanation: str = Field(max_length=1200)
    confidence: float = Field(ge=0, le=1)
    skip: bool
    assignments: list[Assignment] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    placements: list[Placement] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        if len(self.choices) != len(set(self.choices)):
            raise ValueError("Duplicate choices")
        return self

    def validate_for(self, question: Question):
        if self.skip:
            return
        if question.kind == "markers":
            if self.choices or self.text or self.order or self.assignments or not self.placements:
                raise ValueError("Markers require placements")
            available = {option.id: option for option in question.options}
            used = [p.choice for p in self.placements]
            if not set(used) <= available.keys() or len(self.placements) > 100:
                raise ValueError("Unknown marker")
            for option in question.options:
                if option.capacity and used.count(option.id) > option.capacity:
                    raise ValueError("Too many marker copies")
            return
        if self.placements:
            raise ValueError("Unexpected placements")
        if question.kind in {"matching", "embedded", "dragdrop"}:
            if self.choices or self.text or self.order:
                raise ValueError("Use assignments for slots")
            if len(self.assignments) != len(question.slots):
                raise ValueError("Every slot requires an answer")
            by_id = {item.slot: item for item in self.assignments}
            if set(by_id) != {slot.id for slot in question.slots}:
                raise ValueError("Unknown or duplicate slot")
            used = []
            for slot in question.slots:
                item = by_id[slot.id]
                if len(item.choices) != len(set(item.choices)):
                    raise ValueError("Duplicate slot choice")
                if slot.kind in {"select", "single", "multiple", "drop"}:
                    allowed = {option.id for option in slot.options}
                    if not item.choices or not set(item.choices) <= allowed or item.text:
                        raise ValueError("Invalid slot choice")
                    if slot.kind != "multiple" and len(item.choices) != 1:
                        raise ValueError("One slot choice required")
                    if question.kind == "dragdrop":
                        used.extend(item.choices)
                elif slot.kind in {"text", "numeric"}:
                    if not item.text.strip() or item.choices:
                        raise ValueError("Slot text required")
                    if slot.kind == "numeric":
                        self.finite(item.text)
                else:
                    raise ValueError("Unsupported slot")
            for option in question.options:
                if not option.reusable and used.count(option.id) > 1:
                    raise ValueError("Drag choice is not reusable")
            return
        if self.assignments:
            raise ValueError("Unexpected assignments")
        if question.kind == "ordering":
            if self.text or self.choices or set(self.order) != {o.id for o in question.options}:
                raise ValueError("Ordering requires every item")
            if len(self.order) != len(set(self.order)):
                raise ValueError("Duplicate ordered item")
            return
        if self.order:
            raise ValueError("Unexpected ordering")
        valid = {o.id for o in question.options}
        if not set(self.choices) <= valid:
            raise ValueError("Unknown option")
        if question.kind == "single" and len(self.choices) != 1:
            raise ValueError("Single choice required")
        if question.kind == "multiple" and not self.choices:
            raise ValueError("At least one choice required")
        if question.kind in {"text", "numeric"} and (not self.text.strip() or self.choices):
            raise ValueError("Text required")
        if question.kind == "numeric":
            self.finite(self.text)

    @staticmethod
    def finite(text):
        from decimal import Decimal, InvalidOperation

        try:
            if not Decimal(text.replace(",", ".")).is_finite():
                raise ValueError("Finite number required")
        except InvalidOperation:
            raise ValueError("Finite number required") from None

    def display(self, question=None):
        options = {o.id: o.text for o in question.options} if question else {}
        if self.placements:
            return "; ".join(
                f"{options.get(p.choice, p.choice)}: ({p.x:.3f}, {p.y:.3f})" for p in self.placements
            )
        if self.order:
            return " → ".join(options.get(item, item) for item in self.order)
        if self.assignments:
            slots = {s.id: s for s in question.slots} if question else {}
            parts = []
            for item in self.assignments:
                slot = slots.get(item.slot)
                names = {o.id: o.text for o in slot.options} if slot else {}
                value = ", ".join(names.get(choice, choice) for choice in item.choices) or item.text
                parts.append(f"{slot.text if slot else item.slot}: {value}")
            return "; ".join(parts)
        return ", ".join(options.get(item, item) for item in self.choices) or self.text
