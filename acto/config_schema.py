from typing import Literal, List

from pydantic import BaseModel


class AlarmsConfig(BaseModel):
    # trigger an alarm if the checker returns an invalid input
    invalid_input: bool = True
    warning_in_operator_logs: bool = False


class IOConfig(BaseModel):
    write_result_each_generation: bool = True


class NotificationsConfig(BaseModel):
    enabled: bool = False


class StateCheckerAnalysisMode(BaseModel):
    dependency: bool = False
    taint: bool = False


class StateCheckerConfig(BaseModel):
    enable_canonicalization: bool = True
    enable_default_value_comparison: bool = True
    analysis: StateCheckerAnalysisMode


class CheckersConfig(BaseModel):
    state: StateCheckerConfig

class ModeConfig(BaseModel):
    mode: Literal['whitebox', 'blackbox'] = 'whitebox'

class Config(BaseModel):
    alarms: AlarmsConfig
    checkers: CheckersConfig
    mode: ModeConfig
    notifications: NotificationsConfig
    io: IOConfig
