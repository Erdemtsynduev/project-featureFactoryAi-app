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


@pytest.fixture
def workshop(tmp_path):
    (tmp_path / "ui.preview.json").write_text(
        json.dumps(
            {
                "captured_at": "2026-09-28T00:00:00Z",
                "source": "read-only portfolio",
                "items": [
                    {
                        "id": "104:ECL-11",
                        "title": "Body get-up acceptance",
                        "plan": "104",
                        "context": "Verify the rise",
                        "status": "ready",
                        "kind": "ticket",
                        "dependencies": [],
                        "path": "plan.md",
                    },
                    {
                        "id": "105:TEST",
                        "title": "<script>alert(1)</script>",
                        "plan": "105",
                        "context": "Literal text",
                        "status": "blocked",
                        "kind": "requirement",
                        "dependencies": [],
                        "path": "other.md",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
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
    server = create_server(service, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", service
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    service.coordinator.close()


def test_question_inbox_preserves_draft_and_submits_once(page, workshop):
    from playwright.sync_api import expect

    url, service = workshop
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.goto(url)
    expect(page.locator("#inbox")).to_contain_text("ждёт вашего ответа")
    expect(page.locator(".lane-blocked .card")).to_have_count(1)
    page.get_by_role("button", name="needs-answer → Ответить").click()
    expect(page.get_by_role("heading", name="Какой режим выбрать?")).to_be_visible()
    answer = page.get_by_role("textbox", name="Ваш ответ", exact=True)
    answer.fill("Только ручное подтверждение")
    page.get_by_role("button", name="Обновить", exact=True).click()
    expect(answer).to_have_value("Только ручное подтверждение")
    page.get_by_role("button", name="Подтвердить ответ").click()
    expect(page.locator("#inbox")).to_be_hidden()
    assert service.engine.store.get("needs-answer").step == "finish"
    assert service.state()["totals"]["calls"] == 0
    assert not errors


def test_graph_edit_connections_rename_drag_and_persist(page, workshop):
    from playwright.sync_api import expect

    url, _ = workshop
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.goto(url)
    page.get_by_role("button", name="Настроить main flow ↗").click()
    expect(page.locator(".flow-node")).to_have_count(10)
    page.get_by_role("button", name="Разработка · implement", exact=True).click()
    page.locator('#step-form input[name="id"]').fill("build")
    page.get_by_role("button", name="Применить к черновику").click()
    draft = json.loads(page.locator("#flow-json").input_value())
    assert ["done", "build"] in next(s for s in draft["steps"] if s["id"] == "tickets")[
        "transitions"
    ]
    page.locator("#edge-source").select_option("build")
    page.locator("#edge-target").select_option("interview")
    page.locator("#edge-outcome").fill("questions")
    page.get_by_role("button", name="Соединить", exact=True).click()
    expect(
        page.get_by_role("button", name="build: questions → interview", exact=True)
    ).to_have_count(1)
    node = page.get_by_role("button", name="build · build", exact=True)
    node.scroll_into_view_if_needed()
    before = node.get_attribute("transform")
    box = node.bounding_box()
    page.mouse.move(box["x"] + 60, box["y"] + 40)
    page.mouse.down()
    page.mouse.move(box["x"] + 85, box["y"] + 60, steps=5)
    page.mouse.up()
    assert node.get_attribute("transform") != before
    page.get_by_role("button", name="Сохранить черновик").click()
    page.reload()
    page.get_by_role("button", name="Редактор флоу", exact=True).click()
    expect(
        page.get_by_role("button", name="build: questions → interview", exact=True)
    ).to_have_count(1)
    page.get_by_role("button", name="build: questions → interview", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="Удалить связь", exact=True).click()
    expect(
        page.get_by_role("button", name="build: questions → interview", exact=True)
    ).to_have_count(0)
    page.locator("#edge-outcome").fill("questions")
    page.get_by_role("button", name="Выход review", exact=True).focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="Вход interview", exact=True).focus()
    page.keyboard.press("Enter")
    expect(
        page.get_by_role("button", name="review: questions → interview", exact=True)
    ).to_have_count(1)
    assert not errors


def test_snapshot_search_is_read_only_and_mobile_fits(page, workshop):
    from playwright.sync_api import expect

    url, service = workshop
    page.goto(url)
    page.get_by_role("button", name="Планы оркестратора", exact=True).click()
    page.locator("#plan-filter").select_option("104")
    expect(page.locator("#board .card")).to_have_count(1)
    page.locator("#board .card").click()
    expect(page.locator("#detail")).to_contain_text("ТОЛЬКО ПРОСМОТР")
    expect(page.locator("#detail .detail-actions")).to_have_count(0)
    assert len(service.state()["runs"]) == 1
    page.keyboard.press("Escape")
    page.locator("#task-search").fill("missing")
    expect(page.locator("#board .card")).to_have_count(0)
    page.locator("#task-search").fill("")
    page.locator("#plan-filter").select_option("105")
    expect(page.locator("#board .card")).to_contain_text("<script>alert(1)</script>")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.emulate_media(reduced_motion="reduce")
    assert (
        page.locator(".busy .pixel-person").first.evaluate(
            "el => getComputedStyle(el).animationName"
        )
        == "none"
    )
    Path("reports/ui").mkdir(parents=True, exist_ok=True)
    page.screenshot(path="reports/ui/workshop-mobile.png", full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1100})
    page.screenshot(path="reports/ui/workshop-desktop.png", full_page=True)


def test_settings_budget_validation_and_persistence(page, workshop):
    from playwright.sync_api import expect

    url, service = workshop
    page.goto(url)
    page.get_by_role("button", name="Команда и настройки", exact=True).click()
    page.locator('#budget-form input[name="max_calls"]').fill("12")
    page.locator('#budget-form input[name="max_planning_calls"]').fill("13")
    page.get_by_role("button", name="Сохранить лимиты").click()
    expect(page.locator("#notice")).to_contain_text("Queue budgets")
    assert service.engine.max_queue_calls == 40
    page.locator('#budget-form input[name="max_planning_calls"]').fill("3")
    page.get_by_role("button", name="Сохранить лимиты").click()
    expect(page.locator("#calls")).to_have_text("0 / 12")
    assert json.loads(service.settings_path.read_text())["max_calls"] == 12
    page.locator('#profile-form input[name="name"]').fill("reviewer")
    page.locator('#profile-form input[name="executable"]').fill(sys.executable)
    page.locator('#profile-form input[name="model"]').fill("test-model")
    page.get_by_role("button", name="Добавить / обновить профиль").click()
    page.get_by_role("button", name="Проверить и сохранить", exact=True).click()
    expect(page.locator(".profile-card")).to_contain_text("reviewer")
    page.reload()
    page.get_by_role("button", name="Команда и настройки", exact=True).click()
    page.locator(".profile-card").click()
    expect(page.locator('#profile-form input[name="model"]')).to_have_value("test-model")
    assert service.state()["totals"]["calls"] == 0


def test_projects_drag_drop_theme_language_and_question_demo(page, workshop, tmp_path):
    from playwright.sync_api import expect

    url, service = workshop
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    page.goto(url)
    for name in ("alpha", "beta"):
        root = tmp_path / name
        root.mkdir()
        page.locator("#manage-projects").click()
        form = page.locator("#project-form")
        form.locator('[name="id"]').fill(name)
        form.locator('[name="name"]').fill(name.title())
        form.locator('[name="workspace"]').fill(str(root))
        page.get_by_role("button", name="Сохранить проект", exact=True).click()
        expect(form).to_be_hidden()
    assert len(service.state()["projects"]) == 2
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
    page.reload()
    expect(page.locator("#project-select")).to_have_value("beta")
    page.locator("#live-board").click()
    card = page.locator('.card[data-run="drag-me"]')
    expect(card).to_be_visible()
    card.drag_to(page.locator('.lane[data-lane="running"]'))
    expect(page.locator('.lane-running .card[data-run="drag-me"]')).to_be_visible()
    assert not service.engine.store.get("drag-me").paused
    card.drag_to(page.locator('.lane[data-lane="ready"]'))
    expect(page.locator('.lane-ready .card[data-run="drag-me"]')).to_be_visible()
    assert service.engine.store.get("drag-me").paused
    page.locator("#project-select").select_option("alpha")
    expect(card).to_have_count(0)
    page.locator("#theme-select").select_option("dark")
    page.locator("#language-select").select_option("en")
    expect(page.get_by_role("button", name="Add project", exact=True)).to_be_visible()
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.get_by_role("button", name="Add project", exact=True)).to_be_visible()
    page.locator("#interactive-demo").click()
    expect(page.locator("#task-dialog")).to_be_visible()
    expect(page.locator(".question-thread h3")).to_contain_text("Which working mode")
    page.get_by_role("button", name="Approval before changes", exact=True).click()
    expect(page.get_by_role("textbox", name="Your answer", exact=True)).to_have_value(
        "Approval before changes"
    )
    expect(page.locator(".question-thread select")).to_be_hidden()
    page.get_by_role("textbox", name="Your answer", exact=True).fill("Approval first")
    page.get_by_role("button", name="Submit answer", exact=True).click()
    expect(page.locator(".question-thread")).to_have_count(0)
    page.keyboard.press("Escape")
    page.locator("#language-select").select_option("ru")
    expect(page.get_by_role("button", name="Добавить проект", exact=True)).to_be_visible()
    page.locator("#theme-select").select_option("light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.emulate_media(reduced_motion="reduce")
    assert (
        page.locator(".pixel-person").first.evaluate("el => getComputedStyle(el).animationName")
        == "none"
    )
    assert service.state()["totals"]["calls"] == 0
    assert not errors


def test_browser_notification_on_new_question(page, workshop):
    from playwright.sync_api import expect

    page.add_init_script("""
      window.notificationLog = [];
      window.Notification = class {
        static permission = "granted";
        static async requestPermission() { return "granted"; }
        constructor(title, options) { window.notificationLog.push({title, ...options}); }
      };
    """)
    url, service = workshop
    page.goto(url)
    expect(page.locator("#inbox")).to_be_visible()
    page.locator("#notifications").click()
    service.mutate("interactive-demo", {"language": "ru"})
    page.wait_for_function("() => window.notificationLog.length === 1", timeout=10000)
    assert page.evaluate("window.notificationLog[0].body") == "Пример вопроса команды"
    page.evaluate("refresh()")
    assert page.evaluate("window.notificationLog.length") == 1
