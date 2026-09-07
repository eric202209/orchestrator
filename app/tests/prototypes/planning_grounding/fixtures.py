"""Natural-language grounding fixtures (PHASE35-PGP1).

Expected regions live here and in the tests only. They are never passed to the
adapter, and the adapter never imports this module.

CASE_A / CASE_B reuse the exact operator task text of the Phase35-A1 and
Phase35-A2 product dogfood runs, and their expected relevant regions are the
ones recorded by Phase35-G1. CASE_C is the retired-project task required by
Phase35-PGP1 Part 8, with no path, symbol, route, or snippet. CASE_D is a
negative control: ordinary product language for behavior this repository does
not implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GroundingFixture:
    name: str
    task_text: str
    expected_path: str | None
    expected_regions: tuple[tuple[int, int], ...] = ()
    expects_sufficient: bool = True
    notes: str = ""
    known_expected_symbols: tuple[str, ...] = field(default=())


CASE_A = GroundingFixture(
    name="CASE_A_a1_shape",
    task_text=(
        "The task list API silently accepts nonsense paging and sorting "
        "parameters. Asking for a negative limit returns every task in the "
        "database in one unbounded response, a zero limit returns an empty list "
        "as if there were no tasks at all, and a negative skip is accepted too. "
        "Sorting is just as loose: a misspelled sort field or an unrecognised "
        "sort direction still returns 200 with the rows in an order nobody asked "
        "for, so a client has no way to tell it got the wrong answer. The "
        "page-based parameters on the same endpoints already reject bad values "
        "with 422. Make the legacy skip/limit parameters and the sort parameters "
        "behave the same way: reject out-of-range or unrecognised values with a "
        "clear 422 instead of silently returning wrong or unbounded data. Valid "
        "requests must keep returning exactly what they return today, including "
        "the current defaults and every sort field that works now. Only the task "
        "list endpoints are in scope; leave the session endpoints alone. Add "
        "regression coverage for the rejected values and for the preserved "
        "behaviour, and keep it runnable with the repository's existing test "
        "command."
    ),
    expected_path="app/api/v1/endpoints/tasks.py",
    expected_regions=((27853, 31813), (32691, 35166)),
    known_expected_symbols=("get_all_tasks", "get_project_tasks"),
    notes="Phase35-A1 operator text. Current architecture: head_fallback_no_target, 0 overlap.",
)

CASE_B = GroundingFixture(
    name="CASE_B_a2_shape",
    task_text=(
        "When work is cancelled, the mobile status views keep counting it as "
        "still pending. On the mobile dashboard, on a project's status view and "
        "in a session summary, a cancelled task is folded into the pending "
        "number, so the amount of outstanding work never drains and operators "
        "keep chasing items that were deliberately called off. Cancelled work "
        "should not be counted as pending anywhere those task counts are "
        "reported, and it should be visible as its own count alongside the "
        "existing ones so it is clear where those tasks went. Everything else "
        "about those responses must stay exactly as it is today: the same "
        "totals, the same running, done and failed numbers, the same completion "
        "rate definition, the same surrounding fields, and the same "
        "authentication requirement. Add regression coverage for the cancelled "
        "handling and for the counts that must not change, and keep it runnable "
        "with the repository's existing test command."
    ),
    expected_path="app/api/v1/endpoints/mobile.py",
    expected_regions=((4677, 5262), (22463, 22713), (37183, 38596)),
    known_expected_symbols=(
        "_build_task_counts",
        "get_session_summary",
        "get_dashboard",
    ),
    notes="Phase35-A2 operator text. Current architecture selected a docstring at 52818-54773.",
)

CASE_C = GroundingFixture(
    name="CASE_C_retired_project_shape",
    task_text=(
        "Allow callers to include retired projects when browsing projects while "
        "preserving the current default behavior."
    ),
    expected_path="app/api/v1/endpoints/projects.py",
    expected_regions=((3894, 5598),),
    known_expected_symbols=("get_projects",),
    notes="Phase35-G1R required conceptual case. Current architecture: APIRouter() at 420-2360.",
)

CASE_D = GroundingFixture(
    name="CASE_D_exhaustion_negative_control",
    task_text=(
        "Operators cannot export the monthly invoice reconciliation ledger "
        "before the billing cycle closes, so finance reconstructs it by hand "
        "every month. Provide an export of that ledger while keeping the "
        "existing invoice totals and currency rounding exactly as they are "
        "today."
    ),
    expected_path=None,
    expected_regions=(),
    expects_sufficient=False,
    notes="No such implementation exists in this repository. Must stop insufficient_grounding.",
)

ALL_FIXTURES = (CASE_A, CASE_B, CASE_C, CASE_D)
