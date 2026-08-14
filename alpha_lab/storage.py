"""因子存储: 表达式 + 回测结果落盘 JSONL, 支持列表/加载/删除"""
import json
import os
import time

STORAGE_FILE = "factors.jsonl"   # 相对 data/alpha/


def _storage_path(base_dir):
    d = os.path.join(base_dir, "data", "alpha")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, STORAGE_FILE)


def _load_all(base_dir):
    path = _storage_path(base_dir)
    items = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return items


def save_factor(base_dir, name, expr, params, metrics):
    """保存因子 (同名覆盖)"""
    item = {
        "name": name,
        "expr": expr,
        "params": params,
        "metrics": metrics,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    items = [it for it in _load_all(base_dir) if it["name"] != name]
    items.append(item)
    path = _storage_path(base_dir)
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return item


def list_factors(base_dir):
    return _load_all(base_dir)


def get_factor(base_dir, name):
    for it in _load_all(base_dir):
        if it["name"] == name:
            return it
    return None


def delete_factor(base_dir, name):
    items = [it for it in _load_all(base_dir) if it["name"] != name]
    path = _storage_path(base_dir)
    with open(path, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return True
