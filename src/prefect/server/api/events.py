import base64
from typing import List, Optional

from prefect._vendor.fastapi import Response, WebSocket, status
from prefect._vendor.fastapi.exceptions import HTTPException
from prefect._vendor.fastapi.param_functions import Depends, Path
from prefect._vendor.fastapi.params import Body, Query
from prefect._vendor.starlette.requests import Request
from sqlalchemy.ext.asyncio import AsyncSession

from prefect.logging import get_logger
from prefect.server.database.dependencies import provide_database_interface
from prefect.server.database.interface import PrefectDBInterface
from prefect.server.events import messaging
from prefect.server.events.counting import (
    Countable,
    InvalidEventCountParameters,
    TimeUnit,
)
from prefect.server.events.filters import EventFilter
from prefect.server.events.schemas.events import Event, EventCount, EventPage
from prefect.server.events.storage import INTERACTIVE_PAGE_SIZE, InvalidTokenError
from prefect.server.events.storage.database import (
    count_events,
    query_events,
    query_next_page,
)
from prefect.server.utilities import subscriptions
from prefect.server.utilities.server import PrefectRouter

logger = get_logger(__name__)


router = PrefectRouter(prefix="/events", tags=["Events"])


@router.post("", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def create_events(events: List[Event]):
    """Record a batch of Events"""
    await messaging.publish([event.receive() for event in events])


@router.websocket("/in")
async def stream_events_in(websocket: WebSocket) -> None:
    """Open a WebSocket to stream incoming Events"""

    await websocket.accept()

    try:
        async with messaging.create_event_publisher() as publisher:
            async for event_json in websocket.iter_text():
                event = Event.parse_raw(event_json)
                await publisher.publish_event(event.receive())
    except subscriptions.NORMAL_DISCONNECT_EXCEPTIONS:  # pragma: no cover
        pass  # it's fine if a client disconnects either normally or abnormally

    return None


def verified_page_token(
    page_token: str = Query(..., alias="page-token"),
) -> str:
    try:
        page_token = base64.b64decode(page_token.encode()).decode()
    except Exception:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    if not page_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    return page_token


@router.post(
    "/filter",
)
async def read_events(
    request: Request,
    filter: Optional[EventFilter] = Body(
        None,
        description=(
            "Additional optional filter criteria to narrow down the set of Events"
        ),
    ),
    limit: int = Body(
        INTERACTIVE_PAGE_SIZE,
        ge=0,
        le=INTERACTIVE_PAGE_SIZE,
        embed=True,
        description="The number of events to return with each page",
    ),
    db: PrefectDBInterface = Depends(provide_database_interface),
) -> EventPage:
    """
    Queries for Events matching the given filter criteria in the given Account.  Returns
    the first page of results, and the URL to request the next page (if there are more
    results).
    """
    filter = filter or EventFilter()
    async with db.session_context() as session:
        events, total, next_token = await query_events(
            session=session,
            filter=filter,
            page_size=limit,
        )

        return EventPage(
            events=events,
            total=total,
            next_page=generate_next_page_link(request, next_token),
        )


@router.get(
    "/filter/next",
)
async def read_account_events_page(
    request: Request,
    page_token: str = Depends(verified_page_token),
    db: PrefectDBInterface = Depends(provide_database_interface),
) -> EventPage:
    """
    Returns the next page of Events for a previous query against the given Account, and
    the URL to request the next page (if there are more results).
    """
    async with db.session_context() as session:
        try:
            events, total, next_token = await query_next_page(
                session=session, page_token=page_token
            )
        except InvalidTokenError:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

        return EventPage(
            events=events,
            total=total,
            next_page=generate_next_page_link(request, next_token),
        )


def generate_next_page_link(
    request: Request,
    page_token: "str | None",
) -> "str | None":
    if not page_token:
        return None

    next_page = (
        f"{request.base_url}api/events/filter/next"
        f"?page-token={base64.b64encode(page_token.encode()).decode()}"
    )
    return next_page


@router.post(
    "/count-by/{countable}",
)
async def count_account_events(
    filter: EventFilter,
    countable: Countable = Path(...),
    time_unit: TimeUnit = Body(default=TimeUnit.day),
    time_interval: float = Body(default=1.0, ge=0.01),
    db: PrefectDBInterface = Depends(provide_database_interface),
) -> List[EventCount]:
    """
    Returns distinct objects and the count of events associated with them.  Objects
    that can be counted include the day the event occurred, the type of event, or
    the IDs of the resources associated with the event.
    """
    async with db.session_context() as session:
        return await handle_event_count_request(
            session=session,
            filter=filter,
            countable=countable,
            time_unit=time_unit,
            time_interval=time_interval,
        )


async def handle_event_count_request(
    session: AsyncSession,
    filter: EventFilter,
    countable: Countable,
    time_unit: TimeUnit,
    time_interval: float,
) -> List[EventCount]:
    logger.debug(
        "countable %s, time_unit %s, time_interval %s, events filter: %s",
        countable,
        time_unit,
        time_interval,
        filter.json(),
    )

    try:
        return await count_events(
            session=session,
            filter=filter,
            countable=countable,
            time_unit=time_unit,
            time_interval=time_interval,
        )
    except InvalidEventCountParameters as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        )
