"""The tool job_status: the background jobs of the tool calls (background_jobs.py)."""

import time

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult

from .. import background_jobs
from ..background_jobs import (
    CANCEL_LIMIT,
    JOB_STATUS_TOOL,
    Job,
    arguments_text,
    cancelled_text,
    find,
    jobs,
    text_result,
)
from ..server import mcp
from ..version_check import add_notes


def _text(text: str) -> ToolResult:
    """An answer of job_status (no output schema: job_status gives ToolResult)."""
    return text_result(text, wrap=False)


def _line(job: Job) -> str:
    started = time.strftime("%H:%M:%S", time.localtime(job.started_at))
    return (
        f"| `{job.id}` | {job.state()} | {job.tool} | {arguments_text(job.arguments)} "
        f"| {started} | {job.seconds():.0f} s |"
    )


@mcp.tool(name=JOB_STATUS_TOOL)
async def job_status(job_id: str = "", cancel: bool = False) -> ToolResult:
    """Give the background jobs of the tool calls, the state or the result of one job, or
    cancel a job.

    A tool call that takes longer than 20 s becomes a background job: its answer starts
    with "⏳" and gives the job ID, and the work continues (also when the client stops to
    wait for the call). This tool gives the result of the job: the same text that the
    tool gives, or the same error. A call of the same tool with the same arguments while
    its job runs starts no second run: it waits for that job again (up to 20 s) and
    gives its result or its ID. The list also has the calls that run in their first
    20 s. The jobs are in the memory of the server process: a restart of the server (of
    the MCP client) loses them. A finished job stays 1 h (the 50 newest finished jobs).

    A cancel stops the task of the job at its next step. Work in a thread does not stop:
    kicad-cli, the Python of KiCad or a file write can still end and change files. Only
    the schematic edit tools (add_component_from_library, add_wire and the label tools)
    do not save when the cancel comes before their save. Check the files after a cancel.
    Cancel a job only when the user asks.

    Args:
        job_id: The ID of a job. Empty: the list of the jobs of the server process.
        cancel: True: cancel the running job job_id.

    Returns:
        The list of the jobs; the state of a running job; the result of a finished job;
        or the answer of the cancel.
    """
    if not job_id:
        if cancel:
            return _text("❌ Give the job_id of the job to cancel.")
        listed = jobs()
        if not listed:
            return _text(
                "No background job in this server process. A tool call becomes a job when "
                f"it takes longer than {background_jobs.JOB_WAIT:.0f} s."
            )
        return _text(
            f"# Background jobs ({len(listed)})\n\n"
            "| Job | State | Tool | Arguments | Started | Duration |\n"
            "|-----|-------|------|-----------|---------|----------|\n"
            + "\n".join(_line(job) for job in listed)
        )
    job = find(job_id)
    if job is None:
        return _text(
            f"❌ No job {job_id!r} in this server process: the ID is wrong, the job was "
            "removed (1 h after its end), or the server started again. Call job_status "
            "with no job_id for the list."
        )
    if cancel:
        if job.task.done():
            return _text(f"❌ Job `{job.id}` ({job.tool}) is not running: it is {job.state()}.")
        job.cancelled_by_status = True
        job.reported = True
        job.task.cancel()
        return _text(
            f"⚠️ Job `{job.id}` ({job.tool}) is cancelled. {CANCEL_LIMIT} Call job_status "
            "again for the final state."
        )
    state = job.state()
    if state == "running":
        return _text(
            f"⏳ Job `{job.id}` ({job.tool}) runs since {job.seconds():.0f} s. Call "
            "job_status again later."
        )
    if state == "cancelled":
        return _text(cancelled_text(job))
    error = job.task.exception()
    if error is not None:
        # The same error as the call with no job (FastMCP gives the client an answer
        # with isError). A new exception: the stored one keeps its traceback.
        raise ToolError(str(error))
    note = f"_Result of job `{job.id}` ({job.tool}, {job.seconds():.0f} s)._"
    return add_notes(job.task.result(), [note])
