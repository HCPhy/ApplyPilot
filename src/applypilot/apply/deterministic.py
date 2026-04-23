"""Deterministic ATS automation for common application platforms.

The goal is to use direct DOM automation for well-known ATS forms and reserve
LLM agents for pages that truly need reasoning.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.apply.dashboard import add_event, get_state, update_state
from applypilot.scoring.pdf import parse_resume

log = logging.getLogger(__name__)
_VERBOSE_ACTION_DELAY_MS = 500


def detect_apply_platform(url: str | None) -> str | None:
    """Detect the ATS platform from an application URL."""
    if not url:
        return None
    lower = url.lower()
    if "greenhouse.io" in lower:
        return "greenhouse"
    if "lever.co" in lower:
        return "lever"
    if "ashbyhq.com" in lower:
        return "ashby"
    if "myworkdayjobs.com" in lower or ".wd" in lower:
        return "workday"
    return None


@dataclass(frozen=True)
class ApplicantContext:
    full_name: str
    first_name: str
    last_name: str
    email: str
    phone: str
    phone_digits: str
    city: str
    state: str
    country: str
    current_location: str
    linkedin_url: str
    github_url: str
    github_username: str
    require_sponsorship: bool
    work_permit_type: str
    education_school: str
    education_degree: str
    education_discipline: str
    education_end_year: str
    education_level: str
    current_title: str
    years_experience_total: str
    salary_expectation: str
    salary_fallback_text: str
    resume_pdf_path: str
    cover_letter_pdf_path: str | None
    resume_text: str
    target_role: str
    relocation_scope: str
    preferred_name: str = ""
    address: str = ""
    postal_code: str = ""
    portfolio_url: str = ""
    website_url: str = ""
    site_password: str = ""
    legally_authorized_to_work: bool = True
    earliest_start_date: str = ""


def _split_name(full_name: str) -> tuple[str, str]:
    bits = [part for part in full_name.split() if part]
    if not bits:
        return "", ""
    if len(bits) == 1:
        return bits[0], ""
    return bits[0], bits[-1]


def _github_username(github_url: str) -> str:
    if not github_url:
        return ""
    return github_url.rstrip("/").split("/")[-1]


def _parse_primary_education(resume_text: str, profile: dict) -> tuple[str, str, str, str]:
    parsed = parse_resume(resume_text)
    education = parsed["sections"].get("EDUCATION", "")
    for line in education.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(
            r"^(?P<school>.+?)\s+[—-]\s+(?P<degree>.+?)(?:,\s*(?:Expected\s*)?(?P<year>\d{4}))?$",
            stripped,
        )
        if not match:
            continue
        school = match.group("school").strip()
        degree_text = match.group("degree").strip()
        year = (match.group("year") or "").strip()
        discipline = ""
        degree = degree_text
        if " in " in degree_text:
            degree, discipline = degree_text.split(" in ", 1)
        return school, degree.strip(), discipline.strip(), year

    return (
        profile.get("resume_facts", {}).get("preserved_school", ""),
        profile.get("experience", {}).get("education_level", ""),
        "",
        "",
    )


def build_applicant_context(job: dict, use_base_resume: bool = False) -> ApplicantContext:
    """Assemble structured applicant data from profile + resume."""
    profile = config.load_profile()
    personal = profile["personal"]
    experience = profile.get("experience", {})
    compensation = profile.get("compensation", {})
    availability = profile.get("availability", {})

    resume_text, resume_pdf_path, _resume_kind = _load_resume_assets_local(job, use_base_resume=use_base_resume)
    if not resume_pdf_path:
        raise ValueError("No resume PDF is available for deterministic apply.")

    school, degree, discipline, end_year = _parse_primary_education(resume_text, profile)
    salary_expectation = str(compensation.get("salary_expectation", "") or "").strip()
    if not salary_expectation:
        salary_range_min = str(compensation.get("salary_range_min", "") or "").strip()
        salary_expectation = salary_range_min

    first_name, last_name = _split_name(personal.get("full_name", ""))
    city = personal.get("city", "").strip()
    state = personal.get("province_state", "").strip()
    country = personal.get("country", "").strip()
    location_bits = [city, state, country]
    current_location = ", ".join(bit for bit in location_bits if bit)

    return ApplicantContext(
        full_name=personal.get("full_name", ""),
        first_name=first_name,
        last_name=last_name,
        email=personal.get("email", ""),
        phone=personal.get("phone", ""),
        phone_digits="".join(ch for ch in personal.get("phone", "") if ch.isdigit()),
        city=city,
        state=state,
        country=country,
        current_location=current_location,
        linkedin_url=personal.get("linkedin_url", ""),
        github_url=personal.get("github_url", ""),
        github_username=_github_username(personal.get("github_url", "")),
        require_sponsorship=bool(profile.get("work_authorization", {}).get("require_sponsorship")),
        work_permit_type=str(profile.get("work_authorization", {}).get("work_permit_type", "") or ""),
        education_school=school,
        education_degree=degree,
        education_discipline=discipline,
        education_end_year=end_year,
        education_level=str(experience.get("education_level", "") or ""),
        current_title=str(experience.get("current_title", "") or ""),
        years_experience_total=str(experience.get("years_of_experience_total", "") or ""),
        salary_expectation=salary_expectation,
        salary_fallback_text="Flexible / open to discuss",
        resume_pdf_path=str(resume_pdf_path),
        cover_letter_pdf_path=None,
        resume_text=resume_text,
        target_role=str(experience.get("target_role", "") or ""),
        relocation_scope=str(availability.get("relocation_scope", "") or ""),
        preferred_name=str(personal.get("preferred_name", "") or ""),
        address=str(personal.get("address", "") or ""),
        postal_code=str(personal.get("postal_code", "") or ""),
        portfolio_url=str(personal.get("portfolio_url", "") or ""),
        website_url=str(personal.get("website_url", "") or ""),
        site_password=str(personal.get("password", "") or ""),
        legally_authorized_to_work=bool(
            profile.get("work_authorization", {}).get("legally_authorized_to_work", True)
        ),
        earliest_start_date=str(availability.get("earliest_start_date", "") or ""),
    )


def _load_resume_assets_local(job: dict, use_base_resume: bool = False) -> tuple[str, Path | None, str]:
    """Load the resume text and PDF path to use for an application."""
    if use_base_resume:
        resume_text = ""
        if config.RESUME_PATH.exists():
            resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
        pdf_path = config.RESUME_PDF_PATH if config.RESUME_PDF_PATH.exists() else None
        return resume_text, pdf_path, "base"

    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    pdf_path = Path(resume_path).with_suffix(".pdf") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")
    if pdf_path and not pdf_path.exists():
        pdf_path = None
    return resume_text, pdf_path, "tailored"


_MISSING_REQUIRED_FIELDS_JS = """
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const out = [];
  const seen = new Set();

  function isVisible(el) {
    if (!el) return false;
    if (el.hidden || el.getAttribute('aria-hidden') === 'true') return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    return !!(el.getClientRects && el.getClientRects().length);
  }

  function fieldLabel(el) {
    const labels = el.labels ? Array.from(el.labels).map(x => norm(x.innerText || x.textContent || '')).filter(Boolean) : [];
    if (labels.length) return labels.join(' / ');
    const aria = norm(el.getAttribute('aria-label') || '');
    if (aria) return aria;
    const placeholder = norm(el.getAttribute('placeholder') || '');
    if (placeholder) return placeholder;
    const name = norm(el.getAttribute('name') || '');
    if (name) return name;
    let current = el.parentElement;
    for (let depth = 0; current && depth < 6; depth += 1, current = current.parentElement) {
      const label = current.querySelector('label, legend, .upload-label');
      if (label) {
        const text = norm(label.innerText || label.textContent || '');
        if (text) return text;
      }
    }
    return el.tagName.toLowerCase();
  }

  const radiosDone = new Set();
  const checkboxesDone = new Set();

  document.querySelectorAll('input, select, textarea').forEach(el => {
    if (el.disabled || el.type === 'hidden') return;
    if (!isVisible(el)) return;
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (!required) return;

    if (el.type === 'radio') {
      const key = el.name || fieldLabel(el);
      if (radiosDone.has(key)) return;
      radiosDone.add(key);
      const group = key && el.name ? Array.from(document.querySelectorAll(`input[type="radio"][name="${el.name}"]`)) : [el];
      if (!group.some(node => node.checked)) {
        const label = fieldLabel(el);
        if (!seen.has(label)) {
          seen.add(label);
          out.push(label);
        }
      }
      return;
    }

    if (el.type === 'checkbox') {
      const fieldset = el.closest('fieldset');
      const key = el.name || (fieldset && fieldset.id) || fieldLabel(el);
      if (checkboxesDone.has(key)) return;
      checkboxesDone.add(key);
      const group = key && el.name ? Array.from(document.querySelectorAll(`input[type="checkbox"][name="${el.name}"]`)) : [el];
      if (!group.some(node => node.checked)) {
        const label = fieldset ? fieldLabel(fieldset) : fieldLabel(el);
        if (!seen.has(label)) {
          seen.add(label);
          out.push(label);
        }
      }
      return;
    }

    if (el.type === 'file') {
      if (!el.files || !el.files.length) {
        const label = fieldLabel(el);
        if (!seen.has(label)) {
          seen.add(label);
          out.push(label);
        }
      }
      return;
    }

    if (el.tagName === 'SELECT') {
      const selected = el.selectedOptions && el.selectedOptions.length ? norm(el.selectedOptions[0].textContent || '') : '';
      if (!el.value || selected.startsWith('select')) {
        const label = fieldLabel(el);
        if (!seen.has(label)) {
          seen.add(label);
          out.push(label);
        }
      }
      return;
    }

    const isCombobox = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list';
    if (isCombobox) {
      const container = el.closest('.select__container') || el.closest('.select');
      const control = container ? container.querySelector('.select__control') : null;
      const placeholder = container ? container.querySelector('.select__placeholder') : null;
      const controlText = norm(control ? control.innerText || control.textContent || '' : '');
      const placeholderText = norm(placeholder ? placeholder.innerText || placeholder.textContent || '' : '');
      if (controlText && controlText !== placeholderText && !controlText.startsWith('select')) {
        return;
      }
    }

    if (!norm(el.value || '')) {
      const label = fieldLabel(el);
      if (!seen.has(label)) {
        seen.add(label);
        out.push(label);
      }
    }
  });

  return out.slice(0, 12);
}
"""


def _fill_text(page, labels: list[str], value: str) -> bool:
    if not value:
        return False
    pattern = re.compile("|".join(re.escape(label) for label in labels), re.I)
    try:
        locator = page.get_by_label(pattern).first
        locator.fill(value, timeout=1500)
        return True
    except PlaywrightError:
        return False


def _fill_name(page, field_name: str, value: str) -> bool:
    if not value:
        return False
    try:
        page.locator(f'[name="{field_name}"]').first.fill(value, timeout=1500)
        return True
    except PlaywrightError:
        return False


def _fill_placeholder(page, placeholders: list[str], value: str) -> bool:
    if not value:
        return False
    pattern = re.compile("|".join(re.escape(item) for item in placeholders), re.I)
    try:
        page.get_by_placeholder(pattern).first.fill(value, timeout=1500)
        return True
    except PlaywrightError:
        return False


def _check_selector(page, selector: str) -> int:
    checked = 0
    locator = page.locator(selector)
    for idx in range(locator.count()):
        try:
            item = locator.nth(idx)
            if not item.is_checked():
                item.check(timeout=1500)
            checked += 1
        except PlaywrightError:
            continue
    return checked


def _current_company_text(ctx: ApplicantContext) -> str:
    if ctx.current_title and ctx.education_school:
        return f"{ctx.current_title} at {ctx.education_school}"
    return ctx.current_title or ctx.education_school or ctx.target_role or "Boston College"


def _start_date_text(days_out: int = 14) -> str:
    return (date.today() + timedelta(days=days_out)).strftime("%m/%d/%Y")


def _trace_progress(
    worker_id: int,
    message: str,
    *,
    verbose: bool,
    page=None,
    delay_ms: int = _VERBOSE_ACTION_DELAY_MS,
) -> None:
    if not verbose:
        return

    state = get_state(worker_id)
    current_actions = state.actions if state is not None else 0
    update_state(
        worker_id,
        actions=current_actions + 1,
        last_action=message[:35],
    )
    add_event(f"[W{worker_id}] {message}")

    if page is not None:
        try:
            page.bring_to_front()
        except PlaywrightError:
            pass
        if delay_ms > 0:
            page.wait_for_timeout(delay_ms)


def _safe_wait_network_idle(page, timeout: int = 8_000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def _normalize_workday_apply_url(url: str) -> str:
    base = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if "/apply/" in base or base.endswith("/apply"):
        return base
    return f"{base}/apply"


def _fill_automation_id(page, automation_ids: list[str], value: str) -> bool:
    if not value:
        return False
    for automation_id in automation_ids:
        locator = page.locator(f'[data-automation-id="{automation_id}"]')
        if not locator.count():
            continue
        try:
            locator.first.fill(value, timeout=1_500)
            return True
        except PlaywrightError:
            continue
    return False


def _click_automation_id(page, automation_ids: list[str]) -> bool:
    for automation_id in automation_ids:
        locator = page.locator(f'[data-automation-id="{automation_id}"]')
        if not locator.count():
            continue
        try:
            locator.first.click(timeout=2_000)
            return True
        except PlaywrightError:
            continue
    return False


def _click_named_control(page, names: list[str], *, exact: bool = True) -> bool:
    for name in names:
        pattern = re.compile(f"^{re.escape(name)}$" if exact else re.escape(name), re.I)
        for getter in (page.get_by_role("button", name=pattern), page.get_by_role("link", name=pattern)):
            try:
                getter.first.click(timeout=2_000)
                return True
            except PlaywrightError:
                continue
    return False


def _workday_heading(page) -> str:
    for selector in ("h1", "h2", "h3", '[data-automation-id="formTitle"]'):
        locator = page.locator(selector)
        if not locator.count():
            continue
        for idx in range(min(locator.count(), 3)):
            try:
                text = (locator.nth(idx).text_content() or "").strip()
            except PlaywrightError:
                continue
            if text:
                return text
    return ""


def _workday_step_signature(page) -> str:
    try:
        active_step = page.locator('[data-automation-id="progressBarActiveStep"]').count()
    except PlaywrightError:
        active_step = 0
    return "|".join(
        [
            page.url,
            _norm_text(_workday_heading(page)),
            str(active_step),
        ]
    )


def _workday_auth_mode(page) -> str | None:
    if page.locator('[data-automation-id="SignInWithEmailButton"]').count():
        return "sign_in_with_email"
    if page.locator('[data-automation-id="createAccountSubmitButton"]').count():
        return "create_account"
    if page.locator('[data-automation-id="signInSubmitButton"]').count():
        return "sign_in"
    heading = _norm_text(_workday_heading(page))
    if "create account" in heading:
        return "create_account"
    if "sign in" in heading:
        return "sign_in"
    return None


def _workday_is_auth_page(page) -> bool:
    return _workday_auth_mode(page) is not None


def _workday_confirmation_seen(page) -> bool:
    text = _visible_text(page).lower()
    markers = (
        "application submitted",
        "thank you for applying",
        "received your application",
        "your application has been submitted",
        "application received",
    )
    return any(marker in text for marker in markers)


def _workday_enter_flow(page) -> bool:
    text = _visible_text(page).lower()
    if "/apply/" in page.url or page.url.rstrip("/").endswith("/apply") or _workday_is_auth_page(page):
        return True
    if "start your application" in text:
        if _click_named_control(page, ["Autofill with Resume", "Apply Manually"]):
            page.wait_for_timeout(1_000)
            _safe_wait_network_idle(page, timeout=6_000)
            return True
    if _click_named_control(page, ["Apply", "Apply Now"], exact=False):
        page.wait_for_timeout(1_000)
        _safe_wait_network_idle(page, timeout=6_000)
        return True
    return False


def _workday_fill_auth_form(
    page,
    ctx: ApplicantContext,
    dry_run: bool,
    *,
    worker_id: int,
    verbose: bool,
) -> str | None:
    if not ctx.site_password:
        return "failed:workday_missing_password"

    for _ in range(4):
        mode = _workday_auth_mode(page)
        if mode is None:
            return None

        if mode == "sign_in_with_email":
            _trace_progress(worker_id, "Workday: choosing email sign-in", verbose=verbose, page=page)
            if not _click_automation_id(page, ["SignInWithEmailButton"]):
                return "failed:login_issue"
            page.wait_for_timeout(800)
            continue

        if mode == "create_account":
            if dry_run:
                _trace_progress(worker_id, "Workday: auth requires an account", verbose=verbose, page=page)
                if _click_automation_id(page, ["signInLink"]):
                    page.wait_for_timeout(800)
                    continue
                return "failed:workday_dry_run_needs_account"

            _trace_progress(worker_id, "Workday: creating candidate account", verbose=verbose, page=page)
            _fill_automation_id(page, ["email"], ctx.email)
            _fill_automation_id(page, ["password"], ctx.site_password)
            _fill_automation_id(page, ["verifyPassword"], ctx.site_password)
            _click_automation_id(page, ["createAccountCheckbox"])

            if not (_click_named_control(page, ["Create Account"]) or _click_automation_id(page, ["createAccountSubmitButton"])):
                return "failed:login_issue"

            page.wait_for_timeout(1_500)
            _safe_wait_network_idle(page, timeout=6_000)
            text = _visible_text(page).lower()
            if any(marker in text for marker in ("already have an account", "already exists")) and _click_automation_id(page, ["signInLink"]):
                page.wait_for_timeout(800)
                continue
            if _workday_is_auth_page(page):
                if any(marker in text for marker in ("verify your email", "verification code", "confirm your account")):
                    return "failed:login_issue"
                if page.locator('[data-automation-id="createAccountSubmitButton"]').count():
                    return "failed:login_issue"
            return None

        if mode == "sign_in":
            if not dry_run and page.locator('[data-automation-id="createAccountLink"]').count():
                try:
                    _trace_progress(worker_id, "Workday: switching to account creation", verbose=verbose, page=page)
                    page.locator('[data-automation-id="createAccountLink"]').first.click(timeout=1_500)
                    page.wait_for_timeout(800)
                    continue
                except PlaywrightError:
                    pass

            _trace_progress(worker_id, "Workday: signing in", verbose=verbose, page=page)
            _fill_automation_id(page, ["email"], ctx.email)
            _fill_automation_id(page, ["password"], ctx.site_password)
            if not (_click_named_control(page, ["Sign In"]) or _click_automation_id(page, ["signInSubmitButton"])):
                return "failed:login_issue"

            page.wait_for_timeout(1_500)
            _safe_wait_network_idle(page, timeout=6_000)
            if _workday_is_auth_page(page):
                text = _visible_text(page).lower()
                if any(marker in text for marker in ("invalid", "incorrect", "unable to sign in", "doesn't match")):
                    return "failed:login_issue"
                if not dry_run and page.locator('[data-automation-id="createAccountLink"]').count():
                    try:
                        _trace_progress(worker_id, "Workday: falling back to account creation", verbose=verbose, page=page)
                        page.locator('[data-automation-id="createAccountLink"]').first.click(timeout=1_500)
                        page.wait_for_timeout(800)
                        continue
                    except PlaywrightError:
                        pass
                return "failed:login_issue"
            return None

    return "failed:login_issue"


def _workday_upload_resume(page, ctx: ApplicantContext) -> bool:
    file_inputs = page.locator('input[type="file"]')
    count = file_inputs.count()
    if not count:
        return False

    for idx in range(count):
        candidate = file_inputs.nth(idx)
        try:
            surrounding = candidate.evaluate(
                """
                (el) => {
                  const host = el.closest('[data-automation-id], section, form, div');
                  return (host?.innerText || el.getAttribute('aria-label') || '').trim();
                }
                """
            )
        except PlaywrightError:
            surrounding = ""
        text = _norm_text(surrounding)
        if count == 1 or "resume" in text or "cv" in text or "autofill" in text:
            try:
                candidate.set_input_files(ctx.resume_pdf_path)
                page.wait_for_timeout(1_000)
                return True
            except PlaywrightError:
                continue
    return False


def _workday_select(page, labels: list[str], options: list[str]) -> bool:
    if _select_choice(page, labels, options):
        return True
    return _click_options(page, labels, options) > 0


def _workday_answer_yes_no(page, labels: list[str], answer: bool) -> bool:
    button_text = "Yes" if answer else "No"
    if _click_button_option(page, labels, button_text):
        return True
    return _click_options(page, labels, [button_text.lower()]) > 0


def _workday_check_agreements(page) -> int:
    checked = 0
    checkboxes = page.locator('input[type="checkbox"]')
    for idx in range(checkboxes.count()):
        item = checkboxes.nth(idx)
        try:
            if not item.is_visible() or item.is_checked():
                continue
            text = item.evaluate(
                """
                (el) => {
                  const host = el.closest('label, div, fieldset') || el.parentElement;
                  return (host?.innerText || '').trim();
                }
                """
            )
        except PlaywrightError:
            continue
        normalized = _norm_text(text)
        if not normalized:
            continue
        if any(marker in normalized for marker in ("text message", "sms", "job alert", "marketing", "newsletter")):
            continue
        if any(marker in normalized for marker in ("i agree", "acknowledge", "consent", "certify", "privacy", "terms")):
            try:
                item.check(timeout=1_500)
                checked += 1
            except PlaywrightError:
                continue
    return checked


def _workday_fill_common_fields(page, ctx: ApplicantContext) -> None:
    preferred_name = ctx.preferred_name or ctx.first_name
    phone_value = ctx.phone_digits or ctx.phone
    website = ctx.website_url or ctx.portfolio_url
    earliest_start = ctx.earliest_start_date if ctx.earliest_start_date and ctx.earliest_start_date.lower() != "immediately" else _start_date_text()

    _fill_text(page, ["first name", "legal first name", "given name"], ctx.first_name)
    _fill_text(page, ["preferred name", "nickname"], preferred_name)
    _fill_text(page, ["last name", "family name", "surname"], ctx.last_name)
    _fill_text(page, ["email"], ctx.email)
    _fill_text(page, ["phone", "mobile phone"], phone_value)
    _fill_text(page, ["address line 1", "street address", "address"], ctx.address)
    _fill_text(page, ["city"], ctx.city)
    _fill_text(page, ["postal code", "zip code", "zip/postal code"], ctx.postal_code)
    _fill_text(page, ["linkedin"], ctx.linkedin_url)
    _fill_text(page, ["github"], ctx.github_url)
    _fill_text(page, ["website", "portfolio", "personal website"], website)
    _fill_text(page, ["current company", "employer"], _current_company_text(ctx))
    _fill_text(page, ["job title", "current title"], ctx.current_title or ctx.target_role)
    _fill_text(page, ["school"], ctx.education_school)
    _fill_text(page, ["major", "field of study", "discipline"], ctx.education_discipline or "Physics")
    _fill_text(page, ["start date", "earliest start date", "available to start"], earliest_start)
    _fill_text(
        page,
        ["desired salary", "salary expectation", "compensation expectation", "annual compensation"],
        ctx.salary_expectation or ctx.salary_fallback_text,
    )
    _fill_placeholder(page, ["mm/dd/yyyy"], earliest_start)

    _workday_select(page, ["country", "country/region"], [ctx.country, "United States", "USA"])
    _workday_select(page, ["state", "province", "state/province"], [ctx.state])
    _workday_select(page, ["phone device type"], ["Mobile", "Cell"])
    _workday_select(page, ["degree"], _degree_terms(ctx.education_degree or ctx.education_level))
    _workday_select(
        page,
        ["gender"],
        ["Decline to self-identify", "I do not wish to answer", "Prefer not to say"],
    )
    _workday_select(
        page,
        ["race", "ethnicity", "race/ethnicity"],
        ["Decline to self-identify", "I do not wish to answer", "Prefer not to say"],
    )
    _workday_select(
        page,
        ["veteran"],
        ["I am not a protected veteran", "I do not wish to answer", "Prefer not to say"],
    )
    _workday_select(
        page,
        ["disability"],
        [
            "I don't wish to answer",
            "I do not want to answer",
            "No, I don't have a disability, or a history/record of having a disability",
        ],
    )

    _workday_answer_yes_no(
        page,
        ["legally authorized to work", "authorized to work", "work authorization"],
        ctx.legally_authorized_to_work,
    )
    _workday_answer_yes_no(
        page,
        ["require sponsorship", "need sponsorship", "employment visa"],
        ctx.require_sponsorship,
    )
    _workday_answer_yes_no(
        page,
        ["willing to relocate", "open to relocate", "relocation"],
        ctx.relocation_scope == "us",
    )
    _workday_answer_yes_no(
        page,
        ["current employee", "previously employed", "worked for"],
        False,
    )
    _workday_answer_yes_no(
        page,
        ["18 years of age", "at least 18 years old"],
        True,
    )


def _workday_click_next(page) -> bool:
    return _click_named_control(
        page,
        ["Save and Continue", "Continue", "Next", "Review and Submit"],
    )


def _workday_has_submit(page) -> bool:
    pattern = re.compile(r"^Submit$", re.I)
    for locator in (page.get_by_role("button", name=pattern), page.get_by_role("link", name=pattern)):
        try:
            if locator.count():
                return True
        except PlaywrightError:
            continue
    return False


def _workday_click_submit(page) -> bool:
    return _click_named_control(page, ["Submit"])


def _connect_browser_cdp(playwright, port: int, attempts: int = 20, delay_s: float = 0.5):
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            return playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except PlaywrightError as exc:
            last_error = exc
            time.sleep(delay_s)
    if last_error is not None:
        raise last_error
    raise RuntimeError("Failed to connect to Chrome over CDP.")


def _norm_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _match_option_index(option_texts: list[str], desired_terms: list[str]) -> int | None:
    normalized_options = [_norm_text(text) for text in option_texts]
    normalized_terms = [_norm_text(term) for term in desired_terms if term]

    for term in normalized_terms:
        for idx, text in enumerate(normalized_options):
            if text == term:
                return idx

        for idx, text in enumerate(normalized_options):
            if text.startswith(term):
                return idx

        for idx, text in enumerate(normalized_options):
            if term in text:
                return idx

    return None


def _click_combobox_option(page, locator, desired_terms: list[str], *, filter_term: str | None = None) -> bool:
    if filter_term is not None:
        try:
            locator.fill(filter_term, timeout=1500)
        except PlaywrightError:
            return False
        page.wait_for_timeout(600)

    listbox_id = locator.get_attribute("aria-controls")
    if not listbox_id:
        try:
            locator.press("ArrowDown")
        except PlaywrightError:
            return False
        page.wait_for_timeout(250)
        listbox_id = locator.get_attribute("aria-controls")
    if not listbox_id:
        return False

    options = page.locator(f"#{listbox_id} [role=option]")
    count = options.count()
    if not count:
        return False

    option_texts = [(options.nth(idx).text_content() or "").strip() for idx in range(count)]
    match_idx = _match_option_index(option_texts, desired_terms)
    if match_idx is None:
        return False

    try:
        options.nth(match_idx).click(timeout=1500)
    except PlaywrightError:
        return False
    page.wait_for_timeout(350)
    return True


def _select_choice(page, labels: list[str], desired_terms: list[str]) -> bool:
    if not desired_terms:
        return False
    pattern = re.compile("|".join(re.escape(label) for label in labels), re.I)
    try:
        matches = page.get_by_label(pattern)
        if not matches.count():
            return False
        locator = matches.first
        tag_name = locator.evaluate("(el) => el.tagName.toLowerCase()")
        if tag_name == "select":
            options = locator.evaluate(
                "(el) => Array.from(el.options).map(o => ({value: o.value, text: (o.textContent || '').trim()}))"
            )
            for term in desired_terms:
                for option in options:
                    if term.lower() in option["text"].lower():
                        locator.select_option(option["value"], timeout=1500)
                        return True
            return False

        role = locator.get_attribute("role") or ""
        aria_autocomplete = locator.get_attribute("aria-autocomplete") or ""
        if role == "combobox" or aria_autocomplete == "list":
            for term in desired_terms:
                if not term:
                    continue
                locator.click(timeout=1500)
                if _click_combobox_option(page, locator, desired_terms, filter_term=term):
                    return True

            locator.click(timeout=1500)
            try:
                locator.fill("", timeout=1500)
            except PlaywrightError:
                pass
            page.wait_for_timeout(250)
            if _click_combobox_option(page, locator, desired_terms):
                return True
            return False
    except PlaywrightError:
        return False
    return False


def _click_options(page, question_labels: list[str], desired_terms: list[str]) -> int:
    desired_patterns = [re.compile(re.escape(term), re.I) for term in desired_terms if term]
    if not desired_patterns:
        return 0

    question_pattern = re.compile("|".join(re.escape(label) for label in question_labels), re.I)
    candidates = [
        page.locator("div.ashby-application-form-field-entry").filter(has_text=question_pattern),
        page.locator("li.application-question").filter(has_text=question_pattern),
        page.locator("fieldset").filter(has_text=question_pattern),
        page.locator("div").filter(has_text=question_pattern),
    ]

    container = page
    for candidate in candidates:
        if candidate.count():
            container = candidate.first
            break

    clicked = 0
    for pattern in desired_patterns:
        try:
            container.locator("label").filter(has_text=pattern).first.click(timeout=1500)
            clicked += 1
        except PlaywrightError:
            continue
    return clicked


def _click_button_option(page, question_labels: list[str], button_text: str) -> bool:
    question_pattern = re.compile("|".join(re.escape(label) for label in question_labels), re.I)
    selectors = [
        "div.ashby-application-form-field-entry",
        "li.application-question",
        "fieldset",
        "div",
    ]
    for selector in selectors:
        candidate = page.locator(selector).filter(has_text=question_pattern)
        if not candidate.count():
            continue
        try:
            candidate.first.get_by_role("button", name=re.compile(f"^{re.escape(button_text)}$", re.I)).first.click(timeout=1500)
            return True
        except PlaywrightError:
            continue
    return False


def _visible_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def _remaining_required_fields(page) -> list[str]:
    return page.evaluate(_MISSING_REQUIRED_FIELDS_JS) or []


def _greenhouse_expired(text: str) -> bool:
    normalized = text.lower()
    markers = (
        "no longer accepting applications",
        "job has been closed",
        "this job is no longer available",
        "position has been filled",
    )
    return any(marker in normalized for marker in markers)


def _choose_greenhouse_location_options(text: str, ctx: ApplicantContext) -> list[str]:
    options = []
    normalized = text.lower()
    if "new york" in normalized and ctx.city.lower() in {"boston", "new york"}:
        options.append("new york")
    if ctx.relocation_scope == "us":
        for fallback in ("chicago", "new york", "boston", "austin", "seattle", "san francisco"):
            if fallback in normalized and fallback not in options:
                options.append(fallback)
    return options[:2] or ["new york", "chicago"]


def _choose_eligibility_terms(ctx: ApplicantContext) -> list[str]:
    if ctx.require_sponsorship:
        return ["Yes, will require firm sponsorship"]
    return ["No. already has permanent work authorization"]


def _choose_experience_terms(ctx: ApplicantContext) -> list[str]:
    years = ctx.years_experience_total.strip()
    if years.isdigit():
        num = int(years)
        if num <= 1:
            return ["Graduate (no full time experience)"]
        if num <= 3:
            return ["1-3"]
        if num <= 10:
            return ["4-10"]
        return ["10+"]
    if ctx.education_level.lower() in {"phd", "ph.d", "doctorate"}:
        return ["Graduate (no full time experience)"]
    return ["1-3", "Graduate (no full time experience)"]


def _current_degree_terms(ctx: ApplicantContext) -> list[str]:
    degree_text = (ctx.education_degree or ctx.education_level).lower()
    if "ph" in degree_text or "doctor" in degree_text:
        return ["PhD"]
    if "master" in degree_text or "m." in degree_text:
        return ["Master's"]
    if "bachelor" in degree_text or "b." in degree_text:
        return ["Bachelor's"]
    return ["PhD", "Master's", "Bachelor's"]


def _gpa_terms(ctx: ApplicantContext) -> list[str]:
    resume = ctx.resume_text.lower()
    if "gpa" not in resume:
        return ["NA"]
    if re.search(r"\b4\.0\b", resume) or re.search(r"\b3\.[6-9]\b", resume):
        return ["3.6-4.0"]
    if re.search(r"\b3\.[0-5]\b", resume):
        return ["3.0 -3.5"]
    return ["NA"]


def _internship_terms(ctx: ApplicantContext) -> list[str]:
    resume = ctx.resume_text.lower()
    has_internship = re.search(r"\bintern(ship)?\b", resume) is not None
    explicitly_none = re.search(r"\bno internships?\b|\bno internship\b", resume) is not None
    if has_internship and not explicitly_none:
        return ["Yes, I have internship experience, but not within a hedge fund or proprietary trading firm."]
    return ["No, I have not completed any internships."]


def _degree_terms(text: str) -> list[str]:
    lowered = text.lower()
    if "ph" in lowered or "doctor" in lowered:
        return [
            "Doctor of Philosophy (Ph.D.)",
            "Doctorate",
            "Postdoctoral Studies",
        ]
    if "m." in lowered or "master" in lowered:
        return ["Master's Degree", "Masters", "Master of Business Administration (M.B.A.)"]
    if "b." in lowered or "bachelor" in lowered:
        return ["Bachelor's Degree", "Bachelors"]
    return [lowered]


def _run_greenhouse_apply(
    *,
    job: dict,
    port: int,
    worker_id: int,
    dry_run: bool,
    use_base_resume: bool,
    verbose: bool,
) -> tuple[str, int]:
    ctx = build_applicant_context(job, use_base_resume=use_base_resume)
    target_url = job.get("application_url") or job["url"]
    start = time.time()

    with sync_playwright() as p:
        browser = _connect_browser_cdp(p, port)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
        except PlaywrightTimeoutError:
            return "failed:page_error", int((time.time() - start) * 1000)

        update_state(
            worker_id,
            status="applying",
            job_title=job["title"],
            company=job.get("site", ""),
            score=job.get("fit_score", 0),
            start_time=time.time(),
            actions=0,
            last_action="deterministic greenhouse",
        )
        add_event(f"[W{worker_id}] Deterministic Greenhouse: {job['title'][:40]}")
        _trace_progress(worker_id, "Opened Greenhouse application", verbose=verbose, page=page)

        text = _visible_text(page)
        if _greenhouse_expired(text):
            return "expired", int((time.time() - start) * 1000)

        if "apply for this job" not in text.lower() and "submit application" not in text.lower():
            try:
                page.get_by_text("Apply", exact=False).first.click(timeout=2_000)
                page.wait_for_timeout(800)
                text = _visible_text(page)
            except PlaywrightError:
                pass

        # Basic identity
        _trace_progress(worker_id, "Filling contact info", verbose=verbose, page=page)
        _fill_text(page, ["first name"], ctx.first_name)
        _fill_text(page, ["last name"], ctx.last_name)
        _fill_text(page, ["email"], ctx.email)
        _fill_text(page, ["phone"], ctx.phone_digits or ctx.phone)
        _fill_text(page, ["current location"], ctx.current_location)
        _fill_text(page, ["linkedin profile", "linkedin"], ctx.linkedin_url)
        _fill_text(page, ["github username", "github"], ctx.github_username)
        _fill_text(page, ["outstanding offers", "deadlines"], "No outstanding offers or deadlines at this time.")

        # Resume upload
        file_inputs = page.locator("input[type=file]")
        if file_inputs.count():
            _trace_progress(worker_id, "Uploading resume", verbose=verbose, page=page)
            file_inputs.first.set_input_files(ctx.resume_pdf_path)

        # Education block
        _trace_progress(worker_id, "Filling education", verbose=verbose, page=page)
        _select_choice(page, ["school"], [ctx.education_school])
        _select_choice(page, ["degree"], _degree_terms(ctx.education_degree or ctx.education_level))
        _select_choice(page, ["discipline"], [ctx.education_discipline or "Physics", "Physics"])
        _fill_text(page, ["end date year"], ctx.education_end_year)

        # Common screening answers
        _trace_progress(worker_id, "Answering screening questions", verbose=verbose, page=page)
        _select_choice(page, ["employment eligibility status"], _choose_eligibility_terms(ctx))
        _click_options(
            page,
            ["will you require the firm's sponsorship", "will you require sponsorship"],
            ["yes"] if ctx.require_sponsorship else ["no"],
        )
        _select_choice(page, ["current professional experience"], _choose_experience_terms(ctx))
        _select_choice(page, ["what degree are you currently pursuing", "degree are you currently pursuing"], _current_degree_terms(ctx))
        _select_choice(page, ["what year are you expected to graduate", "expected to graduate"], [ctx.education_end_year])
        _click_options(
            page,
            ["fields of study", "education background"],
            [ctx.education_discipline.lower() or "physics"],
        )
        _select_choice(page, ["for your most recent degree", "gpa"], _gpa_terms(ctx))
        _select_choice(page, ["have you completed any internships"], _internship_terms(ctx))
        _click_options(
            page,
            ["mathematics competitions", "have you participated"],
            ["i have not participated in any of these competitions", "none"],
        )
        _fill_text(
            page,
            ["annualized total compensation expectations", "compensation expectations"],
            ctx.salary_expectation or ctx.salary_fallback_text,
        )
        _fill_text(page, ["how did you hear about this job"], "LinkedIn")

        # Country / preference fields if they are native selects.
        _select_choice(page, ["country"], [ctx.country, "united states", "usa"])
        if not _select_choice(page, ["location preference"], _choose_greenhouse_location_options(text, ctx)):
            _click_options(page, ["location preference"], _choose_greenhouse_location_options(text, ctx))

        page.wait_for_timeout(700)
        _trace_progress(worker_id, "Reviewing required fields", verbose=verbose, page=page)
        missing = _remaining_required_fields(page)
        if missing:
            add_event(f"[W{worker_id}] Deterministic missing: {', '.join(missing[:3])}")
            update_state(worker_id, last_action=f"missing {', '.join(missing[:2])}"[:35])
            return "failed:deterministic_missing_data", int((time.time() - start) * 1000)

        if dry_run:
            add_event(f"[W{worker_id}] Deterministic ready: {job['title'][:30]}")
            return "applied", int((time.time() - start) * 1000)

        try:
            _trace_progress(worker_id, "Submitting application", verbose=verbose, page=page)
            page.get_by_text("Submit application", exact=False).first.click(timeout=3_000)
            page.wait_for_timeout(2_500)
        except PlaywrightError:
            return "failed:submission_not_confirmed", int((time.time() - start) * 1000)

        post_submit = _visible_text(page).lower()
        if any(marker in post_submit for marker in ("application submitted", "thank you", "received your application")):
            return "applied", int((time.time() - start) * 1000)
        return "failed:submission_not_confirmed", int((time.time() - start) * 1000)


def _run_lever_apply(
    *,
    job: dict,
    port: int,
    worker_id: int,
    dry_run: bool,
    use_base_resume: bool,
    verbose: bool,
) -> tuple[str, int]:
    ctx = build_applicant_context(job, use_base_resume=use_base_resume)
    target_url = job.get("application_url") or job["url"]
    start = time.time()

    with sync_playwright() as p:
        browser = _connect_browser_cdp(p, port)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
        except PlaywrightTimeoutError:
            return "failed:page_error", int((time.time() - start) * 1000)

        update_state(
            worker_id,
            status="applying",
            job_title=job["title"],
            company=job.get("site", ""),
            score=job.get("fit_score", 0),
            start_time=time.time(),
            actions=0,
            last_action="deterministic lever",
        )
        add_event(f"[W{worker_id}] Deterministic Lever: {job['title'][:40]}")
        _trace_progress(worker_id, "Opened Lever application", verbose=verbose, page=page)

        text = _visible_text(page)
        if _greenhouse_expired(text):
            return "expired", int((time.time() - start) * 1000)

        resume_input = page.locator("#resume-upload-input, input[name=resume]")
        if resume_input.count():
            _trace_progress(worker_id, "Uploading resume", verbose=verbose, page=page)
            resume_input.first.set_input_files(ctx.resume_pdf_path)
            page.wait_for_timeout(1000)

        _trace_progress(worker_id, "Filling contact info", verbose=verbose, page=page)
        _fill_name(page, "name", ctx.full_name)
        _fill_name(page, "email", ctx.email)
        _fill_name(page, "phone", ctx.phone_digits or ctx.phone)
        _fill_name(page, "location", ctx.current_location)
        _fill_name(page, "org", _current_company_text(ctx))
        _fill_name(page, "urls[LinkedIn]", ctx.linkedin_url)
        _fill_name(page, "urls[GitHub]", ctx.github_url)

        # Lever custom acknowledgements live in the additional-cards section.
        _trace_progress(worker_id, "Confirming acknowledgements", verbose=verbose, page=page)
        _check_selector(page, '[data-qa="additional-cards"] input[type="checkbox"]')

        page.wait_for_timeout(700)
        _trace_progress(worker_id, "Reviewing required fields", verbose=verbose, page=page)
        missing = _remaining_required_fields(page)
        if missing:
            add_event(f"[W{worker_id}] Deterministic missing: {', '.join(missing[:3])}")
            update_state(worker_id, last_action=f"missing {', '.join(missing[:2])}"[:35])
            return "failed:deterministic_missing_data", int((time.time() - start) * 1000)

        if dry_run:
            add_event(f"[W{worker_id}] Deterministic ready: {job['title'][:30]}")
            return "applied", int((time.time() - start) * 1000)

        try:
            _trace_progress(worker_id, "Submitting application", verbose=verbose, page=page)
            page.locator('button[type="submit"], input[type="submit"]').first.click(timeout=3_000)
            page.wait_for_timeout(3_000)
        except PlaywrightError:
            return "failed:submission_not_confirmed", int((time.time() - start) * 1000)

        post_submit = _visible_text(page).lower()
        if "captcha" in post_submit or "hcaptcha" in post_submit:
            return "captcha", int((time.time() - start) * 1000)
        if any(marker in post_submit for marker in ("application submitted", "thank you", "received your application")):
            return "applied", int((time.time() - start) * 1000)
        return "failed:submission_not_confirmed", int((time.time() - start) * 1000)


def _run_ashby_apply(
    *,
    job: dict,
    port: int,
    worker_id: int,
    dry_run: bool,
    use_base_resume: bool,
    verbose: bool,
) -> tuple[str, int]:
    ctx = build_applicant_context(job, use_base_resume=use_base_resume)
    target_url = job.get("application_url") or job["url"]
    start = time.time()

    with sync_playwright() as p:
        browser = _connect_browser_cdp(p, port)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
        except PlaywrightTimeoutError:
            return "failed:page_error", int((time.time() - start) * 1000)

        update_state(
            worker_id,
            status="applying",
            job_title=job["title"],
            company=job.get("site", ""),
            score=job.get("fit_score", 0),
            start_time=time.time(),
            actions=0,
            last_action="deterministic ashby",
        )
        add_event(f"[W{worker_id}] Deterministic Ashby: {job['title'][:40]}")
        _trace_progress(worker_id, "Opened Ashby application", verbose=verbose, page=page)

        text = _visible_text(page)
        if _greenhouse_expired(text):
            return "expired", int((time.time() - start) * 1000)

        resume_input = page.locator("#_systemfield_resume, input[type=file]")
        if resume_input.count():
            _trace_progress(worker_id, "Uploading resume", verbose=verbose, page=page)
            target = resume_input.first if resume_input.first.get_attribute("id") == "_systemfield_resume" else resume_input.last
            target.set_input_files(ctx.resume_pdf_path)
            page.wait_for_timeout(800)

        _trace_progress(worker_id, "Filling contact info", verbose=verbose, page=page)
        _fill_name(page, "_systemfield_name", ctx.full_name)
        _fill_name(page, "_systemfield_email", ctx.email)
        _fill_text(page, ["phone number"], ctx.phone_digits or ctx.phone)
        _fill_placeholder(page, ["pick date"], _start_date_text())
        _trace_progress(worker_id, "Answering screening questions", verbose=verbose, page=page)
        _click_button_option(page, ["authorized to work in the country"], "Yes")
        _click_button_option(
            page,
            ["require sponsorship for employment visa", "require sponsorship"],
            "Yes" if ctx.require_sponsorship else "No",
        )
        _click_button_option(
            page,
            ["able to work from our us office three days per week", "three days per week"],
            "Yes" if ctx.relocation_scope == "us" or ctx.country.lower() in {"usa", "united states"} else "No",
        )
        _click_options(
            page,
            ["Applicant Arbitration Agreement Acknowledgement"],
            ["I acknowledge that I have opened, read, and understood the Arbitration Agreement"],
        )
        _click_options(
            page,
            ["I hereby certify that I have not knowingly withheld any information"],
            ["I confirm I have read the above."],
        )

        page.wait_for_timeout(700)
        _trace_progress(worker_id, "Reviewing required fields", verbose=verbose, page=page)
        missing = _remaining_required_fields(page)
        if missing:
            add_event(f"[W{worker_id}] Deterministic missing: {', '.join(missing[:3])}")
            update_state(worker_id, last_action=f"missing {', '.join(missing[:2])}"[:35])
            return "failed:deterministic_missing_data", int((time.time() - start) * 1000)

        if dry_run:
            add_event(f"[W{worker_id}] Deterministic ready: {job['title'][:30]}")
            return "applied", int((time.time() - start) * 1000)

        try:
            _trace_progress(worker_id, "Submitting application", verbose=verbose, page=page)
            page.locator('button[type="submit"]').first.click(timeout=3_000)
            page.wait_for_timeout(3_000)
        except PlaywrightError:
            return "failed:submission_not_confirmed", int((time.time() - start) * 1000)

        post_submit = _visible_text(page).lower()
        if any(marker in post_submit for marker in ("application submitted", "thank you", "received your application")):
            return "applied", int((time.time() - start) * 1000)
        return "failed:submission_not_confirmed", int((time.time() - start) * 1000)


def _run_workday_apply(
    *,
    job: dict,
    port: int,
    worker_id: int,
    dry_run: bool,
    use_base_resume: bool,
    verbose: bool,
) -> tuple[str, int]:
    ctx = build_applicant_context(job, use_base_resume=use_base_resume)
    target_url = _normalize_workday_apply_url(job.get("application_url") or job["url"])
    start = time.time()

    with sync_playwright() as p:
        browser = _connect_browser_cdp(p, port)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
            _safe_wait_network_idle(page)
        except PlaywrightTimeoutError:
            return "failed:page_error", int((time.time() - start) * 1000)

        update_state(
            worker_id,
            status="applying",
            job_title=job["title"],
            company=job.get("site", ""),
            score=job.get("fit_score", 0),
            start_time=time.time(),
            actions=0,
            last_action="deterministic workday",
        )
        add_event(f"[W{worker_id}] Deterministic Workday: {job['title'][:40]}")
        _trace_progress(worker_id, "Opened Workday application", verbose=verbose, page=page)

        text = _visible_text(page).lower()
        if _greenhouse_expired(text) or "page you are looking for doesn't exist" in text:
            return "expired", int((time.time() - start) * 1000)

        _trace_progress(worker_id, "Entering Workday apply flow", verbose=verbose, page=page)
        _workday_enter_flow(page)

        auth_result = _workday_fill_auth_form(
            page,
            ctx,
            dry_run=dry_run,
            worker_id=worker_id,
            verbose=verbose,
        )
        if auth_result is not None:
            return auth_result, int((time.time() - start) * 1000)

        seen_signatures: set[str] = set()
        for step_index in range(12):
            _safe_wait_network_idle(page, timeout=6_000)
            page.wait_for_timeout(800)

            if _workday_confirmation_seen(page):
                return "applied", int((time.time() - start) * 1000)
            if "captcha" in _visible_text(page).lower():
                return "captcha", int((time.time() - start) * 1000)

            signature = _workday_step_signature(page)
            if signature in seen_signatures:
                return "failed:workday_navigation_stalled", int((time.time() - start) * 1000)
            seen_signatures.add(signature)

            heading = _workday_heading(page) or f"Step {step_index + 1}"
            _trace_progress(worker_id, f"Workday: {heading}", verbose=verbose, page=page)
            _workday_upload_resume(page, ctx)
            _workday_fill_common_fields(page, ctx)
            _workday_check_agreements(page)
            page.wait_for_timeout(500)

            missing = _remaining_required_fields(page)
            if _workday_has_submit(page):
                if missing:
                    add_event(f"[W{worker_id}] Workday missing: {', '.join(missing[:3])}")
                    update_state(worker_id, last_action=f"missing {', '.join(missing[:2])}"[:35])
                    return "failed:deterministic_missing_data", int((time.time() - start) * 1000)
                if dry_run:
                    add_event(f"[W{worker_id}] Deterministic ready: {job['title'][:30]}")
                    return "applied", int((time.time() - start) * 1000)

                _trace_progress(worker_id, "Submitting Workday application", verbose=verbose, page=page)
                if not _workday_click_submit(page):
                    return "failed:submission_not_confirmed", int((time.time() - start) * 1000)
                page.wait_for_timeout(2_000)
                _safe_wait_network_idle(page, timeout=8_000)
                if _workday_confirmation_seen(page):
                    return "applied", int((time.time() - start) * 1000)
                if "captcha" in _visible_text(page).lower():
                    return "captcha", int((time.time() - start) * 1000)
                return "failed:submission_not_confirmed", int((time.time() - start) * 1000)

            if missing:
                add_event(f"[W{worker_id}] Workday missing: {', '.join(missing[:3])}")
                update_state(worker_id, last_action=f"missing {', '.join(missing[:2])}"[:35])
                return "failed:deterministic_missing_data", int((time.time() - start) * 1000)

            _trace_progress(worker_id, "Continuing Workday wizard", verbose=verbose, page=page)
            if not _workday_click_next(page):
                if _workday_confirmation_seen(page):
                    return "applied", int((time.time() - start) * 1000)
                return "failed:workday_navigation_stalled", int((time.time() - start) * 1000)

            update_state(worker_id, last_action=f"workday step {step_index + 1}")

        return "failed:workday_navigation_stalled", int((time.time() - start) * 1000)


def run_deterministic_apply(
    *,
    job: dict,
    port: int,
    worker_id: int,
    dry_run: bool = False,
    use_base_resume: bool = False,
    verbose: bool = False,
) -> tuple[str, int]:
    """Run deterministic browser automation for supported ATS platforms."""
    platform_name = detect_apply_platform(job.get("application_url") or job.get("url"))
    if platform_name == "greenhouse":
        return _run_greenhouse_apply(
            job=job,
            port=port,
            worker_id=worker_id,
            dry_run=dry_run,
            use_base_resume=use_base_resume,
            verbose=verbose,
        )
    if platform_name == "lever":
        return _run_lever_apply(
            job=job,
            port=port,
            worker_id=worker_id,
            dry_run=dry_run,
            use_base_resume=use_base_resume,
            verbose=verbose,
        )
    if platform_name == "ashby":
        return _run_ashby_apply(
            job=job,
            port=port,
            worker_id=worker_id,
            dry_run=dry_run,
            use_base_resume=use_base_resume,
            verbose=verbose,
        )
    if platform_name == "workday":
        return _run_workday_apply(
            job=job,
            port=port,
            worker_id=worker_id,
            dry_run=dry_run,
            use_base_resume=use_base_resume,
            verbose=verbose,
        )
    return "failed:deterministic_unsupported_platform", 0
