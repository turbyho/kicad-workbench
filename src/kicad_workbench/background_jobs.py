"""The background jobs of the tool calls that take longer than JOB_WAIT.

An MCP client stops a tool call at its timeout and loses the result: Claude Desktop and
the default of the MCP SDK after 60 s (they ignore MCP_TOOL_TIMEOUT), Claude Code moves a
long call to the background after 2 min. The tools of the server run kicad-cli and the
Python of KiCad with limits of 60 to 600 s. Thus each tool call runs as an asyncio task:

- The middleware waits for the task up to JOB_WAIT. A call that ends in time gives its
  result as before.
- Else the call becomes a job: the answer gives its ID, and the tool job_status gives
  its state or its result. The task continues, also when the client cancels the request
  (asyncio.wait does not cancel the task).
- A call of the same tool with the same arguments (after the defaults of the tool, see
  call_key) while a job of it runs waits for that job (up to JOB_WAIT again) and gives
  its result or its ID: no second run.

The model is the model of the MCP Tasks extension (SEP-2663: a task ID, get, result,
list, cancel), thus a later move to it is one to one. A call of a client that uses the
task protocol of FastMCP gets its task from FastMCP at once (call_next does not wait).

The jobs are in the memory of the server process: a restart of the server loses them. A
finished job stays KEEP_SECONDS, and only the KEEP_JOBS newest finished jobs stay.

Limits:

- JOB_WAIT must be shorter than the timeout of the client. A client that stops to wait
  before JOB_WAIT and sends no cancel (the FastMCP client 3.4.11 at its timeout,
  measured) does not get the result of a call that ends before JOB_WAIT. A call that
  runs longer becomes a job as usual.
- A cancel stops the task at its next await. Work in a thread (kicad-cli, the Python of
  KiCad, a file write) does not stop and can still change files. Only the schematic
  edit tools (schematic_editor._run_edit) check the cancel before their save.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

# The time that a tool call waits for its work before it becomes a job (seconds): well
# below the 60 s of Claude Desktop and of the MCP SDK.
JOB_WAIT = 20.0
KEEP_SECONDS = 3600.0
KEEP_JOBS = 50
# The tool that gives the jobs (tools/jobs.py): its calls are not jobs.
JOB_STATUS_TOOL = "job_status"

CANCEL_LIMIT = (
    "Work in a thread does not stop: kicad-cli, the Python of KiCad or a file write can "
    "still end and change files. Only the schematic edit tools (add_component_from_library, "
    "add_wire and the label tools) do not save when the cancel comes before their save. "
    "Check the files after a cancel."
)


@dataclass
class Job:
    """A tool call that runs, or a job (a call that took longer than JOB_WAIT)."""

    id: str
    tool: str
    arguments: dict[str, Any]
    key: str
    started_at: float  # time.time(), only for the shown start time
    started: float  # time.monotonic()
    task: asyncio.Task
    finished: float | None = None  # time.monotonic()
    cancelled_by_status: bool = False  # job_status cancelled it
    reported: bool = False  # the client got the ID (or cancelled the request)

    def state(self) -> str:
        """running, done, failed or cancelled."""
        if not self.task.done():
            return "running"
        if self.task.cancelled():
            return "cancelled"
        return "failed" if self.task.exception() is not None else "done"

    def seconds(self) -> float:
        end = self.finished if self.finished is not None else time.monotonic()
        return end - self.started


_jobs: dict[str, Job] = {}


def _normal(value: Any) -> Any:
    """A value of an argument in one form: a float with no fraction is an int (JSON 30
    and 30.0 are the same number for a tool parameter)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normal(item) for item in value]
    return value


def call_key(tool: str, arguments: dict[str, Any], parameters: dict[str, Any] | None) -> str:
    """The identity of a call: the tool and its arguments, with the default of each
    parameter that the call does not give (the input schema of the tool) and the numbers
    in one form. A path in an other form is an other call."""
    values = dict(arguments)
    for name, schema in ((parameters or {}).get("properties") or {}).items():
        if name not in values and isinstance(schema, dict) and "default" in schema:
            values[name] = schema["default"]
    return json.dumps([tool, _normal(values)], sort_keys=True, default=str, ensure_ascii=False)


def arguments_text(arguments: dict[str, Any]) -> str:
    """The arguments of a call for one line of an answer."""
    return json.dumps(arguments, ensure_ascii=False, default=str)


def _prune() -> None:
    """Remove the finished jobs that are older than KEEP_SECONDS, and the oldest finished
    jobs after KEEP_JOBS."""
    now = time.monotonic()
    for job in [j for j in _jobs.values() if j.task.done()]:
        if job.finished is not None and now - job.finished > KEEP_SECONDS:
            del _jobs[job.id]
    finished = [j for j in _jobs.values() if j.task.done()]
    for job in finished[: max(0, len(finished) - KEEP_JOBS)]:
        del _jobs[job.id]


def _new_id() -> str:
    while (job_id := secrets.token_hex(4)) in _jobs:
        pass
    return job_id


def jobs() -> list[Job]:
    """The jobs of the server process, the oldest first: each running call (also one in
    its first JOB_WAIT: its client can have stopped to wait), and the finished jobs."""
    _prune()
    return list(_jobs.values())


def find(job_id: str) -> Job | None:
    _prune()
    return _jobs.get(job_id)


def wraps_result(output_schema: dict[str, Any] | None) -> bool:
    """True when the output schema of a tool is the schema of FastMCP for a text result
    ({"result": <text>}, x-fastmcp-wrap-result). The tools of the server have this
    schema or none (test_background_jobs checks it)."""
    return bool(output_schema and output_schema.get("x-fastmcp-wrap-result"))


def text_result(text: str, wrap: bool) -> ToolResult:
    """A text result. wrap: the tool has the output schema of a text result: the result
    must have the structured content {"result": text} (MCP: a tool with an outputSchema
    gives a structured result that agrees with it)."""
    return ToolResult(
        content=[mt.TextContent(type="text", text=text)],
        structured_content={"result": text} if wrap else None,
    )


def cancelled_text(job: Job) -> str:
    who = "job_status cancelled it" if job.cancelled_by_status else "the server cancelled it"
    return f"⚠️ Job `{job.id}` ({job.tool}) was cancelled: {who}. {CANCEL_LIMIT}"


def _job_answer(job: Job) -> str:
    started = time.strftime("%H:%M:%S", time.localtime(job.started_at))
    return (
        f"⏳ **The tool runs in the background** (longer than {JOB_WAIT:.0f} s).\n\n"
        f"- Job: `{job.id}`\n"
        f"- Tool: {job.tool}\n"
        f"- Arguments: {arguments_text(job.arguments)}\n"
        f"- Started: {started} ({job.seconds():.0f} s ago)\n\n"
        f'Call job_status with job_id="{job.id}" for the state or the result. A call of the '
        "same tool with the same arguments while the job runs does not start a second run: "
        "it waits for this job again and gives its result or this answer. The job continues "
        "when the client stops to wait. A restart of the server (of the MCP client) loses "
        "the job."
    )


def _finished(task: asyncio.Task, job: Job) -> None:
    job.finished = time.monotonic()
    if not task.cancelled():
        # Mark the exception as retrieved: job_status gives it as a new ToolError.
        task.exception()
    if not job.reported:
        _jobs.pop(job.id, None)


class BackgroundJobs(Middleware):
    """Runs each tool call as a task, and makes a job of a call that takes longer than
    JOB_WAIT. The result of a job has the notes of the middleware inside this one
    (KicadBuildNotes). VersionNotes is outside: it adds its notes to the answer that the
    client gets, also to the "⏳" answer."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool = context.message.name
        if tool == JOB_STATUS_TOOL:
            return await call_next(context)
        arguments = dict(context.message.arguments or {})
        tool_item = await context.fastmcp_context.fastmcp.get_tool(tool)
        wrap = wraps_result(tool_item.output_schema if tool_item else None)
        key = call_key(tool, arguments, tool_item.parameters if tool_item else None)
        job = next((j for j in _jobs.values() if j.key == key and not j.task.done()), None)
        if job is None:
            task = asyncio.create_task(call_next(context), name=f"tool {tool}")
            job = Job(_new_id(), tool, arguments, key, time.time(), time.monotonic(), task)
            _jobs[job.id] = job
            task.add_done_callback(lambda done, job=job: _finished(done, job))
        try:
            # asyncio.wait does not cancel the task: at a timeout, and when the client
            # cancels the request.
            done, _pending = await asyncio.wait({job.task}, timeout=JOB_WAIT)
        except asyncio.CancelledError:
            job.reported = True
            _prune()
            raise
        if not done:
            job.reported = True
            _prune()
            return text_result(_job_answer(job), wrap)
        if job.task.cancelled():
            # job_status cancelled the task while this request waited: an answer, not
            # the CancelledError (the MCP SDK takes it for a cancel of the session).
            job.reported = True
            return text_result(cancelled_text(job), wrap)
        # The result, or the exception of the tool, as with no job.
        return job.task.result()
