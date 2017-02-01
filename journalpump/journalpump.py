# Copyright 2015, Aiven, https://aiven.io/
#
# This file is under the Apache License, Version 2.0.
# See the file `LICENSE` for details.

from . daemon import ServiceDaemon
from . import geohash, statsd
from elasticsearch import Elasticsearch, helpers
from elasticsearch import exceptions
from kafka import KafkaClient, SimpleProducer
from kafka.protocol import CODEC_SNAPPY, CODEC_NONE
from requests import Session
from systemd.journal import Reader
from threading import Thread, Lock
import copy
import datetime
import json
import kafka.common
import logging
import os
import re
import socket
import systemd.journal
import time
import uuid

try:
    import snappy
except ImportError:
    snappy = None

try:
    from geoip2.database import Reader as GeoIPReader
except ImportError:
    GeoIPReader = None


KAFKA_CONN_ERRORS = tuple(kafka.common.RETRY_ERROR_TYPES) + (
    kafka.common.UnknownError,
    socket.timeout,
)


KAFKA_COMPRESSED_MESSAGE_OVERHEAD = 30
MAX_KAFKA_MESSAGE_SIZE = 1024 ** 2


logging.getLogger("elasticsearch").setLevel(logging.ERROR)
logging.getLogger("kafka").setLevel(logging.CRITICAL)  # remove client-internal tracebacks from logging output


def _convert_uuid(s):
    return str(uuid.UUID(s.decode()))


def convert_mon(s):  # pylint: disable=unused-argument
    return None


def convert_realtime(t):
    return int(t) / 1000000.0  # Stock systemd transforms these into datetimes


converters = {
    "MESSAGE_ID": _convert_uuid,
    "_MACHINE_ID": _convert_uuid,
    "_BOOT_ID": _convert_uuid,
    "_SOURCE_REALTIME_TIMESTAMP": convert_realtime,
    "__REALTIME_TIMESTAMP": convert_realtime,
    "_SOURCE_MONOTONIC_TIMESTAMP": convert_mon,
    "__MONOTONIC_TIMESTAMP": convert_mon,
    "COREDUMP_TIMESTAMP": convert_realtime
}

systemd.journal.DEFAULT_CONVERTERS.update(converters)


class JournalObject:
    def __init__(self, cursor=None, entry=None):
        self.cursor = cursor
        self.entry = entry or {}


class PumpReader(Reader):
    def _convert_field(self, key, value):
        try:
            convert = self.converters[key]
            return convert(value)
        except (KeyError, ValueError):
            # Leave in default bytes
            try:
                return bytes.decode(value)
            except:  # pylint: disable=bare-except
                return value

    def get_next(self, skip=1):
        # pylint: disable=no-member, protected-access
        """Private get_next implementation that doesn't store the cursor since we don't want it"""
        if super()._next(skip):
            entry = super()._get_all()
            if entry:
                entry["__REALTIME_TIMESTAMP"] = self._get_realtime()
                return JournalObject(cursor=self._get_cursor(), entry=self._convert_entry(entry))
        return JournalObject()


class LogSender(Thread):
    def __init__(self, config, msg_buffer, stats, max_send_interval):
        super().__init__()
        self.log = logging.getLogger("LogSender")
        self.stats = stats
        self.config = config
        self.cursor = None
        self.last_send_time = time.time()
        self.last_state_save_time = time.time()
        self.msg_buffer = msg_buffer
        self.max_send_interval = max_send_interval
        self.start_time = time.time()
        self.previous_state = None
        self.running = True
        self.log.info("Initialized LogSender")

    def send_messages(self, message_batch):
        pass

    def maintenance_operations(self):
        # This can be overridden in the classes that inherit this
        pass

    def run(self):
        while self.running:
            self.maintenance_operations()
            if len(self.msg_buffer) > 1000 or \
               time.time() - self.last_send_time > self.max_send_interval:
                self.get_and_send_messages()
            else:
                time.sleep(0.1)
        self.log.info("Stopping")

    def get_and_send_messages(self):
        start_time = time.time()
        try:
            messages, cursor = self.msg_buffer.get_items()
            msg_count = len(messages)
            self.log.debug("Got %d items from msg_buffer, cursor: %r", msg_count, cursor)
            while self.running and messages:
                batch_size = len(messages[0]) + KAFKA_COMPRESSED_MESSAGE_OVERHEAD
                index = 1
                while index < len(messages):
                    item_size = len(messages[index]) + KAFKA_COMPRESSED_MESSAGE_OVERHEAD
                    if batch_size + item_size >= MAX_KAFKA_MESSAGE_SIZE:
                        break
                    batch_size += item_size
                    index += 1

                messages_batch = messages[:index]
                if self.send_messages(messages_batch):
                    messages = messages[index:]

            self.cursor = cursor
            self.log.debug("Sending %d msgs, cursor: %r took %.4fs",
                           msg_count, self.cursor, time.time() - start_time)

            if time.time() - self.last_state_save_time > 1.0:
                self.save_state()
            self.last_send_time = time.time()
        except:  # pylint: disable=bare-except
            self.log.exception("Problem sending messages: %r", messages)
            time.sleep(0.5)

    def save_state(self):
        state_to_save = {
            "cursor": self.cursor,
            "total_size": self.msg_buffer.total_size,
            "entry_num": self.msg_buffer.entry_num,
            "start_time": self.start_time,
            "current_queue": len(self.msg_buffer)
        }

        if state_to_save != self.previous_state:
            with open(self.config.get("json_state_file_path", "journalpump_state.json"), "w") as fp:
                json.dump(state_to_save, fp, indent=4, sort_keys=True)
                self.previous_state = state_to_save
                self.log.debug("Wrote state file: %r, %.2f entries/s processed", state_to_save,
                               self.msg_buffer.entry_num / (time.time() - self.start_time))


class KafkaSender(LogSender):
    def __init__(self, config, msg_buffer, stats):
        super().__init__(config=config, msg_buffer=msg_buffer, stats=stats,
                         max_send_interval=config.get("max_send_interval", 0.3))
        self.config = config
        self.msg_buffer = msg_buffer
        self.stats = stats

        self.kafka = None
        self.kafka_producer = None

        if not isinstance(self.config["kafka_topic"], bytes):
            topic = self.config["kafka_topic"].encode("utf8")
        self.topic = topic

    def _init_kafka(self):
        self.log.info("Initializing Kafka client, address: %r", self.config["kafka_address"])
        while self.running:
            try:
                if self.kafka_producer:
                    self.kafka_producer.stop()
                if self.kafka:
                    self.kafka.close()

                self.kafka = KafkaClient(  # pylint: disable=unexpected-keyword-arg
                    self.config["kafka_address"],
                    ssl=self.config.get("ssl", False),
                    certfile=self.config.get("certfile"),
                    keyfile=self.config.get("keyfile"),
                    ca=self.config.get("ca")
                )
                self.kafka_producer = SimpleProducer(self.kafka, codec=CODEC_SNAPPY
                                                     if snappy else CODEC_NONE)
                self.log.info("Initialized Kafka Client, address: %r", self.config["kafka_address"])
                break
            except KAFKA_CONN_ERRORS as ex:
                self.log.warning("Retriable error during Kafka initialization: %s: %s, sleeping",
                                 ex.__class__.__name__, ex)
            self.kafka = None
            self.kafka_producer = None
            time.sleep(5.0)

    def send_messages(self, message_batch):
        if not self.kafka:
            self._init_kafka()
        try:
            self.kafka_producer.send_messages(self.topic, *message_batch)
            return True
        except KAFKA_CONN_ERRORS as ex:
            self.log.info("Kafka retriable error during send: %s: %s, waiting", ex.__class__.__name__, ex)
            time.sleep(0.5)
            self._init_kafka()
        except Exception as ex:  # pylint: disable=broad-except
            self.log.exception("Unexpected exception during send to kafka")
            self.stats.unexpected_exception(ex=ex, where="sender", tags={"app": "journalpump"})
            time.sleep(5.0)
            self._init_kafka()


class FileSender(LogSender):
    def __init__(self, config, msg_buffer, stats):
        super().__init__(config=config, msg_buffer=msg_buffer, stats=stats,
                         max_send_interval=config.get("max_send_interval", 0.3))
        self.config = config
        self.output = open(config["file_output"], "ab")

    def send_messages(self, message_batch):
        for msg in message_batch:
            self.output.write(msg + b"\n")
        return True


class ElasticsearchSender(LogSender):
    def __init__(self, config, msg_buffer, stats):
        super().__init__(config=config, msg_buffer=msg_buffer, stats=stats,
                         max_send_interval=config.get("max_send_interval", 10.0))
        self.config = config
        self.msg_buffer = msg_buffer
        self.stats = stats
        self.elasticsearch_url = self.config.get("elasticsearch_url")
        self.last_index_check_time = 0
        self.request_timeout = self.config.get("elasticsearch_timeout", 10.0)
        self.index_days_max = self.config.get("elasticsearch_index_days_max", 3)
        self.index_name = self.config.get("elasticsearch_index_prefix", "journalpump")
        self.es = None
        self.indices = set()

    def _init_es(self):
        while self.es is None and self.running is True:
            try:
                self.es = Elasticsearch([self.elasticsearch_url], timeout=self.request_timeout)
                self.indices = set(self.es.indices.get_aliases())  # pylint: disable=no-member
                break
            except exceptions.ConnectionError:   # pylint: disable=bare-except
                self.es = None
                self.log.warning("Could not initialize Elasticsearch, %r", self.elasticsearch_url)
                time.sleep(1.0)
        if self.es:
            return True

    def create_index_and_mappings(self, index_name):
        try:
            self.log.info("Creating index: %r", index_name)
            self.es.indices.create(index_name, {
                "mappings": {
                    "journal_msg": {
                        "properties": {
                            "SYSTEMD_SESSION": {"type": "string"},
                            "SESSION_ID": {"type": "string"},
                        }
                    }
                }
            })
            self.indices.add(index_name)
        except exceptions.RequestError as ex:
            self.log.exception("Problem creating index: %r %r", index_name, ex)

    def check_indices(self):
        if not self._init_es():
            return
        indices = sorted(key for key in self.es.indices.get_aliases().keys()  # pylint: disable=no-member
                         if key.startswith(self.index_name))
        self.log.info("Checking indices, currently: %r are available", indices)
        while len(indices) > self.index_days_max:
            index_to_delete = indices.pop(0)
            self.log.info("Deleting index: %r since we only keep %d days worth of indices",
                          index_to_delete, self.index_days_max)
            try:
                self.es.indices.delete(index_to_delete)
                self.indices.discard(index_to_delete)
            except:   # pylint: disable=bare-except
                self.log.exception("Problem deleting index: %r", index_to_delete)

    def maintenance_operations(self):
        if time.monotonic() - self.last_index_check_time > 3600:
            self.last_index_check_time = time.monotonic()
            self.check_indices()

    def send_messages(self, message_batch):
        if not self._init_es():
            return
        start_time = time.monotonic()
        try:
            actions = []
            for msg in message_batch:
                message = json.loads(msg.decode("utf8"))
                timestamp = message.get("timestamp")
                if "__REALTIME_TIMESTAMP" in message:
                    timestamp = datetime.datetime.utcfromtimestamp(message["__REALTIME_TIMESTAMP"])
                else:
                    timestamp = datetime.datetime.utcnow()

                message["timestamp"] = timestamp
                index_name = "{}-{}".format(self.index_name, datetime.datetime.date(timestamp))
                if index_name not in self.indices:
                    self.create_index_and_mappings(index_name)

                actions.append({
                    "_index": index_name,
                    "_type": "journal_msg",
                    "_source": message,
                })
            if actions:
                helpers.bulk(self.es, actions)
                self.log.debug("Sent %d log events to ES, took: %.2fs",
                               len(message_batch), time.monotonic() - start_time)
        except Exception as ex:  # pylint: disable=broad-except
            short_msg = str(ex)[:200]
            self.log.warning("Problem sending logs to ES: %s: %s", ex.__class__.__name__, short_msg)
            return False
        return True


class LogplexSender(LogSender):
    def __init__(self, config, msg_buffer, stats):
        super().__init__(config=config, msg_buffer=msg_buffer, stats=stats,
                         max_send_interval=config.get("max_send_interval", 5.0))
        self.config = config
        self.msg_buffer = msg_buffer
        self.stats = stats
        self.logplex_input_url = self.config["logplex_log_input_url"]
        self.request_timeout = self.config.get("logplex_request_timeout", 2)
        self.logplex_token = self.config["logplex_token"]
        self.session = Session()
        self.msg_id = "-"
        self.structured_data = "-"

    def format_msg(self, msg):
        # TODO: figure out a way to optionally get the entry without JSON
        entry = json.loads(msg.decode("utf8"))
        hostname = entry.get("_HOSTNAME", "localhost")
        pid = entry.get("_PID", "localhost")
        pkt = "<190>1 {} {} {} {} {} {}".format(
            datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00 "),
            hostname,
            self.logplex_token,
            pid,
            self.msg_id,
            self.structured_data)
        pkt += entry["MESSAGE"]
        pkt = pkt.encode("utf8")
        return '{} {}'.format(len(pkt), pkt)

    def send_messages(self, message_batch):
        auth = ('token', self.config["logplex_token"])
        msg_data = ''.join([self.format_msg(msg) for msg in message_batch])
        msg_count = len(message_batch)
        headers = {
            "Content-Type": "application/logplex-1",
            "Logplex-Msg-Count": msg_count,
        }
        self.session.post(
            self.logplex_input_url,
            auth=auth,
            headers=headers,
            data=msg_data,
            timeout=self.request_timeout,
            verify=False
        )


class MsgBuffer:
    def __init__(self, cursor=None):
        self.log = logging.getLogger("MsgBuffer")
        self.msg_buffer = []
        self.lock = Lock()
        self.cursor = cursor
        self.entry_num = 0
        self.total_size = 0
        self.last_journal_msg_time = time.monotonic()
        self.log.info("Initialized MsgBuffer with cursor: %r", cursor)

    def __len__(self):
        return len(self.msg_buffer)

    def get_items(self):
        messages = []
        with self.lock:
            if self.msg_buffer:
                messages = self.msg_buffer
                self.msg_buffer = []
        return messages, self.cursor

    def set_cursor(self, cursor):
        self.cursor = cursor
        self.last_journal_msg_time = time.monotonic()

    def set_item(self, item, cursor):
        with self.lock:
            self.msg_buffer.append(item)
            self.cursor = cursor
            self.last_journal_msg_time = time.monotonic()
        self.entry_num += 1
        self.total_size += len(item)


class JournalPump(ServiceDaemon):
    def __init__(self, config_path):
        self.stats = None  # required by handle_new_config()
        self.searches = []
        self.geoip = None
        super().__init__(config_path=config_path, multi_threaded=True, log_level=logging.INFO)
        self.journald_reader = None
        self.msg_buffer = MsgBuffer(self.load_state())
        self.sender = None
        self.init_reader()

    def init_reader(self):
        if self.journald_reader:
            self.journald_reader.close()  # pylint: disable=no-member
            self.journald_reader = None

        if self.config.get("journal_path"):
            while self.running:
                try:
                    self.journald_reader = PumpReader(path=self.config["journal_path"])
                    break
                except FileNotFoundError as ex:
                    self.log.warning("journal not available yet, waiting: %s: %s",
                                     ex.__class__.__name__, ex)
                    time.sleep(5.0)
        else:
            self.journald_reader = PumpReader()

        for unit_to_match in self.config.get("units_to_match", []):
            self.journald_reader.add_match(_SYSTEMD_UNIT=unit_to_match)

        if self.msg_buffer.cursor:
            self.journald_reader.seek_cursor(self.msg_buffer.cursor)  # pylint: disable=no-member

    def ip_to_geohash(self, tags, args):
        """ip_to_geohash(ip_tag_name,precision) -> Convert IP address to geohash"""
        if len(args) > 1:
            precision = int(args[1])
        else:
            precision = 8
        ip = tags[args[0]]
        res = self.geoip.city(ip)
        if not res:
            return ""

        loc = res.location
        return geohash.encode(loc.latitude, loc.longitude, precision)  # pylint: disable=no-member

    def _build_searches(self):
        """
        Pre-generate regex objects and tag value conversion methods for searches
        """
        # Example:
        # {"name": "service_stop", "tags": {"foo": "bar"}, "search": {"MESSAGE": "Stopped target (?P<target>.+)\\."}}
        re_op = re.compile("(?P<func>[a-z_]+)\\((?P<args>[a-z0-9_,]+)\\)")
        funcs = {
            "ip_to_geohash": self.ip_to_geohash,
        }
        for search in self.config.get("searches", []):
            search.setdefault("tags", {})
            search.setdefault("fields", {})
            output = copy.deepcopy(search)
            for name, pattern in output["fields"].items():
                output["fields"][name] = re.compile(pattern)

            for tag, value in search["tags"].items():
                if "(" in value or ")" in value:
                    # Tag uses a method conversion call, e.g. "ip_to_geohash(ip_address,5)"
                    match = re_op.search(value)
                    if not match:
                        raise Exception("Invalid tag function tag value: {!r}".format(value))
                    func_name = match.groupdict()["func"]
                    try:
                        f = funcs[func_name]  # pylint: disable=unused-variable
                    except KeyError:
                        raise Exception("Unknown tag function {!r} in {!r}".format(func_name, value))

                    args = match.groupdict()["args"].split(",")  # pylint: disable=unused-variable

                    def value_func(tags, f=f, args=args):  # pylint: disable=undefined-variable
                        return f(tags, args)

                    output["tags"][tag] = value_func

            yield output

    def handle_new_config(self):
        """Called by ServiceDaemon when config has changed"""
        stats = self.config.get("statsd") or {}
        self.stats = statsd.StatsClient(
            host=stats.get("host"),
            port=stats.get("port"),
            tags=stats.get("tags"),
        )
        self.searches = list(self._build_searches())
        geoip_db_path = self.config.get("geoip_database")
        if geoip_db_path:
            self.log.info("Loading GeoIP data from %r", geoip_db_path)
            self.geoip = GeoIPReader(geoip_db_path)

    def sigterm(self, signum, frame):
        if self.sender:
            self.sender.running = False
        super().sigterm(signum, frame)

    def load_state(self):
        filepath = self.config.get("json_state_file_path", "journalpump_state.json")
        if os.path.exists(filepath):
            with open(filepath, "r") as fp:
                state_file = json.load(fp)
            return state_file["cursor"]
        return None

    def check_match(self, entry):
        if not self.config.get("match_key"):
            return True
        elif entry.get(self.config["match_key"]) == self.config["match_value"]:
            return True
        return False

    def initialize_sender(self):
        if not self.sender:
            senders = {
                "elasticsearch": ElasticsearchSender,
                "kafka": KafkaSender,
                "logplex": LogplexSender,
                "file": FileSender,
            }
            class_name = senders.get(self.config["output_type"])
            self.sender = class_name(config=self.config, msg_buffer=self.msg_buffer, stats=self.stats)
            self.sender.start()

    def perform_searches(self, jobject):
        entry = jobject.entry
        for search in self.searches:
            all_match = True
            tags = {}
            for field, regex in search["fields"].items():
                line = entry.get(field, "")
                if not line:
                    all_match = False
                    break

                if isinstance(line, bytes):
                    try:
                        line = line.decode("utf-8")
                    except UnicodeDecodeError:
                        # best-effort decode failed
                        all_match = False
                        break

                match = regex.search(line)
                if not match:
                    all_match = False
                    break
                else:
                    field_values = match.groupdict()
                    tags = {}
                    for tag, value in field_values.items():
                        tags[tag] = value

                    for tag, value in search.get("tags", {}).items():
                        if isinstance(value, str):
                            tags[tag] = value
                        else:
                            tags[tag] = value(tags)
            if all_match:
                self.stats.increase(search["name"], tags=tags)
                search["hits"] = search.get("hits", 0) + 1

    def run(self):
        last_stats_time = 0
        while self.running:
            entry = None
            try:
                self.initialize_sender()
                msg_buffer_length = len(self.msg_buffer)
                if msg_buffer_length > self.config.get("msg_buffer_max_length", 50000):
                    # This makes the self.msg_buffer grow to at most msg_buffer_max_length entries
                    self.log.debug("%d entries in msg buffer, slowing down a bit by sleeping",
                                   msg_buffer_length)
                    time.sleep(1.0)
                    continue

                jobject = next(self.journald_reader)
                new_entry = {}
                for key, value in jobject.entry.items():
                    if isinstance(value, bytes):
                        new_entry[key.lstrip("_")] = repr(value)  # value may be bytes in any encoding
                    else:
                        new_entry[key.lstrip("_")] = value

                self.perform_searches(jobject)
                if jobject.cursor is not None:
                    if not self.check_match(new_entry):
                        self.msg_buffer.set_cursor(jobject.cursor)
                        continue
                    json_entry = json.dumps(new_entry).encode("utf8")
                    if len(json_entry) > MAX_KAFKA_MESSAGE_SIZE:
                        self.stats.increase("journal.error", tags={"error": "too_long"})
                        error = "too large message {} bytes vs maximum {} bytes".format(
                            len(json_entry), MAX_KAFKA_MESSAGE_SIZE)
                        self.log.warning("%s: %s ...", error, json_entry[:1024])
                        entry = {
                            "error": error,
                            "partial_data": json_entry[:1024],
                        }
                        json_entry = json.dumps(entry).encode("utf8")
                    self.stats.increase("journal.lines")
                    self.stats.increase("journal.bytes", inc_value=len(json_entry))
                    self.msg_buffer.set_item(json_entry, jobject.cursor)
                else:
                    self.log.debug("No more journal entries to read, sleeping")
                    if time.monotonic() - self.msg_buffer.last_journal_msg_time > 180 and self.msg_buffer.cursor:
                        self.log.info("We haven't seen any msgs in 180s, reinitiate PumpReader() and seek to: %r",
                                      self.msg_buffer.cursor)
                        self.init_reader()
                        self.msg_buffer.last_journal_msg_time = time.monotonic()
                    time.sleep(0.5)
            except StopIteration:
                self.log.debug("No more journal entries to read, sleeping")
                time.sleep(0.5)
            except Exception as ex:  # pylint: disable=broad-except
                self.log.exception("Unexpected exception during handling entry: %r", jobject)
                self.stats.unexpected_exception(ex=ex, where="mainloop", tags={"app": "journalpump"})
                time.sleep(0.5)

            if self.searches and time.monotonic() - last_stats_time > 60.0:
                self.log.info("search hits stats: %s",
                              ", ".join("{}={}".format(s["name"], s.get("hits", 0)) for s in self.searches))
                last_stats_time = time.monotonic()

            self.ping_watchdog()


if __name__ == "__main__":
    JournalPump.run_exit()
