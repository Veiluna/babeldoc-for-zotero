"""封存回归：表格表头分类契约（0.7.0 起不参与生产翻译）。"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "addon" / "content" / "python" / "archive" / "ztf_table_headers.py"

SPEC = importlib.util.spec_from_file_location("ztf_table_headers", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
headers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(headers)


def box(x, y, x2, y2):
    return {"x": x, "y": y, "x2": x2, "y2": y2}


def curve(x, y, x2, y2):
    return {"box": box(x, y, x2, y2)}


def paragraph(text, x, y, x2, y2, label="fallback_line", debug=None):
    return {
        "unicode": text,
        "box": box(x, y, x2, y2),
        "layout_label": label,
        "debug_id": debug or f"id-{text}-{x}-{y}",
        "vertical": False,
    }


def grid_page(table_box, rows, columns):
    """Rows hold (text, first_column, last_column) tuples, top to bottom."""
    left, bottom, right, top = (
        table_box["x"], table_box["y"], table_box["x2"], table_box["y2"]
    )
    count = len(columns) - 1
    width = (right - left) / count
    xs = [left + index * width for index in range(count + 1)]
    ys = [top]
    row_height = (top - bottom) / len(rows)
    for index in range(len(rows)):
        ys.append(top - (index + 1) * row_height)
    curves = []
    for x in xs:
        curves.append(curve(x, bottom, x, top))
    for y in ys:
        curves.append(curve(left, y, right, y))
    paragraphs = []
    for index, row in enumerate(rows):
        for text, first_column, last_column in row:
            x = xs[first_column]
            x2 = xs[last_column]
            paragraphs.append(
                paragraph(text, x + 1, ys[index + 1] + 1, x2 - 1, ys[index] - 1)
            )
    return paragraphs, curves


def analyze(table_box, rows, columns, caption=None, caption_box=None):
    paragraphs, curves = grid_page(table_box, rows, columns)
    if caption is not None:
        paragraphs.append(
            paragraph(
                caption,
                caption_box["x"],
                caption_box["y"],
                caption_box["x2"],
                caption_box["y2"],
                label="table_caption",
            )
        )
    page = {
        "page_layout": [
            {"id": 1, "class_name": "table", "box": table_box},
        ],
        "pdf_paragraph": paragraphs,
        "pdf_curve": curves,
    }
    return headers.analyze_document({"page": [page]})


class TableHeaderRulesTest(unittest.TestCase):

    def test_generic_labels_do_not_include_names_or_values(self):
        for label in ("Method", "Precision", "Recall", "F1", "Params", "FPS",
                      "Detection Performance", "Real-time", "Precision (%)",
                      "F1 (%)", "Params (M)", "AP@0.5", "Top-1"):
            assert headers._is_generic_label(label), label
        for value in ("ChangeMamba", "SECOND", "Swin", "0.912", "YES", "NO", "33M"):
            assert not headers._is_generic_label(value), value


    def test_multi_level_headers_translate_but_methods_and_numbers_do_not(self):
        table = {"x": 0, "y": 0, "x2": 200, "y2": 120}
        rows = [
            [("Detection Performance", 1, 4), ("Efficiency", 4, 5)],
            [("Method", 0, 1), ("Precision", 1, 2), ("Recall", 2, 3),
             ("F1", 3, 4), ("FPS", 4, 5)],
            [("ChangeMamba", 0, 1), ("0.912", 1, 2), ("0.887", 2, 3),
             ("0.899", 3, 4), ("51", 4, 5)],
            [("SECOND", 0, 1), ("0.874", 1, 2), ("0.851", 2, 3),
             ("0.862", 3, 4), ("87", 4, 5)],
        ]
        plans = analyze(table, rows, [0, 1, 2, 3, 4, 5])
        assert len(plans) == 1
        plan = plans[0]
        assert plan["status"] == "clear", plan
        header_texts = {header["text"] for header in plan["headers"]}
        assert header_texts == {
            "Detection Performance", "Efficiency", "Method", "Precision",
            "Recall", "F1", "FPS",
        }
        body_texts = {body["text"] for body in plan["body"]}
        assert {"ChangeMamba", "SECOND", "0.912", "0.887", "51", "87"} <= body_texts
        assert not (header_texts & {"ChangeMamba", "SECOND", "0.912", "YES"})


    def test_left_row_headers_translate_while_top_method_names_stay(self):
        table = {"x": 0, "y": 0, "x2": 200, "y2": 100}
        rows = [
            [("Method", 0, 1), ("ChangeMamba", 1, 2), ("SECOND", 2, 3),
             ("Swin", 3, 4)],
            [("Precision", 0, 1), ("0.91", 1, 2), ("0.87", 2, 3), ("0.85", 3, 4)],
            [("Recall", 0, 1), ("0.88", 1, 2), ("0.85", 2, 3), ("0.83", 3, 4)],
            [("F1", 0, 1), ("0.89", 1, 2), ("0.86", 2, 3), ("0.84", 3, 4)],
        ]
        plans = analyze(table, rows, [0, 1, 2, 3, 4])
        plan = plans[0]
        assert plan["status"] == "clear", plan
        header_texts = {header["text"] for header in plan["headers"]}
        assert header_texts == {"Method", "Precision", "Recall", "F1"}
        roles = {header["text"]: header["role"] for header in plan["headers"]}
        assert roles["Method"] == "column_header"
        assert roles["Precision"] == "row_header"
        body_texts = {body["text"] for body in plan["body"]}
        assert {"ChangeMamba", "SECOND", "Swin", "0.91"} <= body_texts


    def test_borderless_table_gets_text_based_cell_bounds(self):
        table = {"x": 0, "y": 0, "x2": 200, "y2": 100}
        paragraphs = [
            paragraph("Method", 0, 80, 25, 95),
            paragraph("Precision", 60, 80, 95, 95),
            paragraph("Recall", 120, 80, 155, 95),
            paragraph("ChangeMamba", 0, 50, 50, 65),
            paragraph("0.9", 60, 50, 80, 65),
            paragraph("0.8", 120, 50, 140, 65),
            paragraph("SECOND", 0, 20, 45, 35),
            paragraph("0.8", 60, 20, 80, 35),
            paragraph("0.7", 120, 20, 140, 35),
        ]
        page = {
            "page_layout": [{"id": 1, "class_name": "table", "box": table}],
            "pdf_paragraph": paragraphs,
            "pdf_curve": [],
        }
        plans = headers.analyze_document({"page": [page]})
        assert len(plans) == 1
        plan = plans[0]
        assert plan["status"] == "clear", plan
        assert {header["text"] for header in plan["headers"]} == {
            "Method", "Precision", "Recall",
        }
        for header in plan["headers"]:
            assert header["cell_box"] is not None
            left, bottom, right, top = header["cell_box"]
            assert right > left and top > bottom
        assert {"ChangeMamba", "SECOND", "0.9"} <= {
            body["text"] for body in plan["body"]
        }


    def test_table_without_data_rows_is_preserved_and_uncertain(self):
        table = {"x": 0, "y": 0, "x2": 200, "y2": 100}
        rows = [
            [("Precision", 0, 1), ("Recall", 1, 2)],
            [("F1", 0, 1), ("AP", 1, 2)],
            [("mAP", 0, 1), ("IoU", 1, 2)],
        ]
        plans = analyze(table, rows, [0, 1, 2])
        plan = plans[0]
        assert plan["status"] == "uncertain"
        assert plan["headers"] == []
        assert {body["text"] for body in plan["body"]} == {
            "Precision", "Recall", "F1", "AP", "mAP", "IoU",
        }


    def test_caption_number_is_reported_for_uncertain_tables(self):
        table = {"x": 0, "y": 60, "x2": 200, "y2": 160}
        rows = [
            [("Precision", 0, 1), ("Recall", 1, 2)],
            [("F1", 0, 1), ("AP", 1, 2)],
        ]
        plans = analyze(
            table, rows, [0, 1, 2],
            caption="Table 4: Terms used in this study.",
            caption_box={"x": 0, "y": 165, "x2": 180, "y2": 175},
        )
        plan = plans[0]
        assert plan["status"] == "uncertain"
        assert plan["table"] == "表 4"
        assert plan["table_number"] == "4"



if __name__ == "__main__":
    unittest.main()
