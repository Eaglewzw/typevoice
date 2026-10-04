"""服务商错误文案映射 — 与 mac 的 AIPolisher.extractAPIErrorMessage / friendlyProviderError 一致。

把百炼 DashScope / 火山 Ark 最常见的错误翻成能照着办的中文；认不出的保留原话。
"""

def friendly_provider_error(code, type_, message) -> str | None:
    c = (code or "").lower()
    t = (type_ or "").lower()
    m = (message or "").lower()
    tag = f"（{code}）" if code else ""

    if (c in ("invalid_api_key", "invalidapikey", "authenticationerror") or t == "unauthorized"
            or "incorrect api key" in m or "api key format is incorrect" in m
            or "didn't provide an api key" in m or "invalid api key" in m):
        return f"API Key 无效，请检查是否复制完整{tag}"
    if (c in ("arrearage", "accountoverdueerror", "insufficient_quota")
            or "in good standing" in m or "arrearage" in m or "overdue" in m
            or "quota exceeded" in m or "enough balance" in m):
        return f"账号欠费或免费额度已用完，请到服务商控制台检查{tag}"
    if (c in ("quotaexceeded", "ratelimitexceeded", "throttling", "limit_requests")
            or c.startswith("throttling.") or "rate limit" in m or "too many requests" in m):
        return f"请求太频繁或额度超限，稍后再试{tag}"
    return None


def extract_api_error_message(json_obj) -> str | None:
    if not isinstance(json_obj, dict):
        return None
    err = json_obj.get("error")
    if isinstance(err, dict):
        code, type_, m = err.get("code"), err.get("type"), err.get("message")
        friendly = friendly_provider_error(code, type_, m)
        if friendly:
            return friendly
        if isinstance(m, str) and m:
            return m
        if isinstance(code, str) and code:
            return code
    elif isinstance(err, str) and err:
        return friendly_provider_error(None, None, err) or err
    m = json_obj.get("message")
    if isinstance(m, str) and m:
        code = json_obj.get("code")
        friendly = friendly_provider_error(code, None, m)
        if friendly:
            return friendly
        return f"{m}（{code}）" if code else m
    return None
