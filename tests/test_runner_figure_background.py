"""正式 runner 的底图接入回归：先验证失败，再检查像素、去重及生命周期。"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pymupdf


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "addon" / "content" / "python" / "ztf_babeldoc_runner.py"
SPEC = importlib.util.spec_from_file_location("ztf_babeldoc_runner_background", RUNNER_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


class RunnerFigureBackgroundTests(unittest.TestCase):
    def test_classification_keeps_source_preview_after_figure_il_removal(self):
        paragraph = types.SimpleNamespace(unicode='Chart label', pdf_paragraph_composition=[])
        page = types.SimpleNamespace(page_number=0, pdf_paragraph=[paragraph])
        document = types.SimpleNamespace(page=[page])
        styles = type('Styles', (), {'process': lambda self, doc: None})
        helper = types.SimpleNamespace(remembered=lambda p: None,
                                       build_plan=lambda p, **kw: object(),
                                       remove_selected_objects=lambda p, plan: setattr(paragraph, 'unicode', ''),
                                       remember=lambda *a, **kw: None)
        state = {}
        with mock.patch.object(sys, 'argv', ['runner', '--no-dual']), \
                mock.patch.dict(os.environ, {runner.SPLIT_DUAL_ENV: '0'}), \
                mock.patch.object(runner, '_figure_background_helper', return_value=helper):
            runner._install_figure_background_hook(styles, state, '')
            styles().process(document)
        self.assertEqual(paragraph.unicode, '')
        original = runner.snapshot_paragraph_state(
            document, state.get('figure_background_source_texts'))
        self.assertEqual(runner.classify_paragraphs(document, original),
                         [{'page': 1, 'index': 0, 'preview': 'Chart label', 'decision': 'kept'}])

    def test_preimported_background_is_shared_with_runner(self):
        sys.path.insert(0, str(RUNNER_PATH.parent))
        import ztf_figure_background as background
        with mock.patch.dict(runner._HELPER_MODULES, {}, clear=True):
            self.assertIs(runner._figure_background_helper(), background)

    def test_top_half_figure_is_injected_once_in_both_modes(self):
        sys.path.insert(0, str(RUNNER_PATH.parent))
        import ztf_figure_background as background
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        source_path, output_path = directory / 'source.pdf', directory / 'mono.pdf'
        region = pymupdf.Rect(40, 50, 180, 140)
        with pymupdf.open() as source:
            source.new_page(width=300, height=800).draw_rect(region, fill=(1, 0, 0))
            source[0].insert_text((60, 90), 'Chart label')
            source.save(source_path)
        for split in ('0', '1'):
            with self.subTest(split=split):
                background.forget_all()
                box = types.SimpleNamespace(x=40, y=660, x2=180, y2=750)
                curve = types.SimpleNamespace(box=box)
                page = types.SimpleNamespace(page_number=0, page_layout=[
                    types.SimpleNamespace(class_name='figure', box=box)],
                    pdf_curve=[curve], pdf_paragraph=[])
                styles = type('Styles', (), {'process': lambda self, doc: None})
                state = {}
                with pymupdf.open() as output:
                    output.new_page(width=300, height=800)
                    output.save(output_path)
                with mock.patch.dict(os.environ, {runner.SPLIT_DUAL_ENV: split}), \
                        mock.patch.object(sys, 'argv', ['runner', '--no-dual', '--files', str(source_path)]), \
                        mock.patch.object(runner, '_figure_background_helper', return_value=background):
                    runner._install_figure_background_hook(styles, state, '')
                    styles().process(types.SimpleNamespace(page=[page]))
                    self.assertEqual(page.pdf_curve, [])
                    self.assertEqual(state['figure_background_plans'][0].figure_boxes,
                                     ((40., 50., 180., 140.),))
                    if split == '1':
                        background.inject_backgrounds(str(output_path), str(source_path),
                                                      state['figure_background_plans'], page_numbers=[0])
                    runner._inject_figure_backgrounds({'result': str(output_path)}, state, '')
                    with pymupdf.open(source_path) as source, pymupdf.open(output_path) as output:
                        self.assertEqual(source[0].get_pixmap(dpi=150, clip=region).samples,
                                         output[0].get_pixmap(dpi=150, clip=region).samples)
                        self.assertEqual(len(output[0].get_text('words')), 2)
                    runner._forget_figure_background(state)

    def _page(self, number: int):
        return types.SimpleNamespace(page_number=number, page_layout=["figure"])

    def test_capture_registers_once_with_split_disabled(self):
        page = self._page(4)
        document = types.SimpleNamespace(page=[page])
        styles = type("Styles", (), {})
        calls = []
        helper = types.SimpleNamespace(
            build_plan=lambda current, **kwargs: calls.append(("build", current)) or "plan",
            remembered=lambda current: None,
            remove_selected_objects=lambda current, plan: calls.append(("remove", current, plan)),
            remember=lambda current, plan, **kwargs: calls.append(("remember", current, plan)),
        )
        styles.process = lambda self, current: calls.append(("styles", current))
        state = {}
        with mock.patch.dict(os.environ, {runner.SPLIT_DUAL_ENV: "0"}), mock.patch.object(
            sys, "argv", ["runner", "--no-dual"]
        ), mock.patch.object(runner, "_figure_background_helper", return_value=helper):
            runner._install_figure_background_hook(styles, state, "key")
            styles().process(document)
        self.assertEqual([item[0] for item in calls], ["styles", "build", "remove", "remember"])
        self.assertEqual(state["figure_background_plans"], {4: "plan"})

    def test_capture_registers_once_with_split_enabled(self):
        page = self._page(2)
        document = types.SimpleNamespace(page=[page])
        styles = type("Styles", (), {})
        calls = []
        helper = types.SimpleNamespace(
            build_plan=lambda current, **kwargs: calls.append("build") or "plan",
            remembered=lambda current: None,
            remove_selected_objects=lambda current, plan: calls.append("remove"),
            remember=lambda current, plan, **kwargs: calls.append("remember"),
        )
        styles.process = lambda self, current: None
        state = {}
        with mock.patch.dict(os.environ, {runner.SPLIT_DUAL_ENV: "1"}), mock.patch.object(
            runner, "_figure_background_helper", return_value=helper
        ):
            runner._install_figure_background_hook(styles, state, "key")
            styles().process(document)
            styles().process(document)
        self.assertEqual(calls, ["build", "remove", "remember"])

    def test_native_dual_keeps_il_and_records_explicit_skip(self):
        page = self._page(0)
        page.pdf_curve = [object()]
        original_curve = page.pdf_curve[0]
        styles = type('Styles', (), {'process': lambda self, doc: None})
        state = {}
        with mock.patch.dict(os.environ, {runner.SPLIT_DUAL_ENV: '0'}), \
                mock.patch.object(sys, 'argv', ['runner']), \
                mock.patch.object(runner, '_figure_background_helper') as helper, \
                mock.patch.object(runner, '_write_job_log') as log:
            runner._install_figure_background_hook(styles, state, '')
            styles().process(types.SimpleNamespace(page=[page]))
            helper.assert_not_called()
        self.assertIs(page.pdf_curve[0], original_curve)
        self.assertEqual(state['figure_background_skipped'], 'native_dual_unsafe')
        self.assertEqual(log.call_args.args[1]['skipped'], 'native_dual_unsafe')

    def test_forget_is_called_for_registered_pages(self):
        pages = [self._page(0), self._page(1)]
        helper = types.SimpleNamespace(forget=mock.Mock(), forget_all=mock.Mock())
        state = {"figure_background_pages": pages, "figure_background_plans": {0: "a", 1: "b"}}
        with mock.patch.object(runner, "_figure_background_helper", return_value=helper):
            runner._forget_figure_background(state)
        self.assertEqual(helper.forget.call_args_list, [mock.call(pages[0]), mock.call(pages[1])])
        self.assertNotIn("figure_background_plans", state)

    def test_lifecycle_forgets_plan_after_translation_error(self):
        sys.path.insert(0, str(RUNNER_PATH.parent))
        import ztf_figure_background as background
        page = self._page(0)
        background.remember(page, background.BackgroundPlan(()))
        state = {'figure_background_pages': [page]}
        with self.assertRaisesRegex(RuntimeError, 'translation failed'):
            with runner._figure_background_lifecycle(state):
                raise RuntimeError('translation failed')
        self.assertIsNone(background.remembered(page))

    def test_selected_pages_preserve_source_numbers(self):
        config = types.SimpleNamespace(
            only_include_translated_page=True,
            should_translate_page=lambda number: number in {2, 4},
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        source = Path(tmp.name) / "runner-background-source.pdf"
        document = pymupdf.open()
        try:
            for _ in range(5):
                document.new_page()
            document.save(source)
        finally:
            document.close()
        self.assertEqual(
            runner._figure_background_source_pages(str(source), config, 2),
            [1, 3],
        )

    def test_selected_output_injects_second_and_fourth_source_pages(self):
        background = runner._figure_background_helper()
        background.forget_all()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        source_path, output_path = directory / 'source.pdf', directory / 'mono.pdf'
        region = pymupdf.Rect(20, 20, 80, 80)
        with pymupdf.open() as source, pymupdf.open() as output:
            for number in range(5):
                page = source.new_page(width=100, height=100)
                page.draw_rect(region, color=None, fill=(number / 5, 0, 0))
            for _ in range(2):
                output.new_page(width=100, height=100)
            source.save(source_path)
            output.save(output_path)
        config = types.SimpleNamespace(only_include_translated_page=True,
                                       should_translate_page=lambda n: n in (2, 4))
        plan = background.BackgroundPlan((tuple(region),), clip_regions=(tuple(region),))
        state = {'figure_background_plans': {1: plan, 3: plan},
                 'figure_background_config': config}
        with mock.patch.object(sys, 'argv', ['runner', '--files', str(source_path)]):
            runner._inject_figure_backgrounds({'result': str(output_path)}, state, '')
        with pymupdf.open(source_path) as source, pymupdf.open(output_path) as output:
            for out_index, src_index in enumerate((1, 3)):
                self.assertEqual(source[src_index].get_pixmap(dpi=150).samples,
                                 output[out_index].get_pixmap(dpi=150).samples)
        background.forget_all()

    def test_failed_injection_does_not_deliver_pdf_with_removed_figures(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        output_path = directory / 'mono.pdf'
        with pymupdf.open() as output:
            output.new_page()
            output.save(output_path)
        helper = types.SimpleNamespace(was_injected=lambda path: False,
                                       inject_backgrounds=mock.Mock(side_effect=OSError('write failed')))
        state = {'result': str(output_path)}
        with mock.patch.object(sys, 'argv', ['runner', '--files', 'source.pdf']), \
                mock.patch.object(runner, '_figure_background_helper', return_value=helper):
            runner._inject_figure_backgrounds(state, {'figure_background_plans': {0: object()}}, '')
        self.assertFalse(state.get('result'))
        self.assertEqual(state['error_kind'], 'figure_background')


if __name__ == "__main__":
    unittest.main()
