import abc
import logging
from functools import wraps
import traceback
import asyncio
from asyncio import AbstractEventLoop, Task
from typing import (
    Any,
    Type,
    Callable,
    Coroutine,
    Union,
    Optional,
    TypeVar,
    Generic,
)
import json

from aioamqp.channel import Channel
from aioamqp.envelope import Envelope
from aioamqp.properties import Properties

from easyqueue.connection import AMQPConnection
from easyqueue.message import AMQPMessage
from easyqueue.queue import BaseQueue
from easyqueue.exceptions import MessageError


def _ensure_connected(coro: Callable[..., Coroutine]):
    @wraps(coro)
    async def wrapper(self: "AsyncJsonQueue", *args, **kwargs):
        retries = 0
        while self.is_running and not self.connection.is_connected:
            try:
                await self.connection._connect()
                break
            except Exception as e:
                await asyncio.sleep(self.seconds_between_conn_retry)
                retries += 1
                if self.logger:
                    self.logger.error(
                        {
                            "event": "reconnect-failure",
                            "retry_count": retries,
                            "exc_traceback": traceback.format_tb(
                                e.__traceback__
                            ),
                        }
                    )
        return await coro(self, *args, **kwargs)

    return wrapper


T = TypeVar("T")


class _ConsumptionHandler:
    def __init__(
        self,
        delegate: "AsyncQueueConsumerDelegate",
        queue: "AsyncJsonQueue",
        queue_name: str,
    ) -> None:
        self.delegate = delegate
        self.queue = queue
        self.loop = queue.loop
        self.queue_name = queue_name
        self.consumer_tag: Optional[str] = None

    async def _handle_callback(self, callback, **kwargs):
        """
        Chains the callback coroutine into a try/except and calls
        `on_message_handle_error` in case of failure, avoiding unhandled
        exceptions.

        :param callback:
        :param kwargs:
        :return:
        """
        try:
            return await callback(**kwargs)
        except Exception as e:
            return await self.delegate.on_message_handle_error(
                handler_error=e, **kwargs
            )

    async def handle_message(
        self,
        channel: Channel,
        body: bytes,
        envelope: Envelope,
        properties: Properties,
    ) -> Task:
        msg = AMQPMessage(
            connection=self.queue.connection,
            channel=channel,
            envelope=envelope,
            properties=properties,
            delivery_tag=envelope.delivery_tag,
            deserialization_method=self.queue.deserialize,
            queue_name=self.queue_name,
            serialized_data=body,
        )

        callback = self._handle_callback(
            self.delegate.on_queue_message, msg=msg  # type: ignore
        )
        return self.loop.create_task(callback)


class AsyncJsonQueue(BaseQueue, Generic[T]):
    _transport: Optional[asyncio.BaseTransport]

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        delegate_class: Type["AsyncQueueConsumerDelegate"] = None,
        delegate: Optional["AsyncQueueConsumerDelegate"] = None,
        virtual_host: str = "/",
        heartbeat: int = 60,
        prefetch_count: int = 100,
        max_message_length=0,
        loop: AbstractEventLoop = None,
        seconds_between_conn_retry: int = 1,
        logger: logging.Logger = None,
    ) -> None:
        super().__init__(host, username, password, virtual_host, heartbeat)

        self.loop = loop or asyncio.get_event_loop()

        if delegate is not None and delegate_class is not None:
            raise ValueError("Cant provide both delegate and delegate_class")

        if delegate_class is not None:
            self.delegate = delegate_class()
        else:
            self.delegate = delegate  # type: ignore

        self.prefetch_count = prefetch_count

        if max_message_length < 0:
            raise ValueError("max_message_length must be a positive integer")

        self.max_message_length = max_message_length

        on_error = self.delegate.on_connection_error if self.delegate else None

        self.connection = AMQPConnection(
            host=host,
            username=username,
            password=password,
            virtual_host=virtual_host,
            heartbeat=heartbeat,
            on_error=on_error,
            loop=loop,
        )

        self.seconds_between_conn_retry = seconds_between_conn_retry
        self.is_running = True
        self.logger = logger

    def serialize(self, body: T, **kwargs) -> str:
        return json.dumps(body, **kwargs)

    def deserialize(self, body: bytes) -> T:
        return json.loads(body.decode())

    @_ensure_connected
    async def ack(self, msg: AMQPMessage[T]):
        return await msg.channel.basic_client_ack(msg.delivery_tag)

    @_ensure_connected
    async def reject(self, msg: AMQPMessage[T], requeue=False):
        return await msg.channel.basic_reject(
            delivery_tag=msg.delivery_tag, requeue=requeue
        )

    @_ensure_connected
    async def put(
        self,
        routing_key: str,
        data: Any = None,
        serialized_data: Union[str, bytes] = "",
        exchange: str = "",
    ):
        """
        :param data: A serializable data that should be serialized before
        publishing
        :param serialized_data: A payload to be published as is
        :param exchange: The exchange to publish the message
        :param routing_key: The routing key to publish the message
        """
        if data and serialized_data:
            raise ValueError("Only one of data or json should be specified")

        if data:
            serialized_data = self.serialize(data, ensure_ascii=False)

        if not isinstance(serialized_data, bytes):
            serialized_data = serialized_data.encode()

        return await self.connection.channel.publish(
            payload=serialized_data,
            exchange_name=exchange,
            routing_key=routing_key,
        )

    @_ensure_connected
    async def consume(
        self,
        queue_name: str,
        delegate: "AsyncQueueConsumerDelegate",
        consumer_name: str = "",
    ) -> str:
        """
        Connects the client if needed and starts queue consumption, sending
        `on_before_start_consumption` and `on_consumption_start` notifications
        to the delegate object

        :param queue_name: queue name to consume from
        :param consumer_name: An optional name to be used as a consumer
        identifier. If one isn't provided, a random one is generated by the
        broker
        :return: The consumer tag. Useful for cancelling/stopping consumption
        """
        # todo: Implement a consumer tag generator
        handler = _ConsumptionHandler(
            delegate=delegate, queue=self, queue_name=queue_name
        )

        await delegate.on_before_start_consumption(
            queue_name=queue_name, queue=self
        )
        await self.connection.channel.basic_qos(
            prefetch_count=self.prefetch_count,
            prefetch_size=0,
            connection_global=False,
        )
        tag = await self.connection.channel.basic_consume(
            callback=handler.handle_message,
            consumer_tag=consumer_name,
            queue_name=queue_name,
        )
        consumer_tag = tag["consumer_tag"]
        await delegate.on_consumption_start(
            consumer_tag=consumer_tag, queue=self
        )
        handler.consumer_tag = consumer_tag
        return consumer_tag

    async def stop_consumer(self, consumer_tag: str):
        if self.connection.channel is None:
            raise ConnectionError(
                "Queue isn't connected. "
                "Did you forgot to wait for `connect()`?"
            )

        return await self.connection.channel.basic_cancel(consumer_tag)


class AsyncQueueConsumerDelegate(metaclass=abc.ABCMeta):
    async def on_before_start_consumption(
        self, queue_name: str, queue: AsyncJsonQueue
    ):
        """
        Coroutine called before queue consumption starts. May be overwritten to
        implement further custom initialization.

        :param queue_name: Queue name that will be consumed
        :type queue_name: str
        :param queue: AsynQueue instanced
        :type queue: AsyncJsonQueue
        """
        pass

    async def on_consumption_start(
        self, consumer_tag: str, queue: AsyncJsonQueue
    ):
        """
        Coroutine called once consumption started.
        """

    @abc.abstractmethod
    async def on_queue_message(self, msg: AMQPMessage[Any]):
        """
        Callback called every time that a new, valid and deserialized message
        is ready to be handled.

        :param msg: the consumed message
        """
        raise NotImplementedError

    async def on_message_handle_error(self, handler_error: Exception, **kwargs):
        """
        Callback called when an uncaught exception was raised during message
        handling stage.

        :param handler_error: The exception that triggered
        :param kwargs: arguments used to call the coroutine that handled
        the message
        :return:
        """
        pass

    async def on_connection_error(self, exception: Exception):
        """
        Called when the connection fails
        """
        pass
