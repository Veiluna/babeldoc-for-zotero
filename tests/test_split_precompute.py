"""T5 源侧预计算：命中、失败回退及进程回收。"""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon/content/python'))
import ztf_split_precompute as precompute
import ztf_split_compose as compose
from test_split_compose import SplitComposeFixture
from run_save_equivalence import compare_pdfs


class PrecomputeTests(SplitComposeFixture, unittest.TestCase):
    def test_hit_and_four_fallbacks(self):
        layout = self.fixture()
        entries = json.loads(Path(str(self.mono)+'.variants.json').read_text())['pages']
        baseline = self.root / 'baseline.pdf'
        compose.compose_split_document(str(self.source), str(self.mono), layout, str(baseline))
        for mode in ('hit', 'crash', 'timeout', 'low_memory', 'fingerprint'):
            with self.subTest(mode=mode):
                events = []
                with patch.object(precompute, 'available_memory', return_value=(0 if mode=='low_memory' else 8*1024**3)), patch.dict(os.environ, {'ZTF_TIMING':'1'}):
                    manager = precompute.start(str(self.source), layout, entries, self.root,
                                               log=events.append)
                    process = manager.process
                    if mode == 'crash':
                        process.kill(); process.wait()
                    if mode == 'timeout':
                        manager.wait_seconds = 0
                        # 确定性超时：替换为与真实worker同样的单子进程，不等待机器速度。
                        manager.stop()
                        import subprocess
                        manager.process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
                        process = manager.process
                    final_layout = layout
                    if mode == 'fingerprint':
                        final_layout = json.loads(json.dumps(layout))
                        final_layout['pages'][0]['paragraphs'] = [{'index':0,'box':[1,1,2,2]}]
                    serial = self.root / ('serial-'+mode+'.pdf')
                    with patch.object(precompute, '_active', None):
                        expected = compose.compose_split_document(str(self.source), str(self.mono), final_layout, str(serial))
                    with patch.object(precompute, '_active', manager):
                        actual = compose.compose_split_document(str(self.source), str(self.mono), final_layout, str(self.output))
                    self.assertEqual(actual, expected)
                    self.assertEqual(compare_pdfs(serial, self.output)['status'], 'PASS')
                    self.assertFalse(manager.directory.exists())
                    if process:
                        self.assertIsNotNone(process.poll())
                    status = [event for event in events if event['event']=='split_precompute_result'][-1]
                    self.assertEqual(status['reason'], {'hit':'hit','crash':'worker_failed','timeout':'timeout',
                        'low_memory':'low_memory','fingerprint':'fingerprint_mismatch'}[mode])
                    self.assertEqual(status['hit'], mode=='hit')

    def _parent_termination(self, completed):
        import subprocess
        import time
        import psutil
        layout = self.fixture()
        entries = json.loads(Path(str(self.mono)+'.variants.json').read_text())['pages']
        payload = self.root/'launch.json'
        payload.write_text(json.dumps({'layout':layout,'entries':entries}), encoding='utf-8')
        pid_path = self.root/'worker.pid'
        script = """import json,sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import ztf_split_precompute as p
p.available_memory=lambda:8*1024**3
root=Path(sys.argv[2]); data=json.loads((root/'launch.json').read_text())
m=p.start(str(root/'source.pdf'),data['layout'],data['entries'],root)
(root/'worker.pid').write_text(str(m.process.pid))
time.sleep(60)
"""
        parent = subprocess.Popen([sys.executable,'-B','-c',script,
            str(Path(precompute.__file__).parent),str(self.root)])
        try:
            deadline = time.monotonic()+10
            while not pid_path.exists() and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue(pid_path.is_file())
            pid = int(pid_path.read_text())
            child = psutil.Process(pid)
            if completed:
                deadline = time.monotonic()+10
                while not (self.root/'.split-precompute/result.pickle').exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue((self.root/'.split-precompute/result.pickle').is_file())
            self.assertIsNone(parent.poll())
            parent.kill(); parent.wait()
            child.wait(timeout=10)
            self.assertFalse((self.root/'.split-precompute').exists())
        finally:
            if parent.poll() is None:
                parent.kill(); parent.wait()

    def test_parent_termination_during_work_removes_files(self):
        self._parent_termination(False)

    def test_parent_termination_after_completion_removes_files(self):
        self._parent_termination(True)


if __name__ == '__main__':
    unittest.main()
