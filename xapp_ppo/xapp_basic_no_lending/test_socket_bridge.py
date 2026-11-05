"""Protocol tests with a fake policy; no PyTorch, GPU, E2 or radio required."""
import unittest
from ppo_socket_server import parse_request, response


class FakePolicy:
    def predict(self, sample, now_ms, *, window_tolerance_ms):
        if now_ms - sample['timestamp_ms'] > 200 or sample['timestamp_ms'] - now_ms > 5:
            raise ValueError('stale/future')
        if abs(sample['window_ms'] - 100) > window_tolerance_ms:
            raise ValueError('window')
        return {'service_prbs': {'yolo': 30, 'live': 48}}


class BridgeTests(unittest.TestCase):
    packet = b'PPO1 9 1000 110 12000 21.5 6.2 22000 19 5.1 39 39\n'

    def test_mapping_and_actual_window(self):
        seq, sample = parse_request(self.packet)
        self.assertEqual(seq, 9)
        self.assertEqual(sample['window_ms'], 110)
        self.assertEqual(sample['ues']['yolo_1']['ul_buffer_bytes'], 12000)
        self.assertEqual(sample['ues']['live_1']['ul_snr_db'], 19)
        self.assertEqual(sample['service_prbs'], {'yolo': 39, 'live': 39})
        self.assertTrue(response(self.packet, FakePolicy(), 5, 1050).startswith(b'9 OK 30 48 '))

    def test_bad_datagrams(self):
        for data in (b'', self.packet + b'extra', self.packet.replace(b'21.5', b'nan'),
                     self.packet.replace(b'PPO1', b'PPO2'), self.packet.replace(b' 9 ', b' 0 ')):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_request(data)

    def test_stale_and_window(self):
        with self.assertRaises(ValueError):
            response(self.packet, FakePolicy(), 5, 1201)
        with self.assertRaises(ValueError):
            response(self.packet.replace(b' 110 ', b' 150 '), FakePolicy(), 5, 1050)

    def test_minimum_rejection_not_clamping(self):
        with self.assertRaises(ValueError):
            response(self.packet, FakePolicy(), 31, 1050)


if __name__ == '__main__':
    unittest.main()
