#  Project:      dfe-docker
#  File:         _search_rule.py
#  Purpose:      The console steps that turn a HyperDX search into a rule and a hunt
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""Drive the console the way an analyst turns a search into a detection.

Internal support module - imported by the e2e suite, not executed directly. The
browser work lives here; the engine calls, the polling and the verdict stay with
the suite, which owns the stack. Playwright is imported only when a console opens,
so the unit tests and every other e2e test run without it.

The steps are the ones a tester takes: sign in, open Observe search on a source,
filter from the search bar and from the side panel, press Create Rule, read the
rule page it opens, add a hunt over the rule and trigger it on demand.
"""

import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from _pipeline import MARKER_EXPRESSIONS, escape_literal

STEP_TIMEOUT_MS = 30_000
# A search and its side-panel facets are ClickHouse queries behind two proxies.
QUERY_TIMEOUT_MS = 60_000
# A checkbox click registers at once, so one that has not shows within this.
FILTER_CHECKED_TIMEOUT_MS = 3_000
FILTER_CLICK_ATTEMPTS = 5
# A nested group renders only the rows near its viewport, so a sub-path is sought one screen at a time, wrapping while facets stream in.
NESTED_SCROLL_STEPS = 30
NESTED_SCROLL_WAIT_MS = 2_000
# Scrolls a nested group's list down one screen, or back to the top from its end.
_SCROLL_NESTED_LIST = """panel => {
    const box = [...panel.querySelectorAll("div")].find(
        (el) => getComputedStyle(el).overflowY === "auto"
    );
    if (!box) {
        return false;
    }
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 1;
    box.scrollTop = atEnd ? 0 : box.scrollTop + box.clientHeight;
    return true;
}"""
LOCAL_LOGIN_TAB = "Login with Local"
SEARCH_PATH = "/observe/search"
# The HyperDX route Create Rule posts to, which forwards the engine's answer.
CREATE_RULE_ROUTE = "/dfe/create-rule"
RULE_PATH = "/rules/"
HUNTS_PATH = "/hunts"
# The column the side panel lists a DFE source's JSON sub-paths under.
JSON_COLUMN = "_json"
# An option in whichever console select is open.
OPEN_OPTION = ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content"
PLAYWRIGHT_HINT = (
    "run the suite under an interpreter carrying Playwright, e.g. "
    "`uv run --with pyyaml --with playwright python3 scripts/test_e2e.py`, and "
    "`playwright install chrome` where Google Chrome is not installed"
)


class ConsoleError(Exception):
    """A console step the browser could not complete, with what it saw instead."""


def _step(name: str):
    """Report a browser error inside one console step as a ConsoleError, with a screenshot.

    The suite catches ConsoleError only. A Playwright error reaching it ends the
    run with a traceback, no teardown, no results and no picture of the page.
    """

    def wrap(method):
        @functools.wraps(method)
        def run(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except self._browser_error as error:
                self.shot(f"{name}-failed")
                reason = str(error).strip().splitlines()[0]
                raise ConsoleError(f"{name} failed: {reason}") from error

        return run

    return wrap


def search_condition(*, marker: str, where: str) -> str:
    """The search bar's SQL: the analyst's condition, held to this run's rows."""
    return f"{MARKER_EXPRESSIONS[0]} = '{escape_literal(marker)}' AND ({where})"


def rule_id_from_url(url: str) -> str | None:
    """The rule a console rules URL names, or None for any other page.

    Create Rule opens `/rules/<id>`, and the console settles it on the rules list
    with the rule selected, `/rules?name=<id>`.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.startswith(RULE_PATH):
        return unquote(path[len(RULE_PATH) :]) or None
    if path == RULE_PATH.rstrip("/"):
        names = parse_qs(parsed.query).get("name") or []
        return names[0] if names and names[0] else None
    return None


def hunt_identifier(marker: str) -> str:
    """A hunt name the console's form accepts: lowercase, digits and underscores."""
    name = re.sub(r"[^a-z0-9_]+", "_", marker.lower()).strip("_")
    return name if name[:1].isalpha() else f"h_{name}"


def missing_fragments(where_clause: str, fragments: list[str]) -> list[str]:
    """The fragments a stored WHERE clause does not carry, in the order given."""
    return [fragment for fragment in fragments if fragment not in where_clause]


def _label(name: str) -> re.Pattern[str]:
    """Match a form field by label, with or without the required marker on either side of it."""
    return re.compile(rf"^\s*\*?\s*{re.escape(name)}\s*\*?\s*$")


@dataclass(frozen=True, slots=True)
class HuntPick:
    """One select on the hunt form, and the option a tester chooses in it.

    Attributes:
        label: The field's label.
        search: What to type to narrow the options, or empty to type nothing. A
            select that refetches as it is typed into closes while it loads.
        option: The option's text, as the select lists it.
    """

    label: str
    search: str
    option: str


@dataclass(frozen=True, slots=True)
class SearchRule:
    """The rule the console created from a search.

    Attributes:
        rule_id: The id the console opened the rule page on.
        detail: The rule page's text, as a tester reads it.
    """

    rule_id: str
    detail: str


class Console:
    """One signed-in browser session on the console, closed by `close`."""

    def __init__(
        self, *, ui_url: str, shots_dir: Path, headed: bool, trusted_spki: str = ""
    ) -> None:
        try:
            from playwright.sync_api import Error, expect, sync_playwright
        except ModuleNotFoundError as error:
            raise ConsoleError(
                f"no playwright in this interpreter -- {PLAYWRIGHT_HINT}"
            ) from error
        self._browser_error = Error
        self._expect = expect
        self.ui_url = ui_url.rstrip("/")
        self.shots_dir = shots_dir
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.shots: list[Path] = []
        self._playwright = sync_playwright().start()
        # Chrome takes no CA bundle, so it trusts the key of the leaf the suite has verified by chain and name.
        args = (
            [f"--ignore-certificate-errors-spki-list={trusted_spki}"]
            if trusted_spki
            else []
        )
        try:
            self._browser = self._playwright.chromium.launch(
                args=args, channel="chrome", headless=not (headed)
            )
        except Exception as error:
            self._playwright.stop()
            raise ConsoleError(
                f"Chrome did not start: {error} -- {PLAYWRIGHT_HINT}"
            ) from error
        self._context = self._browser.new_context(
            viewport={"width": 1600, "height": 1000}
        )
        self.page = self._context.new_page()
        self.page.set_default_timeout(STEP_TIMEOUT_MS)

    def close(self) -> None:
        """End the session and the browser."""
        self._context.close()
        self._browser.close()
        self._playwright.stop()

    def shot(self, name: str, page=None) -> Path:
        """Screenshot a page into the shots directory, numbered in step order."""
        path = self.shots_dir / f"{len(self.shots) + 1:02d}-{name}.png"
        (page or self.page).screenshot(path=str(path), full_page=True)
        self.shots.append(path)
        return path

    def sign_in(self, *, username: str, password: str) -> None:
        """Log in through the console's own form as a local account."""
        page = self.page
        page.goto(f"{self.ui_url}/login", wait_until="domcontentloaded")
        local = page.get_by_role("tab", name=LOCAL_LOGIN_TAB, exact=True)
        if local.count():
            local.first.click()
        page.get_by_role("textbox", name=_label("Username")).fill(username)
        page.get_by_role("textbox", name=_label("Password")).fill(password)
        page.get_by_role("button", name="Login", exact=True).click()
        page.wait_for_url(lambda url: "/login" not in url, timeout=QUERY_TIMEOUT_MS)
        self.shot("signed-in")

    def _search_frame(self):
        """The HyperDX search the console embeds on Observe, once it has loaded."""
        self.page.goto(f"{self.ui_url}{SEARCH_PATH}", wait_until="domcontentloaded")
        frame = self.page.frame_locator("iframe").first
        frame.get_by_test_id("where-language-switch").wait_for(
            state="visible", timeout=QUERY_TIMEOUT_MS
        )
        return frame

    def _enter_where(self, frame, text: str) -> None:
        """Replace the search bar's WHERE with text, as typed into its SQL editor."""
        switch = frame.get_by_test_id("where-language-switch")
        language = switch.get_by_role("combobox")
        if language.input_value() != "SQL":
            language.click()
            frame.get_by_role("option", name="SQL", exact=True).click()
        editor = switch.locator("xpath=..").locator(".cm-content")
        editor.click()
        self.page.keyboard.press("ControlOrMeta+A")
        # Inserted whole: CodeMirror can drop keys typed one at a time under load.
        self.page.keyboard.insert_text(text)
        self.page.keyboard.press("Escape")

    def _select_source(self, frame, source: str) -> None:
        """Put the search on source, once the page has picked a source of its own.

        The page selects a default source shortly after the search bar shows, and
        picking the option already selected clears it, which leaves the search
        running nothing.
        """
        selector = frame.get_by_test_id("source-selector")
        selector.wait_for(state="visible")
        try:
            self._expect(selector).not_to_have_value("", timeout=QUERY_TIMEOUT_MS)
        except AssertionError as error:
            self.shot("no-source")
            raise ConsoleError("the search page never selected a source") from error
        if selector.input_value().strip() != source:
            selector.click()
            frame.get_by_role("option", name=source, exact=True).click()
        try:
            self._expect(selector).to_have_value(source)
        except AssertionError as error:
            self.shot("source-not-selected")
            raise ConsoleError(
                f"the search is on {selector.input_value()!r}, not {source!r}"
            ) from error

    def _apply_json_filter(self, frame, *, path: str, value: str) -> None:
        """Pick one value of one JSON sub-path in the side panel."""
        group = frame.get_by_test_id(f"nested-filter-group-{JSON_COLUMN}")
        group.wait_for(state="visible", timeout=QUERY_TIMEOUT_MS)
        control = group.get_by_test_id("nested-filter-group-control")
        if control.get_attribute("aria-expanded") != "true":
            control.click()
        sub_path = group.get_by_text(
            re.compile(rf"^{re.escape(path)}(\s*\(\d+\))?$")
        ).first
        panel = group.get_by_test_id("nested-filter-group-panel")
        for _ in range(NESTED_SCROLL_STEPS):
            try:
                sub_path.wait_for(state="visible", timeout=NESTED_SCROLL_WAIT_MS)
                break
            except self._browser_error:
                if not (panel.evaluate(_SCROLL_NESTED_LIST)):
                    break
        sub_path.wait_for(state="visible", timeout=QUERY_TIMEOUT_MS)
        sub_path.click()
        box = frame.get_by_test_id(f"filter-checkbox-{path}-{value}-input")
        box.wait_for(state="attached", timeout=QUERY_TIMEOUT_MS)
        # The applied filter shows as a pill in the search bar, while the checkbox can re-render away as the list re-measures.
        pill = frame.get_by_text(f"{JSON_COLUMN}.{path} = {value}")
        # Facet values stream in and re-render, which can drop a single click.
        for _ in range(FILTER_CLICK_ATTEMPTS):
            try:
                if not (box.is_checked(timeout=FILTER_CHECKED_TIMEOUT_MS)):
                    box.scroll_into_view_if_needed(timeout=FILTER_CHECKED_TIMEOUT_MS)
                    box.click(timeout=FILTER_CHECKED_TIMEOUT_MS)
            except self._browser_error:
                # A checkbox the list re-rendered away is tried again on the next pass.
                pass
            try:
                self._expect(pill).to_be_visible(timeout=FILTER_CHECKED_TIMEOUT_MS)
                return
            except AssertionError:
                continue
        raise ConsoleError(f"the side panel would not select {path} = {value}")

    @staticmethod
    def _refusal(answers: list) -> str:
        """What the create-rule route answered, the engine's reason included."""
        if not (answers):
            return f"nothing answered {CREATE_RULE_ROUTE}"
        response = answers[-1]
        try:
            text = response.text()
        except Exception as error:
            text = f"(body unreadable: {error})"
        try:
            reason = json.loads(text).get("message") or text
        except (ValueError, AttributeError):
            reason = text
        return f"{CREATE_RULE_ROUTE} answered HTTP {response.status}: {reason}"

    @_step("create-rule")
    def create_rule_from_search(
        self,
        *,
        source: str,
        search: str,
        filter_path: str,
        filter_value: str,
        view_rows: int,
    ) -> SearchRule:
        """Search, filter from the side panel, press Create Rule and read the rule page.

        Args:
            source: The HyperDX source to search.
            search: The search bar's SQL.
            filter_path: The JSON sub-path to filter on in the side panel.
            filter_value: The value to pick for it.
            view_rows: The rows the filtered view shows. Create Rule builds from the
                query the view last ran, which trails the filter pill, so the button
                is pressed only once the view holds exactly these.

        Raises:
            ConsoleError: A step failed, or Create Rule opened no rule page.
        """
        frame = self._search_frame()
        self._select_source(frame, source)
        self._enter_where(frame, search)
        frame.get_by_test_id("search-submit-button").click()
        frame.get_by_test_id("search-results-table").wait_for(
            state="visible", timeout=QUERY_TIMEOUT_MS
        )
        self._apply_json_filter(frame, path=filter_path, value=filter_value)
        # The search bar shows each applied side-panel filter as a pill.
        frame.get_by_text(f"{JSON_COLUMN}.{filter_path} = {filter_value}").wait_for(
            state="visible", timeout=QUERY_TIMEOUT_MS
        )
        rows = frame.get_by_text(re.compile(rf"^{view_rows} Results?$"))
        try:
            rows.wait_for(state="visible", timeout=QUERY_TIMEOUT_MS)
        except Exception as error:
            self.shot("search-never-narrowed")
            raise ConsoleError(
                f"the filtered view never showed {view_rows} row(s)"
            ) from error
        self.shot("search-filtered")

        answers = []

        def _record(response) -> None:
            if CREATE_RULE_ROUTE in response.url:
                answers.append(response)

        button = frame.get_by_test_id("create-rule-from-search-button")
        self.page.on("response", _record)
        try:
            with self._context.expect_page(timeout=QUERY_TIMEOUT_MS) as opened:
                button.click()
            rule_page = opened.value
        except Exception as error:
            self.shot("create-rule-refused")
            raise ConsoleError(
                f"Create Rule opened no rule page: {self._refusal(answers)}"
            ) from error
        finally:
            self.page.remove_listener("response", _record)

        rule_page.wait_for_load_state("domcontentloaded")
        rule_id = rule_id_from_url(rule_page.url)
        if rule_id is None:
            self.shot("create-rule-landed-elsewhere", page=rule_page)
            raise ConsoleError(f"Create Rule opened {rule_page.url}, not a rule page")
        heading = rule_page.get_by_text("Rule Configuration:")
        heading.wait_for(state="visible", timeout=QUERY_TIMEOUT_MS)
        detail = rule_page.locator("main").inner_text()
        self.shot("rule-created", page=rule_page)
        rule_page.close()
        return SearchRule(rule_id=rule_id, detail=detail)

    @_step("create-hunt")
    def create_hunt(self, *, name: str, target: str, picks: list[HuntPick]) -> None:
        """Add a hunt through the console's form.

        Args:
            name: The hunt's identifier.
            target: The table its detections land in.
            picks: What to choose in each of the form's selects.

        Raises:
            ConsoleError: The form did not report the hunt created.
        """
        page = self.page
        page.goto(f"{self.ui_url}{HUNTS_PATH}", wait_until="domcontentloaded")
        page.get_by_role("button", name="Add Hunt", exact=True).first.click()
        drawer = page.get_by_role("dialog", name="Add Hunt")
        drawer.wait_for(state="visible")
        drawer.get_by_role("textbox", name=_label("Name")).fill(name)
        for pick in picks:
            self._pick(drawer, pick)
        drawer.get_by_role("textbox", name=_label("Target Table")).fill(target)
        self.shot("hunt-form")
        drawer.get_by_role("button", name="Add Hunt", exact=True).click()
        try:
            page.get_by_text("Hunt created successfully").wait_for(
                state="visible", timeout=QUERY_TIMEOUT_MS
            )
        except Exception as error:
            self.shot("hunt-refused")
            raise ConsoleError(
                f"the hunt form did not create '{name}': {drawer.inner_text()}"
            ) from error
        self.shot("hunt-created")

    def _pick(self, scope, pick: HuntPick) -> None:
        """Choose one option in a form select, narrowing it first where a search is given."""
        box = scope.get_by_role("combobox", name=_label(pick.label))
        box.click()
        if pick.search:
            box.fill(pick.search)
        option = self.page.locator(OPEN_OPTION).filter(
            has_text=re.compile(rf"^\s*{re.escape(pick.option)}\s*$")
        )
        option.first.wait_for(state="visible", timeout=QUERY_TIMEOUT_MS)
        option.first.click()
        self.page.keyboard.press("Escape")

    @_step("trigger-hunt")
    def trigger_hunt(self, name: str) -> tuple[int, object]:
        """Trigger the selected hunt on demand, returning what the engine answered."""
        page = self.page
        page.get_by_role("button", name=re.compile(r"^Trigger On-Demand")).click()
        modal = page.get_by_role("dialog", name="Trigger On-Demand Hunt")
        modal.wait_for(state="visible")
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith(f"/hunts/{name}/run")
            ),
            timeout=QUERY_TIMEOUT_MS,
        ) as answered:
            modal.get_by_role("button", name="Trigger Hunt", exact=True).click()
        response = answered.value
        try:
            body = response.json()
        except Exception:
            body = response.text()
        self.shot("hunt-triggered")
        return response.status, body
