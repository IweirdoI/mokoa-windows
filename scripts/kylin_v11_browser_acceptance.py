#!/usr/bin/env python3
"""Host browser acceptance against the live Vue workspace (not demo fixtures)."""
from __future__ import annotations

import json
import os
import platform
import socket
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:5173"
API = "http://127.0.0.1:18080"
DEMO_IDS = {
    "pref-language-v2", "pref-language-v1", "pref-download", "pref-code",
    "tool-024", "doc-007", "ctx-015",
}
DOC_NOTE = (
    "运维备忘：apt 更新失败时可先换麒麟官方镜像源再重试。"
    "不要把示例 fixture ID 当作真实记忆。"
)


def fail(message: str) -> None:
    raise RuntimeError(message)


def main() -> int:
    if platform.system() != "Linux":
        fail("Run on the Galaxy Kylin V11 host (Linux), not Cursor/KARE")
    expected_host = os.getenv("MOKOA_KYLIN_HOST", "").strip()
    if expected_host and socket.gethostname() != expected_host:
        fail(f"Expected host {expected_host}, got {socket.gethostname()}")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data/reports" / (
        "kylin-browser-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    out.mkdir(parents=True, exist_ok=True)
    user = "browser_" + time.strftime("%Y%m%d%H%M%S")
    evidence = {
        "passed": False,
        "user_id": user,
        "base": BASE,
        "steps": [],
        "screenshots": [],
        "health": None,
    }

    def save() -> None:
        (out / "browser.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def note(name: str, **detail) -> None:
        evidence["steps"].append({"name": name, **detail})
        save()

    health = httpx.get(f"{API}/health", timeout=10, trust_env=False)
    health.raise_for_status()
    body = health.json()
    evidence["health"] = body
    save()
    if body.get("status") != "ok" or body.get("using_fallback") or body.get("embedding_backend") != "kylin":
        fail(f"API is not on real Kylin SDK: {body}")
    httpx.get(BASE, timeout=10, trust_env=False).raise_for_status()

    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    headed = os.environ.get("MOKOA_BROWSER_HEADED", "1" if os.environ.get("DISPLAY") else "0") != "0"
    slow_mo = int(os.environ.get("MOKOA_BROWSER_SLOW_MS", "200" if headed else "0"))
    # Kylin is not an official Playwright OS; Chromium needs these or goto hangs on a blank page.
    chromium_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    def shot(page, name: str) -> None:
        path = out / f"{name}.png"
        # Avoid full_page on HiDPI Kylin hosts: it can balloon to 4k+ and crash Chromium.
        page.screenshot(path=str(path), full_page=False)
        evidence["screenshots"].append(str(path))
        save()

    def wait_idle(page) -> None:
        if page.is_closed():
            fail("Browser page closed while waiting for UI idle")
        page.wait_for_function(
            "() => !document.querySelector('[data-testid=user-input]')?.disabled",
            timeout=180000,
        )

    def send(page, text: str) -> str:
        page.locator('[data-testid="chat-input"]').fill(text)
        page.locator('[data-testid="chat-send"]').click()
        page.locator(".bubble.thinking").wait_for(state="hidden", timeout=180000)
        wait_idle(page)
        bubbles = page.locator(".message-row.assistant .bubble:not(.thinking)")
        reply = bubbles.last.inner_text().strip()
        if not reply or "失败" in reply:
            fail(f"Assistant error or empty reply for {text!r}: {reply}")
        return reply

    def open_ingestion(page) -> None:
        page.locator('[data-testid="open-ingestion"]').click()
        page.locator('[data-testid="ingest-source"]').wait_for()
        page.locator('[data-testid="tab-ingestion"]').wait_for()

    def submit_structured_source(page, source: str) -> str:
        page.locator('[data-testid="ingest-source"]').select_option(source)
        page.locator('[data-testid="ingest-sample"]').click()
        before = page.locator('[data-testid="ingest-result"][data-state="accepted"]').count()
        page.locator('[data-testid="ingest-submit"]').click()
        page.wait_for_function(
            "n => document.querySelectorAll('[data-testid=ingest-result][data-state=accepted]').length > n",
            arg=before,
            timeout=180000,
        )
        wait_idle(page)
        text = page.locator('[data-testid="ingest-result"]').first.inner_text()
        if "接收" not in text and "写入" not in text:
            fail(f"Structured source {source} was not accepted: {text}")
        return text

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not headed,
            slow_mo=slow_mo,
            args=chromium_args,
            proxy=None,
        )
        # Fit the host display instead of a fixed oversized canvas.
        screen = browser.new_page()
        metrics = screen.evaluate(
            """() => ({
              w: Math.max(1024, Math.min(1600, (window.screen.availWidth || 1280) - 80)),
              h: Math.max(700, Math.min(1000, (window.screen.availHeight || 800) - 120)),
            })"""
        )
        screen.close()
        page = browser.new_page(
            viewport={"width": int(metrics["w"]), "height": int(metrics["h"])},
            device_scale_factor=1,
        )
        page.set_default_timeout(60000)
        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
            page.locator('[data-testid="connection-button"]').wait_for()
            # Auto-connect can lose the race after Vite restart; click into live mode if needed.
            for _ in range(8):
                btn = page.locator('[data-testid="connection-button"]')
                status = btn.inner_text().strip()
                if status == "实时 API 已连接":
                    break
                if "演示" in status:
                    try:
                        btn.click(timeout=3000)
                    except PlaywrightTimeout:
                        page.wait_for_timeout(1000)
                        continue
                page.wait_for_timeout(2000)
            try:
                page.get_by_text("实时 API 已连接", exact=True).wait_for(timeout=30000)
            except PlaywrightTimeout:
                proxy = ""
                try:
                    proxy = page.request.get(f"{BASE}/api/health", timeout=5000).text()[:300]
                except Exception as exc:  # noqa: BLE001
                    proxy = f"proxy health failed: {exc}"
                button = page.locator('[data-testid="connection-button"]').inner_text()
                shot(page, "00-not-connected")
                fail(
                    "Top bar is not 实时 API 已连接; do not use demo fixtures. "
                    f"button={button!r}; {proxy}"
                )
            shot(page, "01-connected")
            note("connected", status=page.locator('[data-testid="connection-button"]').inner_text())

            page.locator('[data-testid="user-input"]').fill(user)
            wait_idle(page)
            page.wait_for_timeout(800)
            shot(page, "02-user")
            note("user", user_id=user)

            pref_reply = send(page, "以后请用中文回答，下载文件默认保存到 E 盘。")
            page.locator('[data-testid="tab-preferences"]').click()
            page.wait_for_timeout(500)
            pref_text = page.locator(".memory-content").inner_text()
            shot(page, "03-preference-written")
            if "E:" not in pref_text and "E 盘" not in pref_text and "download_path" not in pref_text:
                fail("Preference panel did not show the E-disk download path after live ingest")
            if any(demo_id in pref_text for demo_id in DEMO_IDS):
                fail("Demo preference fixtures leaked into the live preference panel")
            note("preference_write", reply=pref_reply, panel=pref_text[:800])

            recall_pref = send(page, "你记得我的下载路径偏好吗？")
            page.locator('[data-testid="tab-memories"]').click()
            page.wait_for_timeout(400)
            memory_text = page.locator(".memory-content").inner_text()
            shot(page, "04-preference-recall")
            if any(demo_id in memory_text for demo_id in DEMO_IDS):
                fail("Demo memory IDs appeared after live recall")
            if "E:" not in memory_text + recall_pref and "E 盘" not in memory_text + recall_pref and "download" not in (
                memory_text + recall_pref
            ).lower():
                fail("Live recall did not surface the download-path preference")
            ids = page.locator("[data-testid=memory-card] small").all_inner_texts()
            note("preference_recall", reply=recall_pref, memory_ids=ids)

            # Must pass quality (explicit cue) and classify as knowledge (配置/方法/安装),
            # otherwise conversation ingest rejects as low_quality or stays preference-only.
            knowledge_write = send(
                page,
                "请记住这个配置方法：pip 安装依赖超时时可以换清华镜像。",
            )
            if "未写入记忆" in knowledge_write:
                fail(f"Knowledge write was rejected by ingest: {knowledge_write}")
            note("knowledge_write", reply=knowledge_write)
            knowledge_reply = send(page, "pip 安装超时换哪个镜像？")
            page.locator('[data-testid="tab-memories"]').click()
            page.wait_for_timeout(400)
            knowledge_text = page.locator(".memory-content").inner_text()
            shot(page, "05-knowledge-recall")
            if "清华" not in knowledge_text + knowledge_reply and "镜像" not in knowledge_text + knowledge_reply:
                fail("Knowledge recall did not return the pip/Tsinghua mirror memory")
            if any(demo_id in knowledge_text for demo_id in DEMO_IDS):
                fail("Demo knowledge fixtures leaked into live recall")
            note("knowledge_recall", reply=knowledge_reply, panel=knowledge_text[:800])

            # Multi-source panel: tool / behavior / config + text document (avoid vision/OCR hang).
            open_ingestion(page)
            shot(page, "08-ingestion-open")
            tool_result_text = submit_structured_source(page, "tool_result")
            note("ingest_tool_result", panel=tool_result_text[:500])
            behavior_text = submit_structured_source(page, "user_behavior")
            note("ingest_user_behavior", panel=behavior_text[:500])
            config_text = submit_structured_source(page, "manual_config")
            note("ingest_manual_config", panel=config_text[:500])
            shot(page, "09-ingestion-structured")

            page.locator('[data-testid="ingest-source"]').select_option("document")
            doc_path = out / "multi-source-note.txt"
            doc_path.write_text(DOC_NOTE + "\n", encoding="utf-8")
            page.locator('[data-testid="ingest-files"]').set_input_files(str(doc_path))
            page.locator('[data-testid="upload-item"]').first.wait_for()
            before_docs = page.locator('[data-testid="ingest-result"][data-state="accepted"]').count()
            page.locator('[data-testid="ingest-submit"]').click()
            try:
                page.wait_for_function(
                    "n => document.querySelectorAll('[data-testid=ingest-result][data-state=accepted]').length > n",
                    arg=before_docs,
                    timeout=180000,
                )
                # Submit stays busy through optional consolidate; wait for the button to recover.
                page.wait_for_function(
                    """() => {
                      const btn = document.querySelector('[data-testid=ingest-submit]');
                      return !!btn && !btn.disabled && !(btn.textContent || '').includes('处理中');
                    }""",
                    timeout=180000,
                )
                wait_idle(page)
            except Exception as exc:
                if page.is_closed():
                    fail(f"Browser closed during document ingest (possible Chromium OOM/crash): {exc}")
                raise
            doc_text = page.locator('[data-testid="ingest-result"]').first.inner_text()
            if "接收" not in doc_text and "写入" not in doc_text:
                fail(f"Document ingest was not accepted: {doc_text}")
            shot(page, "10-ingestion-document")
            note("ingest_document", panel=doc_text[:800], note=DOC_NOTE)

            multi_reply = send(page, "apt 更新失败可以怎么办？")
            page.locator('[data-testid="tab-memories"]').click()
            page.wait_for_timeout(400)
            multi_panel = page.locator(".memory-content").inner_text()
            shot(page, "11-multisource-recall")
            blob = multi_reply + "\n" + multi_panel
            if "镜像" not in blob and "apt" not in blob.lower() and "麒麟" not in blob:
                fail("Multi-source document knowledge was not recalled in chat")
            if any(demo_id in multi_panel for demo_id in DEMO_IDS):
                fail("Demo memory IDs appeared after multi-source recall")
            note("multisource_recall", reply=multi_reply, panel=multi_panel[:800])

            page.locator('[data-testid="tab-forget"]').click()
            page.locator('[data-testid="forget-input"]').fill("忘掉下载路径偏好")
            page.locator('[data-testid="forget-submit"]').click()
            page.get_by_text("已处理：忘掉下载路径偏好").wait_for(timeout=180000)
            forget_text = page.locator('[data-testid="forget-result"]').inner_text()
            shot(page, "06-forget")
            if "删除数量：0" in forget_text and "删除 ID：无" in forget_text:
                fail("Forget reported zero deletions")
            note("forget", result=forget_text)

            page.locator('[data-testid="tab-preferences"]').click()
            page.wait_for_timeout(500)
            cards = page.locator("[data-testid=preference-card]").all_inner_texts()
            shot(page, "07-preference-after-forget")
            joined = "\n".join(cards)
            if any("E:/Downloads" in card or "E:\\Downloads" in card for card in cards):
                fail("E-disk download preference still listed as active after forget")
            note("preference_after_forget", active_cards=cards, panel=joined[:800])

            evidence["passed"] = True
            save()
        except Exception as exc:
            evidence["error"] = f"{type(exc).__name__}: {exc}"
            try:
                shot(page, "99-failure")
            except Exception:
                pass
            save()
            print(json.dumps({"passed": False, "error": evidence["error"], "out": str(out)}, ensure_ascii=False, indent=2))
            return 1
        finally:
            browser.close()

    print(json.dumps({"passed": True, "user_id": user, "out": str(out), "screenshots": evidence["screenshots"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
