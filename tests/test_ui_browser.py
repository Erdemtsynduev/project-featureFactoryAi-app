"""Real-browser interactions; opt in with FFAI_UI_TESTS=1 after installing a browser."""

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from sdd_core.models import Step, Workflow
from sdd_factory.model import TaskRecord
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
    """Open a route; the bare address lands on the board for these scenarios."""
    page.goto(url if "#" in url else url + "/#board")
    page.wait_for_selector("html[data-ready=true]")


def kanban(page):
    """Switch the board from the default tree to status columns."""
    page.locator("#live-board").click()


def test_empty_workspace_shows_welcome_instead_of_board(page, tmp_path):
    from playwright.sync_api import expect

    service = WorkspaceService(tmp_path / "ui.db")
    server, thread = serve(service)
    try:
        ready(page, f"http://127.0.0.1:{server.server_port}")
        expect(page.locator(".welcome")).to_be_visible()
        expect(page.locator("#board")).to_have_count(0)
        expect(page.locator("#new-task")).to_be_disabled()
        page.get_by_role("button", name="Обзор").click()
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
    kanban(page)
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
    # The editor opens read-only: no edit controls, disabled fields.
    expect(page.locator("#add-step")).to_be_hidden()
    expect(page.locator('#step-form input[name="id"]')).to_be_disabled()
    page.locator("#flow-edit").click()
    expect(page.locator("#add-step")).to_be_visible()
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
    expect(page.locator(".tree")).to_be_visible()
    page.locator("#new-task").click()
    expect(page.get_by_text("Нужны профили: codex.")).to_be_visible()
    expect(page.locator(".next-steps")).to_contain_text("Планировщик режет спецификацию")
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
    kanban(page)
    expect(page.locator("#board .card")).to_have_count(1)
    page.locator("#task-search").fill("missing")
    expect(page.locator("#board .card")).to_have_count(0)
    page.reload()
    expect(page.locator("#task-search")).to_have_value("missing")
    page.locator("#task-search").fill("")
    expect(page.locator("#board .card")).to_have_count(1)
    page.get_by_role("button", name="По планам", exact=True).click()
    page.reload()
    expect(page.locator("#plans-board")).to_have_class("active")
    page.get_by_role("button", name="Канбан", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    Path("reports/ui").mkdir(parents=True, exist_ok=True)
    page.screenshot(path="reports/ui/board-mobile.png", full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(url + "/#overview")
    page.locator(".team-panel > summary").click()  # folded while nobody works
    expect(page.locator("#office canvas")).to_be_visible()
    expect(page.locator(".overview-tiles")).to_contain_text("Нужны вы")
    page.screenshot(path="reports/ui/overview-desktop.png", full_page=True)
    assert len(service.state()["runs"]) == 1


def test_budget_validation_and_persistence(page, workshop):
    from playwright.sync_api import expect

    url, service = workshop
    ready(page, url + "/#usage")
    form = page.locator("#budget-form")
    form.locator('input[name="max_calls"]').fill("12")
    form.locator('input[name="max_planning_calls"]').fill("13")
    form.get_by_role("button", name="Сохранить").click()
    expect(page.locator(".toast-error")).to_contain_text("Planning calls are part of all calls")
    assert service.engine.max_queue_calls is None, "no cap until one is saved"
    form.locator('input[name="max_planning_calls"]').fill("3")
    form.get_by_role("button", name="Сохранить").click()
    expect(page.locator("#meters")).to_contain_text("0 / 12")
    assert json.loads(service.catalog.records.preference("queue-settings"))["max_calls"] == 12


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
    kanban(page)
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


def test_notification_center_and_system_notification(page, workshop):
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
    service.queue.settings["running"] = True
    service.queue._watch(0)
    ready(page, url)
    page.locator("#notifications").click()
    panel = page.locator(".dialog-drawer")
    expect(panel).to_contain_text("Нужны вы: 1")
    panel.locator("#notify-system").check()
    demo = service.mutate("interactive-demo", {"language": "ru"})
    service.queue._watch(1)
    expect(panel).to_contain_text("Вопрос агента", timeout=20000)
    page.wait_for_function("() => window.notificationLog.length === 1", timeout=20000)
    assert page.evaluate("window.notificationLog[0].body") == "Пример вопроса команды"
    panel.get_by_role("button", name="Пример вопроса команды").first.click()
    expect(page).to_have_url(url + "/#task/" + demo["id"])


def test_bulk_resume_dialog_and_queue_start_explain_what_happens(page, workshop, tmp_path):
    from playwright.sync_api import expect

    url, service = workshop
    root = tmp_path / "gamma"
    root.mkdir()
    service.mutate("project", {"id": "gamma", "name": "Gamma", "workspace": str(root)})
    flow = Workflow("empty", "done", (Step("done", "finish"),))
    definition = service.engine.store.publish(flow)
    service.tasks.create({"id": "one", "project": "gamma", "definition": definition})
    service.tasks.create(
        {"id": "two", "project": "gamma", "definition": definition, "dependencies": ["one"]}
    )
    ready(page, url)
    page.locator("#project-select").select_option("gamma")
    page.locator("#queue-toggle").click()
    dialog = page.locator("dialog[open]")
    expect(dialog).to_contain_text("все стоят на паузе")
    expect(dialog.get_by_text("Готовые к старту: 1")).to_be_visible()
    expect(dialog.get_by_text("Все на паузе: 2")).to_be_visible()
    dialog.get_by_role("button", name="Запустить", exact=True).click()
    expect(page.locator(".toast-success")).to_contain_text("Запущена 1 задача")
    assert not service.engine.store.get("one").paused
    assert service.engine.store.get("two").paused
    assert service.settings["running"] is True
    page.get_by_role("button", name="Пауза всем").click()
    page.locator("dialog[open]").get_by_role("button", name="Пауза всем").click()
    expect(page.locator(".toast-success").first).to_contain_text("На паузе")
    assert service.engine.store.get("one").paused
    page.locator("#about").click()
    expect(page.locator("dialog[open]")).to_contain_text("sdd-runtime")


def test_back_navigation_opens_and_closes_task(page, workshop):
    from playwright.sync_api import expect

    url, service = workshop
    ready(page, url)
    page.get_by_role("button", name="Сценарии").click()
    expect(page.locator("#graph")).to_be_visible()
    page.get_by_role("button", name="Назад", exact=True).click()
    expect(page.locator("#board")).to_be_visible()
    kanban(page)
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


def test_plan_starts_with_outside_dependencies_and_lanes_page(page, workshop, tmp_path):
    from playwright.sync_api import expect

    url, service = workshop
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    root = tmp_path / "delta"
    root.mkdir()
    service.mutate("project", {"id": "delta", "name": "Delta", "workspace": str(root)})
    flow = Workflow("empty", "done", (Step("done", "finish"),))
    definition = service.engine.store.publish(flow)

    def create(identifier, plan, title, dependencies=()):
        service.engine.create(
            identifier, definition, root, "", "rev", time.time(), tuple(dependencies)
        )
        service.catalog.save_task(
            identifier, TaskRecord(project="delta", kind="ticket", title=title, plan=plan)
        )

    create("base", "p2", "Base contract")
    create("feature", "p1", "Feature on top", ["base"])
    for n in range(43):
        create(f"filler-{n:02}", "p3", f"Filler {n:02}")
    ready(page, url)
    page.locator("#project-select").select_option("delta")
    kanban(page)
    queue = page.locator('.lane[data-lane="queue"]')
    expect(queue.locator(".card")).to_have_count(40)
    queue.get_by_role("button", name="Показать ещё 5").click()
    expect(queue.locator(".card")).to_have_count(45)

    page.locator("#plans-board").click()
    row = page.locator('.plan[data-plan="p1"]')
    expect(row).to_contain_text("ждёт 1 задачу вне плана")
    row.get_by_role("button", name="Запустить план").click()
    dialog = page.locator("dialog[open]")
    expect(dialog).to_contain_text("Запустить и 1 задачу, от которой они зависят")
    expect(dialog).to_contain_text("Base contract")
    dialog.get_by_label("Сразу запустить очередь").uncheck()
    dialog.get_by_role("button", name="Запустить", exact=True).click()
    expect(page.locator(".toast-success")).to_contain_text("Запущено 2 задачи")
    assert not service.engine.store.get("feature").paused
    assert not service.engine.store.get("base").paused
    assert service.engine.store.get("filler-00").paused
    assert service.settings["running"] is False

    row.locator(".plan-name").click()
    row.locator('.card[data-run="feature"]').click()
    panel = page.locator(".waiting-panel")
    expect(panel).to_contain_text("Ждёт приёмки 1 задачи")
    panel.get_by_role("button", name="Base contract").click()
    expect(page).to_have_url(url + "/#task/base")
    assert service.state()["totals"]["calls"] == 0
    assert not errors


def test_tree_nests_tickets_and_closes_a_partly_done_feature(page, workshop, tmp_path):
    from playwright.sync_api import expect

    url, service = workshop
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    root = tmp_path / "tree"
    root.mkdir()
    service.mutate("project", {"id": "tree", "name": "Tree", "workspace": str(root)})
    definition = service.engine.store.publish(Workflow("empty", "done", (Step("done", "finish"),)))

    def create(identifier, title, kind="ticket", parent="", done=False):
        run = service.engine.create(identifier, definition, root, "", "rev", time.time())
        service.catalog.save_task(
            identifier, TaskRecord(project="tree", kind=kind, title=title, parent=parent)
        )
        if done:
            run = service.engine.command(identifier, "resume", identifier, run.version, 1)
            service.engine.dispatch(identifier, 2, identifier + "-a")

    create("checkout", "Checkout", kind="feature", done=True)
    create("checkout-api", "Pay endpoint", parent="checkout", done=True)
    create("checkout-ui", "Pay screen", parent="checkout")
    # A ticket split further: its own planning is done, one sub-ticket is not.
    create("checkout-limits", "Rate limits", parent="checkout", done=True)
    create("checkout-limits-count", "Counter", parent="checkout-limits", done=True)
    create("checkout-limits-block", "Blocking", parent="checkout-limits")
    create("typo", "Fix a typo", kind="task")

    page.set_viewport_size({"width": 1440, "height": 1000})
    ready(page, url)
    page.locator("#project-select").select_option("tree")
    feature = page.locator('.tree-row[data-run="checkout"]')
    expect(feature).to_have_attribute("aria-level", "1")
    expect(feature).to_contain_text("Сделано частично: 1/3")
    expect(feature.locator(".plan-progress")).to_have_text("1/3")
    limits = page.locator('.tree-row[data-run="checkout-limits"]')
    expect(limits).to_contain_text("1/2")
    expect(page.locator('.tree-row[data-run="checkout-limits-block"]')).to_have_attribute(
        "aria-level", "3"
    )
    Path("reports/ui").mkdir(parents=True, exist_ok=True)
    page.screenshot(path="reports/ui/board-tree.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="reports/ui/board-tree-mobile.png", full_page=True)
    page.set_viewport_size({"width": 1440, "height": 1000})

    # Folding hides the children; the fold survives a reload.
    limits.get_by_role("button", name="Свернуть").click()
    expect(page.locator('.tree-row[data-run="checkout-limits-block"]')).to_have_count(0)
    page.reload()
    page.wait_for_selector("html[data-ready=true]")
    expect(page.locator('.tree-row[data-run="checkout-limits-block"]')).to_have_count(0)
    limits.get_by_role("button", name="Развернуть").click()

    # Focus keeps a match's ancestors as dimmed context.
    page.locator(".chips .chip", has_text="Готово").click()
    expect(page.locator('.tree-row[data-run="checkout"]')).to_have_class(
        "tree-row kind-feature tone-idle context"
    )
    expect(page.locator('.tree-row[data-run="typo"]')).to_have_count(0)
    page.locator(".chips .chip", has_text="Все").click()

    # In the kanban the approved feature is left to its tickets.
    kanban(page)
    expect(page.locator('.card[data-run="checkout"]')).to_have_count(0)
    expect(page.locator('.card[data-run="checkout-ui"]')).to_have_count(1)
    page.locator("#tree-board").click()

    feature.get_by_role("button", name="Закрыть частично").click()
    dialog = page.locator("dialog[open]")
    expect(dialog).to_contain_text("Готово 1 из 3")
    dialog.get_by_role("button", name="Закрыть частично").click()
    expect(page.locator(".toast-success")).to_contain_text("отдельными задачами стали: 2")
    expect(feature).to_contain_text("Закрыто досрочно")
    expect(page.locator('.tree-row[data-run="checkout-ui"]')).to_have_attribute("aria-level", "1")
    record = service.catalog.task("checkout-limits")
    assert record.parent == "" and record.origin == "checkout"
    assert service.catalog.task("checkout-api").parent == "checkout"
    assert service.state()["totals"]["calls"] == 0
    assert not errors


def test_project_settings_choose_a_tracker_without_storing_a_token(page, workshop, tmp_path):
    from playwright.sync_api import expect

    url, service = workshop
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    root = tmp_path / "linear-project"
    root.mkdir()
    service.mutate("project", {"id": "lin", "name": "Lin", "workspace": str(root)})
    page.goto(url)
    page.wait_for_selector("html[data-ready=true]")
    page.locator("#project-select").select_option("lin")
    page.locator("#project-edit").click()
    page.locator("select[name=tracker_kind]").select_option("linear")
    settings = page.locator("textarea[name=tracker_settings]")
    settings.fill("team: ENG\nsource: projects\napi_key: lin_api_secret")
    page.get_by_role("button", name="Сохранить").click()
    expect(page.locator(".toast").last).to_contain_text("secret")
    assert (
        "tracker" not in service.catalog.project("lin")
        or not service.catalog.project("lin")["tracker"]
    )
    settings.fill('team: ENG\ntoken_env: LINEAR_API_KEY\nstates: {"needs_person": "In Review"}')
    page.get_by_role("button", name="Сохранить").click()
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert service.catalog.project("lin")["tracker"] == {
        "kind": "linear",
        "team": "ENG",
        "token_env": "LINEAR_API_KEY",
        "states": {"needs_person": "In Review"},
    }
    page.locator("#project-edit").click()
    expect(page.locator("select[name=tracker_kind]")).to_have_value("linear")
    value = page.locator("textarea[name=tracker_settings]").input_value()
    assert "team: ENG" in value and '{"needs_person":"In Review"}' in value, value
    assert not errors
