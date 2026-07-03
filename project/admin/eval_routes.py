"""Endpoints админки для эвалов: CRUD кейсов, запуск прогона, прогресс через SSE."""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

import eval_database as evdb
import eval_runner
from auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/eval")
_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_BASE_DIR / "templates")


def _split_keywords(text: str) -> list[str]:
    """Парсинг textarea с keywords: одна строка = одно слово/regex."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


@router.get("/", response_class=HTMLResponse)
async def eval_index(request: Request):
    user = get_current_user(request)
    cases = await evdb.list_cases()
    runs = await evdb.list_runs(limit=10)
    return templates.TemplateResponse(
        request,
        "eval_list.html",
        {"user": user, "cases": cases, "runs": runs},
    )


@router.get("/cases/new", response_class=HTMLResponse)
async def eval_case_new(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "eval_case_edit.html",
        {"user": user, "case": None},
    )


@router.get("/cases/{case_id}", response_class=HTMLResponse)
async def eval_case_edit(request: Request, case_id: int):
    user = get_current_user(request)
    case = await evdb.get_case(case_id)
    if not case:
        return RedirectResponse(url="/eval/", status_code=302)
    return templates.TemplateResponse(
        request,
        "eval_case_edit.html",
        {"user": user, "case": case},
    )


@router.post("/cases")
async def eval_case_create(
    request: Request,
    category: str = Form("general"),
    question: str = Form(...),
    expected_keywords: str = Form(""),
    forbidden_keywords: str = Form(""),
    expected_answer: str = Form(...),
):
    get_current_user(request)
    await evdb.create_case(
        category=category.strip() or "general",
        question=question.strip(),
        expected_keywords=_split_keywords(expected_keywords),
        forbidden_keywords=_split_keywords(forbidden_keywords),
        expected_answer=expected_answer.strip(),
    )
    return RedirectResponse(url="/eval/", status_code=302)


@router.post("/cases/{case_id}")
async def eval_case_update(
    request: Request,
    case_id: int,
    category: str = Form("general"),
    question: str = Form(...),
    expected_keywords: str = Form(""),
    forbidden_keywords: str = Form(""),
    expected_answer: str = Form(...),
):
    get_current_user(request)
    await evdb.update_case(
        case_id=case_id,
        category=category.strip() or "general",
        question=question.strip(),
        expected_keywords=_split_keywords(expected_keywords),
        forbidden_keywords=_split_keywords(forbidden_keywords),
        expected_answer=expected_answer.strip(),
    )
    return RedirectResponse(url="/eval/", status_code=302)


@router.post("/cases/{case_id}/delete")
async def eval_case_delete(request: Request, case_id: int):
    get_current_user(request)
    await evdb.delete_case(case_id)
    return RedirectResponse(url="/eval/", status_code=302)


@router.post("/runs")
async def eval_run_start(request: Request, background_tasks: BackgroundTasks):
    get_current_user(request)
    cases = await evdb.list_cases()
    if not cases:
        return RedirectResponse(url="/eval/?error=no_cases", status_code=302)

    run_id = await evdb.create_run(
        total=len(cases),
        judge_model=eval_runner.JUDGE_MODEL,
    )
    # Запускаем фоновую задачу — отдельный asyncio task
    asyncio.create_task(eval_runner.run_eval_set(run_id))
    return RedirectResponse(url=f"/eval/runs/{run_id}", status_code=302)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def eval_run_detail(request: Request, run_id: int):
    user = get_current_user(request)
    run = await evdb.get_run(run_id)
    if not run:
        return RedirectResponse(url="/eval/", status_code=302)
    results = await evdb.get_run_results(run_id)
    return templates.TemplateResponse(
        request,
        "eval_run_detail.html",
        {"user": user, "run": run, "results": results},
    )


@router.get("/runs/{run_id}/stream")
async def eval_run_stream(request: Request, run_id: int):
    """SSE-стрим для прогресс-бара. Шлёт обновления статуса прогона + новые результаты."""
    get_current_user(request)

    async def _gen():
        last_id = 0
        while True:
            if await request.is_disconnected():
                break
            run = await evdb.get_run(run_id)
            if not run:
                yield "event: error\ndata: run_not_found\n\n"
                break

            # Новые результаты
            new = await evdb.get_run_results_since(run_id, last_id)
            for r in new:
                last_id = max(last_id, r["id"])
                payload = {
                    "id": r["id"],
                    "case_id": r["case_id"],
                    "question": r["question"][:120],
                    "verdict": r["verdict"],
                    "check_layer": r["check_layer"],
                    "score": r["score"],
                }
                yield f"event: result\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

            # Статус
            progress = {
                "total": run["total"],
                "passed": run["passed"],
                "failed": run["failed"],
                "status": run["status"],
            }
            yield f"event: progress\ndata: {json.dumps(progress)}\n\n"

            if run["status"] in ("finished", "error"):
                yield "event: done\ndata: {}\n\n"
                break

            await asyncio.sleep(1.0)

    return StreamingResponse(_gen(), media_type="text/event-stream")
