"""Backward-compatible imports for integrations that used the old module."""

from .transmission import TransmissionClient as Aria2Client
from .transmission import TransmissionError as Aria2Error
from .transmission import is_metadata_file

__all__ = ["Aria2Client", "Aria2Error", "is_metadata_file"]

