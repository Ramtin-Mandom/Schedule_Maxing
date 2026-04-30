from pydantic import BaseModel, Field
from typing import Literal, Optional, List


class Task(BaseModel):
    name: str = Field(min_length=1)
    category: str
    tag: str
    fixed: bool
    duration: int = Field(gt=0)
    priority: int = Field(ge=1, le=10)
    preference_time: Literal["morning", "afternoon", "evening"]
    start_time: Optional[int] = None  # only if fixed=True


class FixedBlock(BaseModel):
    name: str
    start_time: int
    end_time: int
    category: str = "fixed"


class TimeWindow(BaseModel):
    start_time: int
    end_time: int


class ScheduleInput(BaseModel):
    date: str
    day_start: int = 480   # 8:00 AM
    day_end: int = 1320    # 10:00 PM
    fixed_blocks: List[FixedBlock]
    tasks: List[Task]


class ScheduledTask(BaseModel):
    name: str
    category: str
    tag: str
    start_time: int
    end_time: int
    score: float


class ScheduleOutput(BaseModel):
    date: str
    total_score: float
    scheduled_tasks: List[ScheduledTask]
    unscheduled_tasks: List[Task]