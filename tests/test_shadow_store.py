from config.settings import settings
from src.db.models import ShadowStrategyRow, ShadowParamChange

def test_tables_and_settings_exist():
    assert ShadowStrategyRow.__tablename__ == "shadow_strategies"
    assert {"key", "params", "learning_strength", "enabled", "copied_from", "is_preset",
            "closed_at_last_learn"} <= set(ShadowStrategyRow.__table__.columns.keys())
    assert ShadowParamChange.__tablename__ == "shadow_param_changes"
    assert settings.SHADOW_LEARN_MIN_TRADES == 10 and settings.SHADOW_MAX_STRATEGIES == 12
