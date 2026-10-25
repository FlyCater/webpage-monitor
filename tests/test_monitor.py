import os
import sys
import unittest
sys.path.insert(0, os.path.abspath('scripts'))
from monitor import classify, state_token

class TestMonitor(unittest.TestCase):
    def test_pending(self):
        self.assertEqual(classify('<h2>코믹월드</h2><p>공지 준비중입니다.</p>', '340'), 'pending')
    def test_open(self):
        self.assertEqual(classify('<h1>[동아리 신청] 동아리 전시교류전 신청 안내 / 코믹월드 339 일산</h1>', '339'), 'open')
    def test_no_false_match(self):
        self.assertEqual(classify('<h1>코믹월드 339</h1>', '340'), 'unknown')
    def test_no_navigation_false_positive(self):
        self.assertEqual(classify('<nav>코믹월드 340</nav><h1>다른 페이지</h1>', '340'), 'unknown')
    def test_hmac_not_plain(self):
        token = state_token('340', 'x' * 64)
        self.assertEqual(len(token), 64)
        self.assertNotIn('340', token)

if __name__ == '__main__':
    unittest.main()
