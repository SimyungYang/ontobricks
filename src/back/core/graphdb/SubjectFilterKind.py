"""Matching rule selector for :class:`SubjectFilter`."""

from enum import Enum


class SubjectFilterKind(str, Enum):
    """Kind of a :class:`SubjectFilter` condition.

    A ``str`` enum so members compare equal to their plain string values
    (``SubjectFilterKind.DATA == "data"``); filters built from raw strings keep
    working.
    """

    DATA = "data"
    RELATIONSHIP = "relationship"
    ID = "id"
