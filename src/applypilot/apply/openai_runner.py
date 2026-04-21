"""OpenAI computer-use backend for job applications.

This backend drives the same isolated Chrome workers as the Claude path, but
uses the OpenAI Responses API with the computer-use tool instead of Claude MCP.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from applypilot import config
from applypilot.apply.dashboard import add_event, get_state, update_state

log = logging.getLogger(__name__)

DEFAULT_OPENAI_APPLY_MODEL = "computer-use-preview"
DISPLAY_WIDTH = 1024
DISPLAY_HEIGHT = 768
MAX_STEPS = 80
RESPONSES_URL = "https://api.openai.com/v1/responses"
MODELS_URL = "https://api.openai.com/v1/models"

RESULT_RE = re.compile(r"RESULT:(APPLIED|EXPIRED|CAPTCHA|LOGIN_ISSUE|FAILED(?::[A-Za-z0-9_.-]+)?)")


def parse_result(text: str) -> str | None:
    """Parse an ApplyPilot result code from model output."""
    match = RESULT_RE.search(text or "")
    if not match:
        return None

    raw = match.group(1)
    if raw == "APPLIED":
        return "applied"
    if raw == "EXPIRED":
        return "expired"
    if raw == "CAPTCHA":
        return "captcha"
    if raw == "LOGIN_ISSUE":
        return "login_issue"
    if raw.startswith("FAILED:"):
        return f"failed:{raw.split(':', 1)[1].lower()}"
    return "failed:unknown"


def extract_text(response: dict[str, Any]) -> str:
    """Collect text content from a Responses API response."""
    parts: list[str] = []
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        parts.append(output_text)

    for item in response.get("output", []):
        item_type = item.get("type")
        if item_type == "message":
            for content in item.get("content", []):
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
        elif item_type in {"output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif item_type == "reasoning":
            for summary in item.get("summary", []):
                text = summary.get("text")
                if isinstance(text, str):
                    parts.append(text)

    return "\n".join(part for part in parts if part)


def _computer_tool() -> dict[str, Any]:
    return {
        "type": "computer_use_preview",
        "display_width": DISPLAY_WIDTH,
        "display_height": DISPLAY_HEIGHT,
        "environment": "browser",
    }


def _require_openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required for --agent openai")
    return key


def check_openai_computer_use_access(model: str = DEFAULT_OPENAI_APPLY_MODEL) -> tuple[bool, str]:
    """Check whether the configured OpenAI org can access the computer-use model."""
    api_key = _require_openai_key()
    try:
        response = httpx.get(
            f"{MODELS_URL}/{model}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(20.0, connect=10.0),
        )
        if response.status_code == 404:
            return (
                False,
                f"OpenAI model '{model}' is unavailable for this API key/org. "
                "OpenAI computer-use requires access to computer-use-preview.",
            )
        response.raise_for_status()
        return True, ""
    except httpx.HTTPStatusError as e:
        return False, f"OpenAI model access check failed: HTTP {e.response.status_code}"
    except httpx.HTTPError as e:
        return False, f"OpenAI model access check failed: {e}"


def _post_response(client: httpx.Client, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        RESPONSES_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    response.raise_for_status()
    return response.json()


def _screenshot_data_url(page: Any) -> str:
    png = page.screenshot(full_page=False, type="png")
    encoded = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _first_computer_call(response: dict[str, Any]) -> dict[str, Any] | None:
    for item in response.get("output", []):
        if item.get("type") == "computer_call":
            return item
    return None


def _normalize_key(key: str) -> str:
    aliases = {
        "ENTER": "Enter",
        "RETURN": "Enter",
        "SPACE": " ",
        "BACKSPACE": "Backspace",
        "DELETE": "Delete",
        "TAB": "Tab",
        "ESC": "Escape",
        "ESCAPE": "Escape",
        "ARROWUP": "ArrowUp",
        "ARROWDOWN": "ArrowDown",
        "ARROWLEFT": "ArrowLeft",
        "ARROWRIGHT": "ArrowRight",
    }
    normalized = aliases.get(key.upper(), key)
    if normalized.upper().startswith("CTRL+"):
        return "Control+" + normalized.split("+", 1)[1]
    if normalized.upper().startswith("CMD+"):
        return "Meta+" + normalized.split("+", 1)[1]
    return normalized


def _upload_context_text(page: Any) -> str:
    try:
        return page.evaluate(
            """() => {
                const active = document.activeElement;
                if (!active) return '';
                const bits = [
                  active.getAttribute('aria-label') || '',
                  active.getAttribute('name') || '',
                  active.getAttribute('id') || '',
                  active.getAttribute('placeholder') || ''
                ];
                if (active.id) {
                  document.querySelectorAll(`label[for="${active.id}"]`).forEach(label => bits.push(label.innerText || ''));
                }
                const closest = active.closest('label, div, section, fieldset');
                if (closest) bits.push((closest.innerText || '').slice(0, 300));
                return bits.join(' ').toLowerCase();
            }"""
        )
    except Exception:
        return ""


def _choose_upload_file(page: Any, resume_pdf_path: str, cover_letter_pdf_path: str | None) -> str:
    context = _upload_context_text(page)
    if cover_letter_pdf_path and "cover" in context and "resume" not in context:
        return cover_letter_pdf_path
    return resume_pdf_path


def _click_with_upload_detection(
    page: Any,
    action: dict[str, Any],
    resume_pdf_path: str,
    cover_letter_pdf_path: str | None,
) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    x = int(action.get("x", 0))
    y = int(action.get("y", 0))
    button = action.get("button", "left")
    if button not in {"left", "right", "middle"}:
        button = "left"

    try:
        with page.expect_file_chooser(timeout=800) as chooser_info:
            page.mouse.click(x, y, button=button)
        upload_path = _choose_upload_file(page, resume_pdf_path, cover_letter_pdf_path)
        chooser_info.value.set_files(upload_path)
        return f"upload {Path(upload_path).name}"
    except PlaywrightTimeoutError:
        return f"click {x},{y}"


def handle_action(
    page: Any,
    action: dict[str, Any],
    resume_pdf_path: str,
    cover_letter_pdf_path: str | None,
) -> str:
    """Execute one computer-use action against the Playwright page."""
    action_type = action.get("type")

    if action_type == "click":
        return _click_with_upload_detection(page, action, resume_pdf_path, cover_letter_pdf_path)

    if action_type == "double_click":
        x = int(action.get("x", 0))
        y = int(action.get("y", 0))
        button = action.get("button", "left")
        if button not in {"left", "right", "middle"}:
            button = "left"
        page.mouse.dblclick(x, y, button=button)
        return f"double_click {x},{y}"

    if action_type == "scroll":
        x = int(action.get("x", DISPLAY_WIDTH // 2))
        y = int(action.get("y", DISPLAY_HEIGHT // 2))
        scroll_x = int(action.get("scroll_x", 0))
        scroll_y = int(action.get("scroll_y", 0))
        page.mouse.move(x, y)
        page.evaluate(
            "({scrollX, scrollY}) => window.scrollBy(scrollX, scrollY)",
            {"scrollX": scroll_x, "scrollY": scroll_y},
        )
        return f"scroll {scroll_x},{scroll_y}"

    if action_type == "keypress":
        keys = action.get("keys") or [action.get("key", "")]
        for key in keys:
            if key:
                page.keyboard.press(_normalize_key(str(key)))
        return f"keypress {'+'.join(str(k) for k in keys)}"

    if action_type == "type":
        text = str(action.get("text", ""))
        page.keyboard.type(text, delay=5)
        return f"type {len(text)} chars"

    if action_type == "move":
        x = int(action.get("x", 0))
        y = int(action.get("y", 0))
        page.mouse.move(x, y)
        return f"move {x},{y}"

    if action_type == "drag":
        path = action.get("path") or []
        if path:
            first = path[0]
            page.mouse.move(int(first.get("x", 0)), int(first.get("y", 0)))
            page.mouse.down()
            for point in path[1:]:
                page.mouse.move(int(point.get("x", 0)), int(point.get("y", 0)))
            page.mouse.up()
        return "drag"

    if action_type == "wait":
        time.sleep(2)
        return "wait"

    if action_type == "screenshot":
        return "screenshot"

    return f"unknown_action:{action_type}"


def run_openai_computer_apply(
    *,
    job: dict,
    port: int,
    worker_id: int,
    model: str,
    prompt: str,
    resume_pdf_path: str,
    cover_letter_pdf_path: str | None = None,
    max_steps: int = MAX_STEPS,
) -> tuple[str, int]:
    """Run one application using OpenAI computer-use."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    api_key = _require_openai_key()
    target_url = job.get("application_url") or job["url"]
    start = time.time()
    response: dict[str, Any] | None = None
    text_parts: list[str] = []
    actions = 0

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] OpenAI computer-use: {job['title']} @ {job.get('site', '')}\n"
        f"URL: {target_url}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    try:
        with sync_playwright() as p, httpx.Client(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            # A freshly launched Chrome can expose chrome:// internal pages over
            # CDP. Those targets do not support viewport emulation, so always
            # create a normal page for the OpenAI computer-use loop.
            page = context.new_page()
            try:
                page.set_viewport_size({"width": DISPLAY_WIDTH, "height": DISPLAY_HEIGHT})
            except PlaywrightError:
                log.debug("Viewport sizing is unsupported for this Chrome target; continuing.")

            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass

            update_state(
                worker_id,
                status="applying",
                job_title=job["title"],
                company=job.get("site", ""),
                score=job.get("fit_score", 0),
                start_time=time.time(),
                actions=0,
                last_action="openai starting",
            )
            add_event(f"[W{worker_id}] OpenAI starting: {job['title'][:40]} @ {job.get('site', '')}")

            with open(worker_log, "a", encoding="utf-8") as lf:
                lf.write(log_header)

                screenshot = _screenshot_data_url(page)
                response = _post_response(
                    client,
                    api_key,
                    {
                        "model": model,
                        "tools": [_computer_tool()],
                        "input": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": prompt},
                                    {"type": "input_image", "image_url": screenshot},
                                ],
                            }
                        ],
                        "reasoning": {"summary": "concise"},
                        "truncation": "auto",
                    },
                )

                for _ in range(max_steps):
                    text = extract_text(response)
                    if text:
                        text_parts.append(text)
                        lf.write(text + "\n")

                    parsed = parse_result("\n".join(text_parts))
                    if parsed:
                        duration_ms = int((time.time() - start) * 1000)
                        add_event(f"[W{worker_id}] OpenAI {parsed.upper()}: {job['title'][:30]}")
                        update_state(worker_id, status=parsed.split(":", 1)[0], last_action=parsed[:35])
                        return parsed, duration_ms

                    computer_call = _first_computer_call(response)
                    if not computer_call:
                        break

                    pending_checks = computer_call.get("pending_safety_checks") or []
                    if pending_checks:
                        codes = ",".join(str(check.get("code", "safety_check")) for check in pending_checks)
                        lf.write(f"OpenAI safety check blocked action: {codes}\n")
                        duration_ms = int((time.time() - start) * 1000)
                        return f"failed:openai_safety_check_{codes[:60]}", duration_ms

                    desc = handle_action(
                        page,
                        computer_call.get("action", {}),
                        resume_pdf_path,
                        cover_letter_pdf_path,
                    )
                    actions += 1
                    lf.write(f"  >> {desc}\n")
                    ws = get_state(worker_id)
                    current_actions = ws.actions if ws else 0
                    update_state(worker_id, actions=current_actions + 1, last_action=desc[:35])

                    time.sleep(1)
                    screenshot = _screenshot_data_url(page)
                    response = _post_response(
                        client,
                        api_key,
                        {
                            "model": model,
                            "previous_response_id": response["id"],
                            "tools": [_computer_tool()],
                            "input": [
                                {
                                    "type": "computer_call_output",
                                    "call_id": computer_call["call_id"],
                                    "acknowledged_safety_checks": [],
                                    "output": {
                                        "type": "computer_screenshot",
                                        "image_url": screenshot,
                                    },
                                    "current_url": page.url,
                                }
                            ],
                            "truncation": "auto",
                        },
                    )

            if response:
                text_parts.append(extract_text(response))

            output = "\n".join(part for part in text_parts if part)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            job_log = config.LOG_DIR / f"openai_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
            job_log.write_text(output, encoding="utf-8")

            parsed = parse_result(output)
            if parsed:
                return parsed, int((time.time() - start) * 1000)
            if actions >= max_steps:
                return "failed:openai_step_limit", int((time.time() - start) * 1000)
            return "failed:no_result_line", int((time.time() - start) * 1000)

    except httpx.HTTPStatusError as e:
        body = e.response.text[:300] if e.response is not None else ""
        log.error("OpenAI Responses API error: %s %s", e, body)
        try:
            error_body = e.response.json() if e.response is not None else {}
        except ValueError:
            error_body = {}
        error = error_body.get("error", {}) if isinstance(error_body, dict) else {}
        if error.get("code") == "model_not_found":
            return "failed:openai_computer_use_unavailable", int((time.time() - start) * 1000)
        return f"failed:openai_api_{e.response.status_code}", int((time.time() - start) * 1000)
    except (httpx.HTTPError, PlaywrightError, RuntimeError) as e:
        log.error("OpenAI apply error: %s", e)
        return f"failed:{str(e)[:100]}", int((time.time() - start) * 1000)
    except Exception as e:
        log.exception("Unexpected OpenAI apply error")
        return f"failed:{str(e)[:100]}", int((time.time() - start) * 1000)
