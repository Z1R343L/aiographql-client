from __future__ import annotations

import asyncio
import uuid
from asyncio import CancelledError, Task
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, NoReturn, Optional, Union

import aiohttp
from cafeteria.asyncio.callbacks import CallbackRegistry, SimpleTriggerCallback

from aiographql.client.helpers import create_default_connector
from aiographql.client.transaction import (
    GraphQLBaseResponse,
    GraphQLRequestContainer,
    GraphQLResponse,
)


class GraphQLSubscriptionEventType(Enum):
    CONNECTION_INIT = "connection_init"
    CONNECTION_ACK = "connection_ack"
    CONNECTION_ERROR = "connection_error"
    CONNECTION_TERMINATE = "connection_terminate"
    START = "start"
    DATA = "data"
    ERROR = "error"
    COMPLETE = "complete"
    STOP = "stop"
    KEEP_ALIVE = "ka"


@dataclass(frozen=True)
class GraphQLSubscriptionEvent(GraphQLBaseResponse):
    subscription_id: Optional[str] = field(default=None)

    @property
    def id(self) -> Optional[str]:
        return self.json.get("id")

    @property
    def type(self) -> Optional[GraphQLSubscriptionEventType]:
        try:
            return GraphQLSubscriptionEventType(self.json.get("type"))
        except ValueError:
            pass

    @property
    def payload(self) -> Optional[Union[GraphQLResponse, str]]:
        payload = self.json.get("payload")
        if payload is not None:
            if self.type in (
                GraphQLSubscriptionEventType.DATA,
                GraphQLSubscriptionEventType.ERROR,
            ):
                return GraphQLResponse(request=self.request, json=payload)
            return payload


@dataclass(frozen=True)
class GraphQLSubscription(GraphQLRequestContainer):
    id: str = field(default_factory=lambda: str(uuid.uuid4()), init=False)
    callbacks: CallbackRegistry = field(default_factory=CallbackRegistry)
    stop_event_types: List[GraphQLSubscriptionEventType] = field(
        default_factory=lambda: [
            GraphQLSubscriptionEventType.ERROR,
            GraphQLSubscriptionEventType.CONNECTION_ERROR,
            GraphQLSubscriptionEventType.COMPLETE,
        ]
    )
    task: Task = field(default=None, init=False, compare=False)

    @property
    def is_running(self) -> bool:
        return (
            self.task is not None and not self.task.done() and not self.task.cancelled()
        )

    @property
    def is_complete(self) -> bool:
        return self.task is not None and (self.task.done() or self.task.cancelled())

    def connection_init_request(self) -> Dict[str, Any]:
        return {
            "type": GraphQLSubscriptionEventType.CONNECTION_INIT.value,
            "payload": {"headers": {**self.request.headers}},
        }

    def connection_start_request(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": GraphQLSubscriptionEventType.START.value,
            "payload": self.request.payload(),
        }

    def connection_stop_request(self) -> Dict[str, Any]:
        return {"id": self.id, "type": GraphQLSubscriptionEventType.STOP.value}

    def is_stop_event(self, event: GraphQLSubscriptionEvent) -> bool:
        return event.type in self.stop_event_types

    async def handle(self, event: GraphQLSubscriptionEvent) -> NoReturn:
        if event.id is None or event.id == self.id:
            await self.callbacks.handle_event(event.type, event)

    async def _create_websocket_session(
        self, endpoint: str, session: aiohttp.ClientSession
    ) -> None:
        async with session.ws_connect(endpoint) as ws:
            await ws.send_json(data=self.connection_init_request())

            self.callbacks.register(
                GraphQLSubscriptionEventType.CONNECTION_ACK,
                SimpleTriggerCallback(
                    function=ws.send_json, data=self.connection_start_request()
                ),
            )

            try:
                async for msg in ws:  # type:  aiohttp.WSMessage
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type == aiohttp.WSMsgType.ERROR:
                            break
                        continue

                    event = GraphQLSubscriptionEvent(
                        subscription_id=self.id, request=self.request, json=msg.json(),
                    )
                    await self.handle(event=event)

                    if self.is_stop_event(event):
                        break
            except (asyncio.CancelledError, KeyboardInterrupt):
                await ws.send_json(data=self.connection_stop_request())

    def subscribe(
        self,
        endpoint: str,
        force: bool = False,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        """
        Create a websocket subscription and set internal task.

        :param endpoint: GraphQL endpoint to subscribe to
        :param force: Force re-subscription if already subscribed
        :param session: Optional `aiohttp.ClientSession` to use for requests
        """
        if self.is_running and not force:
            return
        self.unsubscribe()
        if session:
            task = asyncio.create_task(
                self._create_websocket_session(endpoint=endpoint, session=session)
            )
        else:
            connector = await create_default_connector()
            with aiohttp.ClientSession(connector=connector) as session:
                task = asyncio.create_task(
                    self._create_websocket_session(endpoint=endpoint, session=session)
                )
        object.__setattr__(self, "task", task)

    def unsubscribe(self) -> None:
        """
        Unsubscribe current websocket subscription if active and clear internal task.
        """
        if self.is_running:
            try:
                self.task.cancel()
            except CancelledError:
                pass
        object.__setattr__(self, "task", None)
