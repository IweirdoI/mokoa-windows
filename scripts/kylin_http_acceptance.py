"""Assertions over the real HTTP path; preserve every request/response."""
import json
import time
import httpx


def evaluate(out, run_id):
    evidence = {"cases": {}, "requests": []}

    def save():
        (out / "http-assertions.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    with httpx.Client(base_url="http://127.0.0.1:18085", timeout=120, trust_env=False) as client:
        def post(path, payload):
            start = time.perf_counter()
            response = client.post(path, json=payload)
            evidence["requests"].append({"path": path, "payload": payload, "status": response.status_code,
                                         "body": response.text, "wall_ms": (time.perf_counter() - start) * 1000})
            save()
            response.raise_for_status()
            return response.json()

        def require(condition, message):
            if not condition:
                raise RuntimeError(message)

        def store(user, text, metadata=None):
            result = post("/store-entry", {"entry": {"user_id": user, "content": text, "memory_type": "project_context",
                           "source": "manual", "confidence": .95, "metadata": metadata or {}}})
            require(result.get("created"), "Test fixture was not stored")
            return result["memory_id"]

        def recall(user, query):
            return post("/recall", {"user_id": user, "query": query, "top_k": 100})["items"]

        def conflict():
            user = "conflict_" + run_id
            old = store(user, "验收项目服务端口为 8080", {"knowledge_key": "acceptance.service_port"})
            new = store(user, "验收项目服务端口现已改为 9090，替代旧端口 8080", {"knowledge_key": "acceptance.service_port"})
            ids = {item["id"] for item in recall(user, "验收项目当前服务端口")}
            require(new in ids and old not in ids, "New knowledge missing or superseded knowledge still recalled")

        def preference_forget():
            user = "forget_" + run_id
            retained = store(user, "验收项目使用 Python 编程语言")
            old = post("/preferences/extract", {"user_id": user, "text": "下载默认放 D 盘"})
            new = post("/preferences/extract", {"user_id": user, "text": "以后下载改到 E 盘"})
            old_ids = {x["memory_id"] for x in old.get("stored", [])}
            new_ids = {x["memory_id"] for x in new.get("stored", [])}
            require(old_ids and new_ids, "Download preference fixture missing")
            ids = {item["id"] for item in recall(user, "下载路径偏好")}
            require(new_ids <= ids and not (old_ids - new_ids) & ids, "Preference override failed")
            post("/forget", {"user_id": user, "instruction": "忘掉下载路径偏好"})
            items = recall(user, "下载路径偏好及项目语言")
            ids = {x["id"] for x in items}
            require(not (old_ids | new_ids) & ids, "Forgotten preference still recalled")
            require(retained in ids, "Unrelated knowledge lost")

        def phone_forget():
            user = "phone_" + run_id
            retained = store(user, "验收项目使用 Python 编程语言")
            post("/ingest", {"user_id": user, "source": "conversation", "payload": {"content": "我的手机号是 13800138000，请记住"}})
            before = recall(user, "我的手机号")
            post("/forget", {"user_id": user, "instruction": "忘记我的手机号"})
            after = recall(user, "我的手机号和项目语言")
            require("13800138000" not in json.dumps(after, ensure_ascii=False), "Phone still visible")
            require(retained in {x["id"] for x in after}, "Unrelated knowledge lost")
            return {"phone_visible_before": "13800138000" in json.dumps(before, ensure_ascii=False),
                    "note": "If blocked/redacted on ingest, this proves no exposure, not deletion of a stored phone"}

        for name, action in (("knowledge_override", conflict), ("preference_override_and_forget", preference_forget), ("phone_forget", phone_forget)):
            try:
                evidence["cases"][name] = {"status": "passed", "detail": action()}
            except Exception as exc:
                evidence["cases"][name] = {"status": "failed", "error": str(exc)}
            save()
    if any(x["status"] == "failed" for x in evidence["cases"].values()):
        raise RuntimeError("HTTP assertions failed; see http-assertions.json")
    return evidence["cases"]
