"""Real-browser interactions; opt in with FFAI_UI_TESTS=1 after installing a browser."""

import json
import os
import sys
import threading
from pathlib import Path

import pytest
from sdd_core.models import Step, Workflow
from sdd_ui.server import create_server
from sdd_ui.service import WorkspaceService

pytestmark = pytest.mark.skipif(
    os.environ.get("FFAI_UI_TESTS") != "1", reason="Opt-in UI browser tests"
)


def serve(service):
    server = create_server(service, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def workshop(tmp_path):
    """One task without a project, waiting for a human answer."""
    service = WorkspaceService(tmp_path / "ui.db")
    workspace = tmp_path / "project"
    workspace.mkdir()
    flow = Workflow(
        "question",
        "approve",
        (
            Step(
                "approve",
                "human",
                prompt="Какой режим выбрать?",
                transitions=(("approved", "finish"),),
            ),
            Step("finish", "finish"),
        ),
    )
    digest = service.engine.store.publish(flow)
    service.mutate(
        "create", {"id": "needs-answer", "definition": digest, "workspace": str(workspace)}
    )
    service.mutate("resume", {"id": "needs-answer", "version": 0})
    service.coordinator.tick()
    server, thread = serve(service)
    yield f"http://127.0.0.1:{server.server_port}", service
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    service.coordinator.close()


def ready(page, url):
    page.goto(url)
    page.wait_for_selector("html[data-ready=true]")


def test_empty_workspace_shows_welcome_instead_of_board(page, tmp_path):
    from playwright.sync_api import expect

    service = WorkspaceService(tmp_path / "ui.db")
    server, thread = serve(service)
    try:
        ready(page, f"http://127.0.0.1:{server.server_port}")
        expect(page.locator(".welcome")).to_be_visible()
        expect(page.locator("#board")).to_have_count(0)
        expect(page.locator("#new-task")).to_be_disabled()
        page.get_by_role("button", name="Команда").click()
        expect(page.locator(".welcome")).to_be_visible()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        service.coordinator.close()


def test_answer_draft_survives_closing_and_submits_once(page, workshop):
    from playwright.sync_api import expect

    url, service = workshop
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    ready(page, url)
    expect(page.locator("#project-select")).to_have_value("")
    card = page.locator('.lane-needs .card[data-run="needs-answer"]')
    expect(card).to_contain_text("Ждёт вашего ответа")
    card.get_by_role("button", name="Ответить").click()
    expect(page.get_by_role("heading", name="Какой режим выбрать?")).to_be_visible()
    answer = page.get_by_role("textbox", name="Ваш ответ", exact=True)
    answer.fill("Только ручное подтверждение")
    page.keyboard.press("Escape")
    expect(page.locator("dialog[open]")).to_have_count(0)
    expect(page).to_have_url(url + "/#board")
    card.click()
    expect(answer).to_have_value("Только ручное подтверждение")
    page.get_by_role("button", name="Одобрить", exact=True).click()
    expect(page.locator(".answer-panel")).to_have_count(0)
    expect(page.locator(".lane-needs .card")).to_have_count(0)
    assert service.engine.store.get("needs-answer").step == "finish"
    assert service.state()["totals"]["calls"] == 0
    assert not errors


def test_pipeline_edit_applies_on_change_and_persists(page, workshop):
    from playwright.sync_api import expect

    url, _ = workshop
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    ready(page, url + "/#flows")
    stages = page.locator("#graph .stage")
    expect(stages.first).to_be_visible()
    draft = lambda: json.loads(page.locator("#flow-json").input_value())  # noqa: E731
    expect(stages).to_have_count(len(draft()["steps"]))
    page.get_by_role("button", name="Разработка (agent)", exact=True).click()
    field = page.locator('#step-form input[name="id"]')
    field.fill("build")
    field.press("Tab")  # a change applies immediately: nothing to forget to apply
    tickets = next(s for s in draft()["steps"] if s["id"] == "tickets")
    assert ["done", "build"] in tickets["transitions"]
    expect(page.locator(".draft-state")).to_contain_text("Черновик сохранён")
    page.get_by_role("button", name="Insert after tickets (done)", exact=True).click()
    inserted = draft()["steps"][-1]
    assert inserted["transitions"] == [["done", "build"]]
    page.get_by_role("button", name="Connect from review", exact=True).click()
    assert page.locator("#edge-outcome").input_value() == "questions"
    page.get_by_role("button", name="Ваш ответ (human)", exact=True).click()
    link = page.get_by_role("button", name="review: questions → interview", exact=True)
    expect(link).to_have_count(1)
    page.reload()
    page.wait_for_selector("html[data-ready=true]")
    expect(link).to_have_count(1)
    link.focus()
    page.keyboard.press("Enter")
    page.keyboard.press("Delete")
    expect(link).to_have_count(0)
    # Replacing an edited draft asks first.
    page.get_by_role("button", name="Открыть шаблон").click()
    expect(page.get_by_role("heading", name="Заменить черновик?")).to_be_visible()
    page.get_by_role("button", name="Отмена").click()
    assert any(s["id"] == "build" for s in draft()["steps"])
    assert not errors


def test_new_task_dialog_keeps_its_draft_and_closes(page, workshop, tmp_path):
    from playwright.sync_api import expect

    url, service = workshop
    root = tmp_path / "alpha"
    root.mkdir()
    ready(page, url)
    page.locator("#project-add").click()
    dialog = page.locator("dialog[open]")
    dialog.locator('input[name="name"]').fill("Alpha App")
    expect(dialog.locator('input[name="id"]')).to_have_value("alpha-app")
    dialog.locator('input[name="workspace"]').fill(str(root))
    page.get_by_role("button", name="Сохранить", exact=True).click()
    expect(page.locator("#project-select")).to_have_value("alpha-app")
    expect(page.locator(".lane")).to_have_count(4)
    page.locator("#new-task").click()
    expect(page.get_by_text("Нужны профили: codex.")).to_be_visible()
    page.get_by_label("Название").fill("Экспорт отчётов")
    page.get_by_role("button", name="Закрыть").click()
    expect(page.locator("dialog[open]")).to_have_count(0)
    page.reload()
    page.wait_for_selector("html[data-ready=true]")
    page.locator("#new-task").click()
    expect(page.get_by_label("Название")).to_have_value("Экспорт отчётов")
    expect(page.locator(".draft-state")).to_have_text("Черновик сохранён")
    page.get_by_text("Свой сценарий").click()
    flow = Workflow("empty", "finish", (Step("finish", "finish"),))
    service.engine.store.publish(flow)
    page.get_by_label("Описание и критерии приёмки").fill("Отчёт в CSV")
    page.get_by_role("button", name="Создать задачу").click()
    expect(page.locator(".dialog-drawer")).to_be_visible()
    runs = [r for r in service.state()["runs"] if r["id"].startswith("eksport-otchetov-")]
    assert len(runs) == 1 and runs[0]["paused"]
    page.keyboard.press("Escape")
    page.locator("#new-task").click()
    expect(page.get_by_label("Название")).to_have_value("")


def test_search_filters_persist_and_mobile_fits(page, workshop):
    from playwright.sync_api import expect

    url, service = workshop
    ready(page, url)
    expect(page.locator("#board .card")).to_have_count(1)
    page.locator("#task-search").fill("missing")
    expect(page.locator("#board .card")).to_have_count(0)
    page.reload()
    expect(page.locator("#task-search")).to_have_value("missing")
    page.locator("#task-search").fill("")
    expect(page.locator("#board .card")).to_have_count(1)
    page.get_by_role("button", name="Дерево", exact=True).click()
    page.reload()
    expect(page.locator("#plans-board")).to_have_class("active")
    page.get_by_role("button", name="Колонки", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    Path("reports/ui").mkdir(parents=True, exist_ok=True)
    page.screenshot(path="reports/ui/board-mobile.png", full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(url + "/#team")
    expect(page.locator("#office canvas")).to_be_visible()
    page.screenshot(path="reports/ui/team-desktop.png", full_page=True)
    assert len(service.state()["runs"]) == 1


def test_budget_validation_and_persistence(page, workshop):
    from playwright.sync_api import expect

    url, service = workshop
    ready(page, url + "/#usage")
    form = page.locator("#budget-form")
    form.locator('input[name="max_calls"]').fill("12")
    form.locator('input[name="max_planning_calls"]').fill("13")
    form.get_by_role("button", name="Сохранить").click()
    expect(page.locator(".toast-error")).to_contain_text("Queue budgets")
    assert service.engine.max_queue_calls == 40
    form.locator('input[name="max_planning_calls"]').fill("3")
    form.get_by_role("button", name="Сохранить").click()
    expect(page.locator("#meters")).to_contain_text("0 / 12")
    assert json.loads(service.queue.settings_path.read_text())["max_calls"] == 12


def test_drag_drop_theme_language_and_question_demo(page, workshop, tmp_path):
    from playwright.sync_api import expect

    url, service = workshop
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    root = tmp_path / "beta"
    root.mkdir()
    service.mutate("project", {"id": "beta", "name": "Beta", "workspace": str(root)})
    flow = Workflow("drag", "finish", (Step("finish", "finish"),))
    service.mutate(
        "create",
        {
            "id": "drag-me",
            "title": "Move this card",
            "project": "beta",
            "definition": service.engine.store.publish(flow),
        },
    )
    page.set_viewport_size({"width": 1440, "height": 1200})
    ready(page, url)
    page.locator("#project-select").select_option("beta")
    card = page.locator('.card[data-run="drag-me"]')
    expect(card).to_contain_text("На паузе")
    card.drag_to(page.locator('.lane[data-lane="running"]'))
    expect(card).not_to_contain_text("На паузе")
    assert not service.engine.store.get("drag-me").paused
    card.drag_to(page.locator('.lane[data-lane="queue"]'))
    expect(card).to_contain_text("На паузе")
    assert service.engine.store.get("drag-me").paused
    page.locator("#theme-select").select_option("dark")
    page.locator("#language-select").select_option("en")
    expect(page.get_by_role("button", name="Board", exact=True)).to_be_visible()
    page.reload()
    page.wait_for_selector("html[data-ready=true]")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    service.mutate("interactive-demo", {"language": "en"})
    page.locator("#project-select").select_option("")
    page.locator(".card", has_text="Team question example").click()
    expect(page.locator(".answer-panel h3")).to_contain_text("Which working mode")
    recommended = page.get_by_role("radio", name="Approval before changes")
    expect(recommended).to_have_attribute("aria-checked", "true")
    recommended.focus()
    page.keyboard.press("ArrowDown")
    expect(page.get_by_role("radio", name="Work within the agreed scope")).to_have_attribute(
        "aria-checked", "true"
    )
    page.keyboard.press("1")
    expect(recommended).to_have_attribute("aria-checked", "true")
    page.get_by_role("button", name="Send answer", exact=True).click()
    expect(page.locator(".answer-panel")).to_have_count(0)
    page.keyboard.press("Escape")
    expect(page.locator("dialog[open]")).to_have_count(0)
    page.locator("#language-select").select_option("ru")
    expect(page.locator("#nav")).to_contain_text("Доска")
    assert service.state()["totals"]["calls"] == 0
    assert not errors


def test_browser_notification_on_new_question(page, workshop):
    page.add_init_script("""
      window.notificationLog = [];
      window.Notification = class {
        static permission = "granted";
        static async requestPermission() { return "granted"; }
        constructor(title, options) { window.notificationLog.push({title, ...options}); }
      };
    """)
    url, service = workshop
    ready(page, url)
    page.locator("#notifications").click()
    service.mutate("interactive-demo", {"language": "ru"})
    page.wait_for_function("() => window.notificationLog.length === 1", timeout=10000)
    assert page.evaluate("window.notificationLog[0].body") == "Пример вопроса команды"
    page.wait_for_timeout(3500)
    assert page.evaluate("window.notificationLog.length") == 1


def test_back_navigation_opens_and_closes_task(page, workshop):
    from playwright.sync_api import expect

    url, service = workshop
    ready(page, url)
    page.get_by_role("button", name="Сценарии").click()
    expect(page.locator("#graph")).to_be_visible()
    page.get_by_role("button", name="Назад", exact=True).click()
    expect(page.locator("#board")).to_be_visible()
    page.locator(".lane-needs .card").first.click()
    expect(page.locator(".dialog-drawer")).to_be_visible()
    page.go_back()
    expect(page.locator(".dialog-drawer")).to_have_count(0)
    page.go_forward()
    expect(page.locator(".dialog-drawer")).to_be_visible()
    page.get_by_role("button", name="Закрыть").click()
    expect(page.locator(".dialog-drawer")).to_have_count(0)
    page.get_by_role("button", name="Как это работает").click()
    expect(page.get_by_role("heading", name="Задачи и декомпозиция")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert service.state()["totals"]["calls"] == 0
    assert sys.executable
