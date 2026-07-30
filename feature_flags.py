import os


EXPERIENCE_CREATOR_FLAGS = {
    "ENABLE_EXPERIENCE_CREATOR": False,
    "ENABLE_TRIGGER_MANAGEMENT": False,
    "ENABLE_PROCESSING_STATUS_UI": False,
    "ENABLE_EXPERIENCE_QR_ASSET": False,
}


TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}


def flag_enabled(name):
    if name not in EXPERIENCE_CREATOR_FLAGS:
        raise KeyError(name)
    value = os.environ.get(name)
    if value is None:
        return EXPERIENCE_CREATOR_FLAGS[name]
    return str(value).strip().lower() in TRUE_VALUES


def experience_creator_enabled():
    return flag_enabled("ENABLE_EXPERIENCE_CREATOR")


def trigger_management_enabled():
    return experience_creator_enabled() and flag_enabled("ENABLE_TRIGGER_MANAGEMENT")


def processing_status_ui_enabled():
    return experience_creator_enabled() and flag_enabled("ENABLE_PROCESSING_STATUS_UI")


def experience_qr_asset_enabled():
    return experience_creator_enabled() and flag_enabled("ENABLE_EXPERIENCE_QR_ASSET")


def experience_creator_flag_snapshot():
    return {name: flag_enabled(name) for name in EXPERIENCE_CREATOR_FLAGS}
