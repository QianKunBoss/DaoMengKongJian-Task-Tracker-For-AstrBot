"""Exercise the actual plugin class without installing/running AstrBot."""
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import time
import types

import analyzer
import dmkj_client
import eligibility
import reminders


def load_plugin():
    tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DmkjFinderPlugin")
    cls.decorator_list = []
    for method in cls.body:
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method.decorator_list = [d for d in method.decorator_list if isinstance(d, ast.Name) and d.id == "staticmethod"]

    class Star:
        def __init__(self, context):
            self.context = context

    logger = types.SimpleNamespace(**{k: lambda *a, **kw: None for k in ("info", "warning", "debug", "exception", "error")})
    class Node:
        def __init__(self, content, **kwargs):
            self.content = content
            self.__dict__.update(kwargs)

    class Nodes:
        def __init__(self, nodes):
            self.nodes = nodes

    ns = {"Star": Star, "logger": logger, "asyncio": asyncio, "time": time,
          "os": os, "json": json, "datetime": datetime, "timedelta": timedelta,
          "ThreadPoolExecutor": ThreadPoolExecutor, "PLUGIN_VERSION": "test",
          "PollingPausedError": type("PollingPausedError", (RuntimeError,), {}),
          "Image": None, "MessageChain": list, "Plain": str, "MessageEventResult": None}
    ns.update(Node=Node, Nodes=Nodes)
    ns.update({k: getattr(analyzer, k) for k in ("judge", "judge_rule", "build_user_prompt", "parse_llm_result")})
    ns.update({k: getattr(dmkj_client, k) for k in ("DmkjClient", "SessionExpiredError")})
    ns.update({k: getattr(eligibility, k) for k in ("DETAIL_FIELDS", "STATES", "judge_eligibility", "eligibility_text", "eligibility_key", "recommendable", "college_blocked")})
    ns.update({k: getattr(reminders, k) for k in ("ReminderStore", "reminder_options", "signup_timestamp", "CHINA_TZ")})
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, cls], type_ignores=[]))
    exec(compile(module, "main.py", "exec"), ns)
    return ns[cls.name], ns
