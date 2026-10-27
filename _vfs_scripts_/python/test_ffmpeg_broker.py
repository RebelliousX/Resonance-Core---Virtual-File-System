import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import ffmpeg_broker


class FakeBrokerClient:
    """Protocol-level concurrency test using the broker's stream routing."""
    def test_concurrent_streams_are_independent(self):
        client = object.__new__(ffmpeg_broker.FFmpegBrokerClient)
        client._streams = {}
        client._lock = threading.Lock()
        a = ffmpeg_broker.BrokerStream(client, "a", "audio")
        b = ffmpeg_broker.BrokerStream(client, "b", "video")
        client._streams = {"a": a, "b": b}

        def deliver():
            for event in (
                {"event": "data", "id": "b", "data": b"video".hex()},
                {"event": "data", "id": "a", "data": b"audio".hex()},
                {"event": "ended", "id": "b"},
                {"event": "ended", "id": "a"},
            ):
                stream = client._streams.get(event["id"])
                if event["event"] == "data":
                    stream._push(bytes.fromhex(event["data"]))
                else:
                    stream._end()

        threading.Thread(target=deliver).start()
        self.assertEqual(a.read(), b"audio")
        self.assertEqual(b.read(), b"video")
        self.assertEqual(a.read(), b"")
        self.assertEqual(b.read(), b"")


class FramingTests(unittest.TestCase):
    def test_message_size_guard(self):
        class Socket:
            def sendall(self, value):
                self.value = value
        with self.assertRaises(ValueError):
            ffmpeg_broker._send(Socket(), {"data": "x" * (ffmpeg_broker._MAX_MESSAGE + 1)})


if __name__ == "__main__":
    unittest.main()
