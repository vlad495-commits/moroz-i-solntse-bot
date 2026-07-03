"""Client-friendly review board for eval cases."""

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import review_database as rvdb
from auth import get_current_user

router = APIRouter(prefix="/review")
_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_BASE_DIR / "templates")


@router.get("/evals", response_class=HTMLResponse)
async def review_evals(request: Request, status: str = "all"):
    user = get_current_user(request)
    status = status if status in {"all", "pending", "ok", "needs_edit", "delete"} else "all"
    cases = await rvdb.list_review_cases(status=status)
    suggestions = await rvdb.list_suggestions()
    counts = await rvdb.get_review_counts()
    return templates.TemplateResponse(
        request,
        "review_eval_list.html",
        {
            "user": user,
            "cases": cases,
            "suggestions": suggestions,
            "counts": counts,
            "statuses": rvdb.STATUSES,
            "active_status": status,
        },
    )


@router.post("/evals/cases/{case_id}")
async def review_case_save(
    request: Request,
    case_id: int,
    status: str = Form("pending"),
    comment: str = Form(""),
    proposed_answer: str = Form(""),
):
    user = get_current_user(request)
    await rvdb.save_case_review(
        case_id=case_id,
        status=status,
        comment=comment,
        proposed_answer=proposed_answer,
        reviewer=user,
    )
    return RedirectResponse(url="/review/evals?saved=1", status_code=302)


@router.post("/evals/suggestions")
async def review_suggestion_create(
    request: Request,
    category: str = Form("general"),
    question: str = Form(...),
    expected_answer: str = Form(...),
    comment: str = Form(""),
):
    user = get_current_user(request)
    await rvdb.create_suggestion(
        category=category,
        question=question,
        expected_answer=expected_answer,
        comment=comment,
        reviewer=user,
    )
    return RedirectResponse(url="/review/evals?saved=1", status_code=302)
