"""
DEPRECATED: `config.py` is no longer required. Feature flags are
persisted in `feature_flags.json` and loaded at app startup. This
module is kept as a no-op for compatibility but may be removed.
"""

class Config:
    FEATURE_FLAGS = {}
