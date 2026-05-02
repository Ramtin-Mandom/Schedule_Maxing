from pydantic import BaseModel, Field
from typing import  Optional,  Dict


class TimeWindow(BaseModel):
    start_time: int
    end_time: int

class Task(BaseModel):
    name: str = Field(min_length=1)
    date: int
    category: str
    tag: str
    fixed: bool
    duration: int = Field(gt=0)
    priority: int = Field(ge=1, le=10)
    preference_time: TimeWindow 
    dependencies: list[str] = Field(default_factory=list)


class FixedBlock(BaseModel):
    name: str
    time_window: TimeWindow
    category: str = "fixed"



class DaySchedule(BaseModel):
    time_window: TimeWindow = TimeWindow(start_time=480, end_time=1320)
    fixed_blocks: list[FixedBlock]
    tasks: list[Task]

class ScheduleInput(BaseModel):
    schedules: Dict[int, DaySchedule]  # key = date (int)

class ScheduledTask(BaseModel):
    name: str
    category: str
    tag: str
    time_window: TimeWindow
    score: float
class UnscheduledTask(BaseModel):
    name: str 
    reason: str


class DayScheduleOutput(BaseModel):
    date: int
    total_score: float
    scheduled_tasks: list[ScheduledTask]
    unscheduled_tasks: list[UnscheduledTask]


class ScheduleOutput(BaseModel):
    schedules: dict[int, DayScheduleOutput]