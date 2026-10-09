"""Metrics transport and connection-generation regressions (no radio backend)."""
import pathlib
import socket
import struct
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'apps/dashboard'))
import server


def frame(opcode, payload=b'', fin=True):
    header = bytes([(0x80 if fin else 0) | opcode])
    return header + (bytes([len(payload)]) if len(payload) < 126 else b'\x7e' + struct.pack('!H', len(payload))) + payload


def receive(sock, size):
    data = b''
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise EOFError()
        data += part
    return data


def client_frame(sock):
    first, length = receive(sock, 2)
    assert length & 128
    length &= 127
    if length == 126:
        length = struct.unpack('!H', receive(sock, 2))[0]
    mask = receive(sock, 4)
    payload = receive(sock, length)
    return first & 15, bytes(c ^ mask[i % 4] for i, c in enumerate(payload))


class RecoveryTests(unittest.TestCase):
    def test_freshness_uses_scheduler_clock_not_heartbeats_or_ack(self):
        with mock.patch('server.time.monotonic', return_value=10) as clock:
            store = server.StatusStore()
            store.update_gnb_metrics({'ue_list': [{'pci': 1, 'rnti': 1}]})
            clock.return_value = 15
            self.assertFalse(store.snapshot()['ran']['ue_list_stale'])
            clock.return_value = 15.001
            store.update_gnb_metrics({'status': 'ok'})
            self.assertTrue(store.snapshot()['ran']['ue_list_stale'])
            self.assertTrue(store.snapshot()['ran']['ue_list_current_connection'])

    def make_client(self):
        local, self.peer = socket.socketpair()
        self.peer.settimeout(2)
        self.stop = threading.Event()
        # Use the real framing reader with a fast idle timer; no mocked reads.
        with mock.patch.object(server.socket, 'create_connection', return_value=local), \
             mock.patch.object(server.WebSocketClient, '_handshake'):
            self.client = server.WebSocketClient('localhost', 1)
        # This method activates established-session polling and heartbeats.
        self.client.monitor_connection(self.stop, idle_seconds=0.05, pong_seconds=0.12, poll_seconds=0.01)
        self.addCleanup(self.client.close)
        self.addCleanup(self.peer.close)
        return self.client

    def read_async(self):
        results = []
        def read():
            try:
                results.append(self.client.recv_message())
            except Exception as exc:
                results.append(exc)
        worker = threading.Thread(target=read)
        worker.start()
        self.addCleanup(lambda: (self.stop.set(), worker.join(1)))
        return worker, results

    def test_idle_ping_keeps_subscription_alive(self):
        self.make_client()
        worker, result = self.read_async()
        for _ in range(3):
            opcode, token = client_frame(self.peer)
            self.assertEqual(opcode, server.WS_OP_PING)
            self.peer.sendall(frame(server.WS_OP_PONG, token))
        self.peer.sendall(frame(server.WS_OP_TEXT, b'{"cells":[]}'))
        worker.join(1)
        self.assertEqual(result, ['{"cells":[]}'])

    def test_missing_or_wrong_pong_disconnects(self):
        self.make_client()
        worker, result = self.read_async()
        opcode, token = client_frame(self.peer)
        self.peer.sendall(frame(server.WS_OP_PONG, b'wrong'))
        # Other notifications cannot satisfy the outstanding Ping.
        worker.join(1)
        self.assertIsInstance(result[0], server.WebSocketError)
        self.assertIn('heartbeat', str(result[0]))

    def test_partial_headers_payloads_and_fragmentation_survive_idle(self):
        self.make_client()
        worker, result = self.read_async()
        data = frame(server.WS_OP_TEXT, b'x' * 130, fin=False)
        for piece in (data[:1], data[1:3], data[3:20], data[20:]):
            self.peer.sendall(piece)
            # A Pong follows the incomplete frame, so complete it within the
            # Pong deadline while exercising multiple socket poll timeouts.
            time.sleep(0.025)
        opcode, token = client_frame(self.peer)
        self.assertEqual(opcode, server.WS_OP_PING)
        self.peer.sendall(frame(server.WS_OP_PONG, token))
        self.peer.sendall(frame(server.WS_OP_PING, b'server'))
        self.assertEqual(client_frame(self.peer), (server.WS_OP_PONG, b'server'))
        time.sleep(0.02)
        self.peer.sendall(frame(server.WS_OP_CONTINUATION, b'end'))
        worker.join(1)
        self.assertEqual(result, ['x' * 130 + 'end'])

    def test_peer_close_and_idle_shutdown(self):
        self.make_client()
        self.peer.sendall(frame(server.WS_OP_CLOSE))
        self.assertIsNone(self.client.recv_message())
        worker, result = self.read_async()
        self.stop.set()
        worker.join(0.5)
        self.assertFalse(worker.is_alive())

    def test_invalid_control_frame_disconnects(self):
        self.make_client()
        self.peer.sendall(frame(server.WS_OP_PING, b'bad', fin=False))
        with self.assertRaises(server.WebSocketError):
            self.client.recv_message()

    def test_reconnect_ack_does_not_revive_cached_rows(self):
        store = server.StatusStore()
        row = {'pci': 1, 'rnti': 17921}
        for name in ('gnb0', 'gnb1'):
            store.update_gnb_metrics({'ue_list': [row]}, name)
        store.set_gnb_metrics_state('disconnected', 'lost', 'gnb0')
        store.set_gnb_metrics_state('connecting', gnb_id='gnb0')
        store.set_gnb_metrics_state('subscribed', gnb_id='gnb0')
        store.update_gnb_metrics({'status': 'ok'}, 'gnb0')
        snap = store.snapshot()
        self.assertTrue(snap['ran']['ue_list_stale'])
        self.assertFalse(snap['ran']['ue_list_current_connection'])
        self.assertEqual(snap['ran']['ue_list'], [row])
        self.assertFalse(snap['ran_gnbs']['gnb1']['ue_list_stale'])
        # Fresh empty report, then a returning UE with a new identity.
        store.update_gnb_metrics({'cells': [{'pci': 1}]}, 'gnb0')
        self.assertEqual(store.snapshot()['ran']['ue_list'], [])
        for rnti in (17922, 17921):
            store.update_gnb_metrics({'ue_list': [{'pci': 2, 'rnti': rnti}]}, 'gnb0')
            current = store.snapshot()['ran']
            self.assertTrue(current['ue_list_current_connection'])
            self.assertFalse(current['ue_list_stale'])
            self.assertEqual(current['ue_list'], [{'pci': 2, 'rnti': rnti}])


if __name__ == '__main__':
    unittest.main()
