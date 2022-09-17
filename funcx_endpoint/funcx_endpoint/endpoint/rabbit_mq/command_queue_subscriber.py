from __future__ import annotations

import logging
import queue
import threading
import typing as t

import pika

if t.TYPE_CHECKING:
    import pika.exceptions
    from pika.channel import Channel
    from pika.frame import Method
    from pika.spec import Basic, BasicProperties

from .base import SubscriberProcessStatus

log = logging.getLogger(__name__)


class CommandQueueSubscriber(threading.Thread):
    def __init__(
        self,
        *,
        queue_info: dict,
        command_queue: queue.Queue,
        quiesce_event: threading.Event,
    ):
        super().__init__()
        self.status = SubscriberProcessStatus.parent

        self.queue_info = queue_info
        self._command_queue = command_queue
        self._quiesce_event = quiesce_event
        self._to_ack: queue.Queue[int] = queue.Queue()
        self._channel_closed = threading.Event()
        self._cleanup_complete = threading.Event()

        self._connection: pika.SelectConnection | None = None
        self._channel: Channel | None = None
        self._consumer_tag: str | None = None

        self._watcher_poll_period_s = 1

    def run(self):
        try:
            self._connection = self._connect()
            self.event_watcher()
            self._connection.ioloop.start()
        except Exception:
            log.exception("Failed to start subscriber; shutting down thread.")
            self._quiesce_event.set()

    def _connect(self) -> pika.SelectConnection:
        pika_params = pika.URLParameters(self.queue_info["connection_url"])
        return pika.SelectConnection(
            pika_params,
            on_close_callback=self._on_connection_closed,
            on_open_error_callback=self._on_open_failed,
            on_open_callback=self._on_connection_open,
        )

    def _on_open_failed(self, _mq_conn: pika.BaseConnection, exc: str | Exception):
        log.warning(f"Failed to open connection ({type(exc)}): {exc}")
        self._quiesce_event.set()

    def _on_connection_closed(self, _mq_conn: pika.BaseConnection, exc: Exception):
        log.warning(f"Connection unexpectedly closed: {exc}")
        self._quiesce_event.set()

    def _on_connection_open(self, _mq_conn: pika.BaseConnection):
        log.info("Connection established; creating channel")
        self._connection.channel(on_open_callback=self._on_channel_open)

    def _on_channel_open(self, channel: Channel):
        self._channel = channel

        channel.add_on_close_callback(self._on_channel_closed)
        channel.add_on_cancel_callback(self._on_consumer_cancelled)

        log.info(
            "Channel %s opened (%s); begin consuming messages.",
            channel.channel_number,
            channel.connection.params,
        )
        self._start_consuming()

    def _on_channel_closed(self, channel: Channel, exception: Exception):
        """
        Invoked by pika when RabbitMQ unexpectedly closes the channel.
        Channels are usually closed if you attempt to do something that
        violates the protocol, such as re-declare an EXCHANGE_NAME or queue with
        different parameters. In this case, we'll close the connection
        to shutdown the object.

        This is also invoked at the end of a successful client-initiated close, as when
        `connection.close()` is called and closes any open channels.
        """
        log.warning(f"Channel:{channel} was closed: ({exception}")
        if isinstance(exception, pika.exceptions.ChannelClosedByBroker):
            log.warning("Channel closed by RabbitMQ broker")
            if "exclusive use" in exception.reply_text:
                log.exception(
                    "Channel closed by RabbitMQ broker due to exclusive "
                    "ownership by an active endpoint"
                )
                log.warning("Channel will close without connection retry")
                self.status = SubscriberProcessStatus.closing
                self._quiesce_event.set()
        elif isinstance(exception, pika.exceptions.ChannelClosedByClient):
            log.debug("Detected channel closed by client")
        else:
            log.exception("Channel closed by unhandled exception.")
        log.debug("marking channel as closed")
        self._channel_closed.set()

    def _start_consuming(self):
        log.info("Issuing consumer related RPC commands")
        self._consumer_tag = self._channel.basic_consume(
            queue=self.queue_info["queue"],
            on_message_callback=self._on_message,
            # exclusive=True,
        )

    def _on_consumer_cancelled(self, frame: Method[Basic.CancelOk]):
        """Invoked by pika when RabbitMQ sends a Basic.Cancel for a consumer
        receiving messages.
        """
        log.info("Consumer was cancelled remotely, shutting down: %r", frame)
        if self._channel:
            self._channel.close()

    def _on_message(
        self,
        channel: Channel,
        basic_deliver: Basic.Deliver,
        properties: BasicProperties,
        body: bytes,
    ):
        """Invoked by pika when a message is delivered from RabbitMQ. The
        channel is passed for your convenience. The basic_deliver object that
        is passed in carries the EXCHANGE_NAME, routing key, delivery tag and
        a redelivered flag for the message. The properties passed in is an
        instance of BasicProperties with the message properties and the body
        is the message that was sent.
        """
        log.debug(
            "Received message from %s: %s, %s",
            basic_deliver.delivery_tag,
            properties.app_id,
            body,
        )

        try:
            self._command_queue.put((basic_deliver.delivery_tag, properties, body))
        except Exception:
            # No sense in waiting for the RMQ default 30m timeout; let it know
            # *now* that this message failed.
            log.exception("Command queue put failed")
            channel.basic_nack(basic_deliver.delivery_tag, requeue=True)

    def ack(self, msg_tag: int):
        self._to_ack.put(msg_tag)

    def _on_cancelok(self, _frame: Method[Basic.CancelOk]):
        log.info("RabbitMQ acknowledged the cancellation of the consumer")
        self._close_channel()

    def _close_channel(self):
        """Call to close the channel with RabbitMQ cleanly by issuing the
        Channel.Close RPC command.

        """
        log.info("Closing the channel")
        self._channel.close()

    def _shutdown(self):
        log.debug("closing connection")
        self._connection.close()
        log.debug("stopping ioloop")
        self._connection.ioloop.stop()
        log.debug("waiting until channel is closed (timeout=1 second)")
        if not self._channel_closed.wait(1.0):
            log.warning("reached timeout while waiting for channel closed")
        log.info("shutdown done, setting cleanup event")
        self._cleanup_complete.set()

    def event_watcher(self):
        """Polls the quiesce_event periodically to trigger a shutdown"""
        if self._quiesce_event.is_set():
            log.info("Shutting down task queue reader due to quiesce event.")
            try:
                self._shutdown()
            except Exception:
                log.exception("error while shutting down")
                raise
            log.info("Shutdown complete")
            return

        try:
            while True:
                delivery_tag = self._to_ack.get(block=False)
                self._channel.basic_ack(delivery_tag)
                log.debug("Acknowledged command: %s", delivery_tag)
        except queue.Empty:
            pass

        self._connection.ioloop.call_later(
            self._watcher_poll_period_s, self.event_watcher
        )

    def stop(self) -> None:
        """stop() is called by the parent to shutdown the subscriber"""
        log.info("Stopping")
        self._quiesce_event.set()
        log.info("Waiting for cleanup_complete")
        if not self._cleanup_complete.wait(2 * self._watcher_poll_period_s):
            log.warning("Reached timeout while waiting for cleanup complete")
        # join shouldn't block if the above did not raise a timeout
        self.join()
        log.info("Cleanup done")
