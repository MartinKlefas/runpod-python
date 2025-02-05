"""
Job related helpers.
"""
# pylint: disable=too-many-branches

import inspect
from typing import Any, Callable, Dict, Generator, Optional, Union

import os
import json
import traceback
from aiohttp import ClientSession

from runpod.serverless.modules.rp_logger import RunPodLogger
from .worker_state import WORKER_ID, Jobs
from .rp_tips import check_return_size
from ...version import __version__ as runpod_version

JOB_GET_URL = str(os.environ.get('RUNPOD_WEBHOOK_GET_JOB')).replace('$ID', WORKER_ID)

log = RunPodLogger()
job_list = Jobs()


def _job_get_url():
    """
    Prepare the URL for making a 'get' request to the serverless API (sls).

    This function constructs the appropriate URL for sending a 'get' request to the serverless API,
    ensuring that the request will be correctly routed and processed by the API.

    Returns:
        str: The prepared URL for the 'get' request to the serverless API.
    """
    job_in_progress = '1' if job_list.get_job_list() else '0'
    return JOB_GET_URL + f"&job_in_progress={job_in_progress}"


async def get_job(session: ClientSession, retry=True) -> Optional[Dict[str, Any]]:
    """
    Get the job from the queue.
    Will continue trying to get a job until one is available.

    Inputs:
    - session | The aiohttp session

    Note: Retry True just for ease of, if testing improved this can be removed.
    """
    next_job = None

    while next_job is None:
        try:
            async with session.get(_job_get_url()) as response:
                if response.status == 204:
                    log.debug("No content, no job to process.")
                    if not retry:
                        return None
                    continue

                if response.status == 400:
                    log.debug("Received 400 status, expected when FlashBoot is enabled.")
                    if not retry:
                        return None
                    continue

                if response.status != 200:
                    log.error(f"Failed to get job, status code: {response.status}")
                    if not retry:
                        return None
                    continue

                next_job = await response.json()
                log.debug(f"Received Job | {next_job}")

            # Check if the job is valid
            job_id = next_job.get("id", None)
            job_input = next_job.get("input", None)

            if None in [job_id, job_input]:
                missing_fields = []
                if job_id is None:
                    missing_fields.append("id")
                if job_input is None:
                    missing_fields.append("input")

                log.error(f"Job has missing field(s): {', '.join(missing_fields)}.")
                next_job = None

        except Exception as err:  # pylint: disable=broad-except
            log.error(f"Error while getting job: {err}")

        if next_job is None:
            log.debug("No job available, waiting for the next one.")
            if not retry:
                return None

    log.debug(f"{next_job['id']} | Valid Job Confirmed")

    if next_job:
        job_list.add_job(next_job["id"])
        log.debug(f"{next_job['id']} | Added Job ID")

    return next_job


async def run_job(handler: Callable, job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run the job using the handler.
    Returns the job output or error.
    """
    log.info(f'{job["id"]} | Started')
    run_result = {"error": "No output from handler."}

    try:
        handler_return = handler(job)
        job_output = await handler_return if inspect.isawaitable(handler_return) else handler_return

        log.debug(f'{job["id"]} | Handler output: {job_output}')

        if isinstance(job_output, dict):
            error_msg = job_output.pop("error", None)
            refresh_worker = job_output.pop("refresh_worker", None)

            run_result = {"output": job_output}

            if error_msg:
                run_result["error"] = error_msg
            if refresh_worker:
                run_result["stopPod"] = True

        elif isinstance(job_output, bool):
            run_result = {"output": job_output}

        else:
            run_result = {"output": job_output}

        if run_result.get("output") == {}:
            run_result.pop("output")

        check_return_size(run_result)  # Checks the size of the return body.

    except Exception as err:    # pylint: disable=broad-except
        error_info = {
            "error_type": str(type(err)),
            "error_message": str(err),
            "error_traceback": traceback.format_exc(),
            "hostname": os.environ.get("RUNPOD_POD_HOSTNAME", "unknown"),
            "worker_id": os.environ.get("RUNPOD_POD_ID", "unknown"),
            "runpod_version": runpod_version
        }

        log.error(f'{job["id"]} | Captured Handler Exception')
        log.error(json.dumps(error_info, indent=4))
        run_result = {"error": json.dumps(error_info)}

    finally:
        log.debug(f'{job["id"]} | run_job return: {run_result}')

    return run_result


async def run_job_generator(
        handler: Callable,
        job: Dict[str, Any]) -> Generator[Dict[str, Union[str, Any]], None, None]:
    '''
    Run generator job.
    Yields output partials from the generator.
    '''
    try:
        job_output = handler(job)
        if inspect.isasyncgenfunction(handler):
            async for output_partial in job_output:
                yield {"output": output_partial}
        else:
            for output_partial in job_output:
                yield {"output": output_partial}
    except Exception as err:    # pylint: disable=broad-except
        log.error(f'Error while running job {job["id"]}: {err}')
        yield {"error": f"handler: {str(err)} \ntraceback: {traceback.format_exc()}"}
    finally:
        log.info(f'{job["id"]} | Finished ')
