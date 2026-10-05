"""Per-thread settings, with legacy per-window values as migration defaults."""


def clean_sessions(value):
    result = {}
    if not isinstance(value, dict):
        return result
    for thread_id, raw in value.items():
        if not isinstance(raw, dict) or not str(thread_id).strip():
            continue
        item = {}
        for key in ("model", "effort", "after_finish"):
            field = raw.get(key)
            if isinstance(field, str) and "\n" not in field and "\r" not in field:
                item[key] = field.strip()
        if isinstance(raw.get("excluded"), bool):
            item["excluded"] = raw["excluded"]
        if "loops" in raw:
            try:
                item["loops"] = max(-1, min(10000, int(raw["loops"])))
            except (TypeError, ValueError, OverflowError):
                pass
        if item:
            result[str(thread_id)] = item
    return result


def session_options(config, thread_id="", title=""):
    try:
        loops = int(config["after_finish_loops"].get(title, 1))
    except (TypeError, ValueError):
        loops = 1
    options = {"model": config["model_overrides"].get(title, ""),
               "effort": config["effort_overrides"].get(title, ""),
               "excluded": title in config["excluded"],
               "after_finish": config["after_finish"].get(title, ""),
               "loops": loops}
    if thread_id:
        options.update(config.get("sessions", {}).get(thread_id, {}))
    return options
