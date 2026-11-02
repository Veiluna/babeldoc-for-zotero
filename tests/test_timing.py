"""T0: opt-in timing must be transparent and cannot expose document content."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'addon/content/python'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pymupdf as fitz
import ztf_split_compose as compose
import ztf_timing as timing
from test_split_compose import SplitComposeFixture


class TimingTests(SplitComposeFixture, unittest.TestCase):
    def tearDown(self):
        timing.uninstall()

    def test_off_does_not_replace_functions(self):
        original = compose.analyze_source
        self.assertIsNone(timing.install(lambda event: self.fail(event),
                                         directory=self.root, environ={}))
        self.assertIs(compose.analyze_source, original)

    def test_marker_and_exact_environment_switch(self):
        self.assertFalse(timing.enabled(self.root, {'ZTF_TIMING': 'true'}))
        self.assertTrue(timing.enabled(self.root, {'ZTF_TIMING': '1'}))
        Path(self.root, 'ztf_timing.enabled').touch()
        self.assertTrue(timing.enabled(self.root, {}))

    def test_wrapper_preserves_arguments_result_exception_identity_and_order(self):
        events, calls = [], []
        result, failure, argument = object(), RuntimeError('PRIVATE正文sk-secret'), object()
        def target(*args, **kwargs):
            calls.append((args, kwargs))
            if kwargs.get('fail'):
                raise failure
            return result
        session = timing.install(events.append, environ={'ZTF_TIMING': '1'})
        wrapped = session.wrap(target, 'fixture.target[test]')
        self.assertIs(wrapped(argument, x=argument), result)
        with self.assertRaises(RuntimeError) as caught:
            wrapped(argument, fail=True)
        self.assertIs(caught.exception, failure)
        self.assertEqual(calls, [((argument,), {'x': argument}), ((argument,), {'fail': True})])
        self.assertIs(wrapped.__wrapped__, target)
        session.finish()
        summary = next(e for e in events if e['event'] == 'timing_summary')
        item = summary['functions']['fixture.target[test]']
        self.assertEqual(item['calls'], 2)
        self.assertGreaterEqual(item['seconds'], item['max'])
        self.assertIn('stages', summary)
        self.assertEqual(summary['compose_pages'], [])
        self.assertNotIn('PRIVATE', json.dumps(events))
        self.assertNotIn('sk-secret', json.dumps(events))

    def test_observation_failure_does_not_replace_original_exception(self):
        failure = ValueError('PRIVATE')
        session = timing.install(lambda event: (_ for _ in ()).throw(OSError()),
                                 environ={'ZTF_TIMING': '1'})
        def target():
            raise failure
        with self.assertRaises(ValueError) as caught:
            session.wrap(target, 'fixture.target[test]')()
        self.assertIs(caught.exception, failure)
        session.finish()

    def test_staticmethod_descriptor_and_uninstall_are_transparent(self):
        class Writer:
            @staticmethod
            def save(value):
                return value
        original = vars(Writer)['save']
        session = timing.install(lambda event: None, environ={'ZTF_TIMING': '1'})
        session.patch(Writer, 'save', 'fixture.Writer')
        value = object()
        self.assertIs(Writer.save(value), value)
        self.assertIs(Writer().save(value), value)
        timing.uninstall()
        self.assertIs(vars(Writer)['save'], original)

    def test_type0_font_objects_are_deduplicated_across_pages(self):
        reads = []
        class FontDocument:
            def __len__(self):
                return 2
            def get_page_fonts(self, number, full=False):
                self.assert_full = full
                return [(1, 'ttf', 'Type0', 'ABCDEF+Sample', 'F1', 'Identity-H', 0),
                        (2, 'ttf', 'Type0', 'ABCDEF+Sample', 'F2', 'Identity-H', 0)]
            def xref_get_key(self, xref, key):
                reads.append((xref, key))
                if key == 'DescendantFonts':
                    return ('array', f'[{xref + 2} 0 R]')
                if key == 'FontDescriptor/FontFile2':
                    return ('xref', f'{xref + 2} 0 R')
                return ('null', 'null')
            def xref_stream_raw(self, xref):
                return b'a' * xref
        document = FontDocument()
        fonts = timing.embedded_fonts(document)
        self.assertTrue(document.assert_full)
        self.assertEqual(len(fonts), 2)
        self.assertEqual([font['font_file_bytes'] for font in fonts], [5, 6])
        self.assertTrue(all(font['subset'] and font['same_name_count'] == 2 for font in fonts))
        self.assertEqual(reads.count((1, 'DescendantFonts')), 1)

    def test_memory_unavailable_and_windows_fallback(self):
        with patch.dict(sys.modules, {'psutil': None}):
            with patch.object(timing.sys, 'platform', 'unsupported'):
                self.assertEqual(timing.peak_memory(), 'unavailable')
            if sys.platform == 'win32':
                self.assertGreater(timing.peak_memory(), 0)

    def test_runner_lifecycle_instruments_late_loaded_helper_and_redacts(self):
        import ztf_babeldoc_runner as runner
        module = runner._HELPER_MODULES.pop('ztf_links', None)
        records = []
        state = {'result': ''}
        request = {'job_log_path': str(self.root / 'timing.jsonl')}
        try:
            with (patch.dict(timing.os.environ, {'ZTF_TIMING': '1'}),
                  runner._timing_lifecycle(state, request, 'ABCDEF+Sample') as session):
                helper = runner._load_helper('ztf_links')
                self.assertIs(helper.restore_links._ztf_timing_session, session)
                session.wrap(lambda: None, 'fixture.target[save]')()
            records = [json.loads(line) for line in Path(request['job_log_path']).read_text(
                encoding='utf-8').splitlines()]
            self.assertEqual([e['event'] for e in records], ['timing_artifacts', 'timing_summary'])
            self.assertIsNone(timing._active)
            self.assertFalse(hasattr(helper.restore_links, '_ztf_timing_session'))
        finally:
            runner._HELPER_MODULES.pop('ztf_links', None)
            if module is not None:
                runner._HELPER_MODULES['ztf_links'] = module


class TimingComposeTests(SplitComposeFixture, unittest.TestCase):
    def tearDown(self):
        timing.uninstall()

    def snapshot(self, path):
        with fitz.open(path) as doc:
            return (len(doc), [p.get_text('rawdict') for p in doc], self.layout(doc),
                    [p.get_links() for p in doc])

    def test_enabled_and_disabled_pdf_semantics_match_including_safety_net(self):
        layout = self.fixture(2)
        mapping_path = Path(str(self.mono) + '.variants.json')
        mapping = json.loads(mapping_path.read_text())
        mapping['pages'][1]['error'] = 'source_snapshot_missing'
        mapping_path.write_text(json.dumps(mapping), encoding='utf-8')
        with fitz.open(self.source) as doc:
            doc[0].insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(25, 105, 80, 125),
                                'uri': 'https://example.org'})
            doc.saveIncr()
        sidecar_before = mapping_path.read_bytes()
        self.run_compose(layout)
        before, logs = self.snapshot(self.output), list(self.events)
        events = []
        session = timing.install(events.append, environ={'ZTF_TIMING': '1'})
        self.run_compose(layout)
        output_before_artifacts = self.output.read_bytes()
        session.finish({'split': str(self.output), 'mono': str(self.mono),
                        'variants': str(self.root / 'variants.pdf')})
        self.assertEqual(self.output.read_bytes(), output_before_artifacts)
        self.assertEqual(self.snapshot(self.output), before)
        self.assertEqual(self.events, logs)
        self.assertEqual(mapping_path.read_bytes(), sidecar_before)
        summary = next(e for e in events if e['event'] == 'timing_summary')
        self.assertEqual([p['page'] for p in summary['compose_pages']], [0, 1])
        self.assertTrue(all(p['placements'] > 0 and p['regions'] > 0
                            for p in summary['compose_pages']))
        self.assertIn('ztf_split_compose._filter[analyze_source]', summary['functions'])
        for source in ('analyze_source', '_layer', '_content_boxes', '_text_boxes'):
            self.assertIn(f'ztf_split_compose._filter[{source}]', summary['functions'])
        # collect=True 的只读 _filter 不调用图形处理器，不能伪造不存在的调用。
        self.assertIn('ztf_split_compose._GraphicObjects.run[_layer]', summary['functions'])
        artifacts = next(e for e in events if e['event'] == 'timing_artifacts')
        self.assertEqual(artifacts['pdf_bytes']['split'], self.output.stat().st_size)
        self.assertIn('fonts', artifacts)
        self.assertIn('peak_process_memory_bytes', artifacts)
        self.assertNotIn('LEFTA', json.dumps(events))


if __name__ == '__main__':
    unittest.main()
