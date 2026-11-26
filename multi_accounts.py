"""多账号配置展开与稳定的数据目录标识，不在目录或日志中暴露手机号。"""
import copy
import hashlib


def account_configs(config):
    entries = config.get("accounts") or []
    if not isinstance(entries, list):
        raise ValueError("accounts 必须为账号列表")
    if not entries:
        return []
    result, phones, names = [], set(), set()
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"accounts 第{index}项必须为对象")
        if not entry.get("enabled", True):
            continue
        phone = str(entry.get("phone") or "").strip()
        password = entry.get("password")
        name = str(entry.get("name") or f"账号{index}").strip()
        if not phone or not password:
            raise ValueError(f"{name} 未填写手机号或密码")
        if phone in phones or name in names:
            raise ValueError("多账号手机号和名称均不能重复")
        phones.add(phone)
        names.add(name)
        cfg = copy.deepcopy(dict(config))
        cfg.pop("accounts", None)
        cfg["account"] = {"phone": phone, "password": password}
        cfg["eligibility"] = dict(cfg.get("eligibility") or {})
        # 不继承旧账号院系：不同账号可能属于不同学院。
        cfg["eligibility"]["college_id"] = str(entry.get("college_id") or "")
        cfg["push"] = dict(cfg.get("push") or {})
        session = str(entry.get("session") or cfg["push"].get("session") or "").strip()
        if not session:
            raise ValueError(f"{name} 必须配置推送会话 session，或设置全局 push.session")
        cfg["push"]["session"] = session
        cfg["_account_name"] = name
        key = hashlib.sha256(phone.encode()).hexdigest()[:24]
        result.append((key, cfg))
    return result
