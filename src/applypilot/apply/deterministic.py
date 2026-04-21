"""Deterministic ATS automation for common application platforms.

The goal is to use direct DOM automation for well-known ATS forms and reserve
LLM agents for pages that truly need reasoning.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.apply.dashboard import add_event, get_state, update_state
from applypilot.scoring.pdf import parse_resume

log = logging.getLogger(__name__)


def detect_apply_platform(url: str | None) -> str | None:
    """Detect the ATS platform from an application URL."""
    if not url:
        return None
    lower = url.lower()
    if "greenhouse.io" in lower:
        return "greenhouse"
    if "lever.co" in lower:
        return "lever"
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
        locator = page.get_by_label(pattern).first
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
) -> tuple[str, int]:
    ctx = build_applicant_context(job, use_base_resume=use_base_resume)
    target_url = job.get("application_url") or job["url"]
    start = time.time()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
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
            file_inputs.first.set_input_files(ctx.resume_pdf_path)

        # Education block
        _select_choice(page, ["school"], [ctx.education_school])
        _select_choice(page, ["degree"], _degree_terms(ctx.education_degree or ctx.education_level))
        _select_choice(page, ["discipline"], [ctx.education_discipline or "Physics", "Physics"])
        _fill_text(page, ["end date year"], ctx.education_end_year)

        # Common screening answers
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
        missing = _remaining_required_fields(page)
        if missing:
            add_event(f"[W{worker_id}] Deterministic missing: {', '.join(missing[:3])}")
            update_state(worker_id, last_action=f"missing {', '.join(missing[:2])}"[:35])
            return "failed:deterministic_missing_data", int((time.time() - start) * 1000)

        if dry_run:
            add_event(f"[W{worker_id}] Deterministic ready: {job['title'][:30]}")
            return "applied", int((time.time() - start) * 1000)

        try:
            page.get_by_text("Submit application", exact=False).first.click(timeout=3_000)
            page.wait_for_timeout(2_500)
        except PlaywrightError:
            return "failed:submission_not_confirmed", int((time.time() - start) * 1000)

        post_submit = _visible_text(page).lower()
        if any(marker in post_submit for marker in ("application submitted", "thank you", "received your application")):
            return "applied", int((time.time() - start) * 1000)
        return "failed:submission_not_confirmed", int((time.time() - start) * 1000)


def run_deterministic_apply(
    *,
    job: dict,
    port: int,
    worker_id: int,
    dry_run: bool = False,
    use_base_resume: bool = False,
) -> tuple[str, int]:
    """Run deterministic browser automation for supported ATS platforms."""
    platform_name = detect_apply_platform(job.get("application_url") or job.get("url"))
    if platform_name != "greenhouse":
        return "failed:deterministic_unsupported_platform", 0
    return _run_greenhouse_apply(
        job=job,
        port=port,
        worker_id=worker_id,
        dry_run=dry_run,
        use_base_resume=use_base_resume,
    )
