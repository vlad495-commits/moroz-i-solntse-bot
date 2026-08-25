import json

import pytest

import database
import eval_database as evdb


class Connection:
    def __init__(self, fetchrow=None, rows=None):
        self.calls = []
        self._fetchrow = {"id": 8} if fetchrow is None else fetchrow
        self._rows = [] if rows is None else rows

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self._rows

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self._fetchrow

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "OK"


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return Acquire(self.connection)


@pytest.mark.asyncio
async def test_all_eval_queries_are_filtered_by_requested_suite(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(database, "_pool", Pool(connection))

    await evdb.list_cases(suite="router")
    await evdb.list_problem_cases(suite="router")
    await evdb.list_runs(limit=5, suite="router")

    assert all("suite" in query for query, _args in connection.calls)
    assert any(args == ("router",) for _query, args in connection.calls)
    assert any(args == (5, "router") for _query, args in connection.calls)
    problem_query = connection.calls[1][0]
    assert "eval_runs" in problem_query
    assert "c.suite = $1" in problem_query


@pytest.mark.asyncio
async def test_answer_crud_cannot_read_update_or_delete_router_cases(monkeypatch):
    connection = Connection(fetchrow=None)
    monkeypatch.setattr(database, "_pool", Pool(connection))

    await evdb.get_case(7)
    await evdb.update_case(7, "simple", "q", [], [], "a")
    await evdb.delete_case(7)

    assert all("suite = 'answer'" in query for query, _args in connection.calls)


@pytest.mark.asyncio
async def test_create_run_and_result_store_suite_and_actual_data(monkeypatch):
    connection = Connection(fetchrow={"id": 8})
    monkeypatch.setattr(database, "_pool", Pool(connection))

    await evdb.create_run(20, "router-model", suite="router")
    await evdb.save_result(
        8,
        7,
        "input",
        "",
        "",
        "pass",
        "router",
        None,
        "matched",
        5,
        actual_data={"intents": ["faq"], "source": "llm"},
    )

    assert "suite" in connection.calls[0][0]
    assert connection.calls[0][1] == (20, "router-model", "router")
    assert "actual_data" in connection.calls[1][0]
    assert json.loads(connection.calls[1][1][-1]) == {
        "intents": ["faq"],
        "source": "llm",
    }


@pytest.mark.asyncio
async def test_answer_defaults_remain_backward_compatible(monkeypatch):
    connection = Connection(fetchrow={"id": 9})
    monkeypatch.setattr(database, "_pool", Pool(connection))

    await evdb.list_cases()
    await evdb.list_problem_cases()
    await evdb.list_runs(limit=3)
    await evdb.create_run(2, "judge")

    assert connection.calls[0][1] == ("answer",)
    assert connection.calls[1][1] == ("answer",)
    assert connection.calls[2][1] == (3, "answer")
    assert connection.calls[3][1] == (2, "judge", "answer")


@pytest.mark.asyncio
async def test_result_reads_project_suite_safe_expected_and_actual_data(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(database, "_pool", Pool(connection))

    await evdb.get_run_results(8)
    await evdb.get_run_results_since(8, 3)

    assert "input_data" in connection.calls[0][0]
    for query, _args in connection.calls:
        assert "expected_data" in query
        assert "actual_data" in query
        assert "eval_cases" in query
        assert "eval_runs" in query
        assert ".suite" in query


@pytest.mark.asyncio
async def test_structured_jsonb_is_decoded_at_eval_read_boundary(monkeypatch):
    connection = Connection(
        rows=[
            {
                "id": 7,
                "input_data": '{"input":"masked","context":[]}',
                "expected_data": '{"intents":["faq"]}',
                "actual_data": '{"intents":["faq"],"source":"llm"}',
            }
        ]
    )
    monkeypatch.setattr(database, "_pool", Pool(connection))

    cases = await evdb.list_cases("router")
    results = await evdb.get_run_results(8)

    assert cases[0]["input_data"] == {"input": "masked", "context": []}
    assert cases[0]["expected_data"] == {"intents": ["faq"]}
    assert results[0]["expected_data"] == {"intents": ["faq"]}
    assert results[0]["actual_data"] == {"intents": ["faq"], "source": "llm"}
    assert results[0]["input_data"] == {"input": "masked", "context": []}
