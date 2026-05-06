"""Multi-tier classifier: regex (existing) + signal fusion + per-user dict + Tier 2 host LLM.

This package extends the existing `agent_doctor.frustration` module with:
- signal_fusion: typing-shape, trajectory, repeat-theme signals
- user_dict: per-user "do flag" / "do not flag" phrase memory
- tier2: host-inference second pass for borderline cases

Tier 1 (regex) lives in agent_doctor.frustration and is consumed here.
"""

from .signal_fusion import (
    SignalScores,
    score_typing_shape,
    score_trajectory,
    score_repeat_themes,
    fuse_signals,
)
from .tier2 import Tier2Result, tier2_classify
from .user_dict import UserDict, load_user_dict, save_user_dict

__all__ = [
    "SignalScores",
    "score_typing_shape",
    "score_trajectory",
    "score_repeat_themes",
    "fuse_signals",
    "Tier2Result",
    "tier2_classify",
    "UserDict",
    "load_user_dict",
    "save_user_dict",
]
