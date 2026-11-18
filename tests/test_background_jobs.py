"""Tests of the background jobs (background_jobs.py, tools/jobs.py) through a FastMCP
client in memory, with a short JOB_WAIT and slow fake tools. The tests wait for the task
of a job, not for a time."""

import asyncio
import re
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from kicad_workbench import background_jobs
from kicad_workbench.background_jobs import BackgroundJobs
from kicad_workbench.tools import jobs

WAIT = 0.2
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server(monkeypatch):
    """A server with the middleware, job_status and fake tools; the runs of each tool."""
    monkeypatch.setattr(background_jobs, "JOB_WAIT", WAIT)
    monkeypatch.setattr(background_jobs, "_jobs", {})
    runs: dict[str, int] = {}
    test = FastMCP("test")
    test.add_middleware(BackgroundJobs())
    test.tool(name="job_status")(jobs.job_status)
    gate = asyncio.Event()

    @test.tool()
    async def quick(text: str) -> str:
        runs["quick"] = runs.get("quick", 0) + 1
        return f"quick {text}"

    @test.tool()
    async def slow(seconds: float, label: str = "x") -> str:
        runs["slow"] = runs.get("slow", 0) + 1
        await asyncio.sleep(seconds)
        return f"slept {seconds} {label}"

    @test.tool()
    async def gated() -> str:
        """Ends when the test opens the gate."""
        runs["gated"] = runs.get("gated", 0) + 1
        await gate.wait()
        return "opened"

    @test.tool()
    async def broken() -> str:
        await gate.wait()
        raise RuntimeError("the work failed")

    return test, runs, gate


def _text(result) -> str:
    return "\n".join(item.text for item in result.content)


def _job_id(text: str) -> str:
    match = re.search(r"- Job: `([0-9a-f]+)`", text)
    assert match, text
    return match.group(1)


async def _status(client, job_id: str = "", cancel: bool = False) -> str:
    return _text(await client.call_tool("job_status", {"job_id": job_id, "cancel": cancel}))


async def _end(job_id: str) -> None:
    """Wait for the end of the task of a job."""
    await asyncio.wait({background_jobs._jobs[job_id].task})


async def test_a_quick_call_gives_its_result(server):
    test, runs, gate = server
    async with Client(test) as client:
        assert _text(await client.call_tool("quick", {"text": "a"})) == "quick a"
        assert (await _status(client)).startswith("No background job")


async def test_a_slow_call_becomes_a_job(server):
    test, runs, gate = server
    async with Client(test) as client:
        answer = _text(await client.call_tool("gated", {}))
        assert answer.startswith("⏳ **The tool runs in the background**"), answer
        assert "- Arguments: {}" in answer
        job_id = _job_id(answer)
        assert (await _status(client, job_id)).startswith(f"⏳ Job `{job_id}` (gated) runs")
        assert f"| `{job_id}` | running | gated |" in await _status(client)
        gate.set()
        await _end(job_id)
        result = await _status(client, job_id)
        assert result.startswith("opened"), result
        assert f"_Result of job `{job_id}` (gated, " in result
        assert runs["gated"] == 1


async def test_the_answer_of_a_job_agrees_with_the_output_schema(server):
    # A tool with a text result has the output schema {"result": <text>}: the client
    # refuses a result with no structured content.
    test, runs, gate = server
    async with Client(test) as client:
        result = await client.call_tool("gated", {})
        assert result.structured_content == {"result": _text(result)}


async def test_the_same_call_waits_for_the_same_job(server):
    test, runs, gate = server
    async with Client(test) as client:
        first = _job_id(_text(await client.call_tool("gated", {})))
        second = _job_id(_text(await client.call_tool("gated", {})))
        assert first == second and runs["gated"] == 1
        gate.set()
        await _end(first)
        # A finished job is not used again: a new call is a new run.
        assert _text(await client.call_tool("gated", {})) == "opened"
        assert runs["gated"] == 2


async def test_the_defaults_and_the_numbers_of_the_key(server):
    # {"seconds": 1} and {"seconds": 1.0, "label": "x"} are the same call of slow.
    test, runs, gate = server
    async with Client(test) as client:
        first = _job_id(_text(await client.call_tool("slow", {"seconds": 1})))
        second = _job_id(_text(await client.call_tool("slow", {"seconds": 1.0, "label": "x"})))
        other = _job_id(_text(await client.call_tool("slow", {"seconds": 1, "label": "y"})))
        assert first == second != other
        assert runs["slow"] == 2


async def test_the_cancel_of_a_job(server):
    test, runs, gate = server
    async with Client(test) as client:
        job_id = _job_id(_text(await client.call_tool("gated", {})))
        cancelled = await _status(client, job_id, cancel=True)
        assert cancelled.startswith(f"⚠️ Job `{job_id}` (gated) is cancelled"), cancelled
        assert "Check the files after a cancel" in cancelled
        await _end(job_id)
        assert (await _status(client, job_id)).startswith(
            f"⚠️ Job `{job_id}` (gated) was cancelled: job_status cancelled it"
        )
        assert (await _status(client, job_id, cancel=True)).startswith("❌ Job")
        assert (await _status(client, cancel=True)).startswith("❌ Give the job_id")


async def test_a_cancel_while_a_request_waits_keeps_the_session(server, monkeypatch):
    # The request waits in its first JOB_WAIT; job_status cancels its task. The request
    # gets an answer (not the CancelledError, which closes the session).
    monkeypatch.setattr(background_jobs, "JOB_WAIT", 30.0)
    test, runs, gate = server
    async with Client(test) as client:
        waiting = asyncio.create_task(client.call_tool("gated", {}))
        while not background_jobs._jobs:
            await asyncio.sleep(0)
        job_id = next(iter(background_jobs._jobs))
        await _status(client, job_id, cancel=True)
        answer = _text(await waiting)
        assert answer.startswith(f"⚠️ Job `{job_id}` (gated) was cancelled"), answer
        assert _text(await client.call_tool("quick", {"text": "b"})) == "quick b"


async def test_a_job_that_fails(server):
    test, runs, gate = server
    async with Client(test) as client:
        job_id = _job_id(_text(await client.call_tool("broken", {})))
        gate.set()
        await _end(job_id)
        # The same error as the call with no job, each time.
        for _ in range(2):
            with pytest.raises(ToolError, match="Error calling tool 'broken': the work failed"):
                await _status(client, job_id)


async def test_an_unknown_job(server):
    test, runs, gate = server
    async with Client(test) as client:
        assert (await _status(client, "0000")).startswith("❌ No job '0000'")


async def test_the_job_continues_when_the_client_stops_to_wait(server):
    # The client stops to wait at its timeout (shorter than JOB_WAIT here). The call is
    # in the list, and it becomes a job after JOB_WAIT.
    test, runs, gate = server
    async with Client(test) as client:
        with pytest.raises(McpError):
            await client.call_tool("gated", {}, timeout=0.05)
        listed = await _status(client)
        assert "| running | gated |" in listed, listed
        job_id = re.search(r"\| `([0-9a-f]+)` \|", listed).group(1)
        while not background_jobs._jobs[job_id].reported:
            await asyncio.sleep(0.01)
        gate.set()
        await _end(job_id)
        assert (await _status(client, job_id)).startswith("opened")


def test_old_jobs_are_removed(monkeypatch):
    monkeypatch.setattr(background_jobs, "_jobs", {})
    monkeypatch.setattr(background_jobs, "KEEP_JOBS", 2)

    async def run():
        for number in range(4):
            task = asyncio.create_task(asyncio.sleep(0))
            await task
            job = background_jobs.Job(f"j{number}", "t", {}, f"k{number}", 0.0, 0.0, task)
            job.finished, job.reported = background_jobs.time.monotonic(), True
            background_jobs._jobs[job.id] = job
        return [job.id for job in background_jobs.jobs()]

    assert asyncio.run(run()) == ["j2", "j3"]


async def test_each_tool_of_the_server_has_a_known_output_schema():
    # The answer of a job agrees only with a text result or with no schema.
    from kicad_workbench.server import mcp

    for tool in await mcp.list_tools():
        schema = tool.output_schema
        assert schema is None or background_jobs.wraps_result(schema), tool.name


def test_the_texts_have_the_numbers_of_the_constants():
    # The docstring of job_status and the documentation give JOB_WAIT, KEEP_SECONDS and
    # KEEP_JOBS as numbers.
    wait = f"{background_jobs.JOB_WAIT:.0f} s"
    keep = f"{background_jobs.KEEP_SECONDS / 3600:.0f} h"
    newest = f"{background_jobs.KEEP_JOBS} newest"
    doc = " ".join(jobs.job_status.__doc__.split())
    tools_md = " ".join((ROOT / "docs/tools.md").read_text(encoding="utf-8").split())
    for text in (doc, tools_md):
        assert wait in text and keep in text and newest in text
