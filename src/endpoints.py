import time
import random
import warnings
from asyncio import wait_for
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from playwright_captcha import CaptchaType
import trafilatura

from src.consts import CHALLENGE_TITLES
from src.models import (
    HealthcheckResponse,
    LinkRequest,
    LinkResponse,
    Readability,
    Solution,
)
from src.utils import CamoufoxDepClass, TimeoutTimer, get_camoufox, logger

warnings.filterwarnings("ignore", category=SyntaxWarning)


router = APIRouter()

CamoufoxDep = Annotated[CamoufoxDepClass, Depends(get_camoufox)]


@router.get("/", include_in_schema=False)
def read_root():
    """Redirect to /docs."""
    logger.debug("Redirecting to /docs")
    return RedirectResponse(url="/docs", status_code=301)


@router.get("/health")
async def health_check(sb: CamoufoxDep):
    """Health check endpoint."""
    health_check_request = await read_item(
        LinkRequest.model_construct(url="https://google.com"),
        sb,
    )

    if health_check_request.solution.status != HTTPStatus.OK:
        raise HTTPException(
            status_code=500,
            detail="Health check failed",
        )

    return HealthcheckResponse(user_agent=health_check_request.solution.user_agent)


@router.post("/v1")
async def read_item(request: LinkRequest, dep: CamoufoxDep) -> LinkResponse:
    """Handle POST requests."""
    start_time = int(time.time() * 1000)

    timer = TimeoutTimer(duration=request.max_timeout)

    request.url = request.url.replace('"', "").strip()
    try:
        await dep.page.mouse.move(random.randint(50, 200), random.randint(50, 200))
        await dep.page.wait_for_timeout(random.randint(100, 300))

        page_request = await dep.page.goto(
            request.url, timeout=timer.remaining() * 1000
        )
        status = page_request.status if page_request else HTTPStatus.OK
        await dep.page.wait_for_load_state(
            state="domcontentloaded", timeout=timer.remaining() * 1000
        )
        await dep.page.wait_for_load_state(
            "networkidle", timeout=timer.remaining() * 1000
        )

        if await dep.page.title() in CHALLENGE_TITLES:
            logger.info("Challenge detected, attempting to solve...")
            # Solve the captcha
            await wait_for(
                dep.solver.solve_captcha(  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
                    captcha_container=dep.page,
                    captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
                    wait_checkbox_attempts=1,
                    wait_checkbox_delay=0.5,
                ),
                timeout=timer.remaining(),
            )
            status = HTTPStatus.OK
            logger.debug("Challenge solved successfully.")
    except TimeoutError as e:
        logger.error("Timed out while solving the challenge")
        raise HTTPException(
            status_code=408,
            detail="Timed out while solving the challenge",
        ) from e

    cookies = await dep.context.cookies()
    page_content = await dep.page.content()

    # Extract readability content if enabled in request
    readability_obj = None
    if request.enable_readability:
        logger.info("Extracting readability content for %s", request.url)
        extracted_content = trafilatura.extract(page_content)
        if extracted_content:
            readability_obj = Readability(content=extracted_content)
            logger.debug("Readability extraction successful (%d chars)", len(extracted_content))
        else:
            logger.warning("Readability extraction returned no content")

    return LinkResponse(
        message="Success",
        solution=Solution(
            user_agent=await dep.page.evaluate("navigator.userAgent"),
            url=dep.page.url,
            status=status,
            cookies=cookies,
            headers=page_request.headers if page_request else {},
            response=page_content,
            readability=readability_obj,
        ),
        start_timestamp=start_time,
    )
