#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tasks Subpackage

Task execution managers for QA, Stress, and Browser tasks.
"""

from .qa import QATaskManager
from .stress import StressTaskManager
from .browser import BrowserTaskManager

__all__ = [
    'QATaskManager',
    'StressTaskManager',
    'BrowserTaskManager',
]