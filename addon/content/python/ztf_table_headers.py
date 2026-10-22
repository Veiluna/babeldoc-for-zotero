"""Classify table cells as translatable headers or table-body content.

BabelDOC 0.6.4 has no table model: a table is one 'table' layout box and its
cells are ordinary 'fallback_line' paragraphs.  The layout box alone cannot
tell a column header from a data value, and PyMuPDF's find_tables is not
reliable enough to trust (Chen's table pages either produce no table under
lines_strict or the whole page under text).  This module therefore combines
the BabelDOC table boundary, the cell geometry and the other cells in the same
table:

* top row bands are column-header candidates;
* the leftmost column is a row-header candidate;
* a cell is only marked as a table header when its own wording has general
  table meaning (Method, Precision, Recall, ...), never when it is a method
  name, dataset, abbreviation, number, symbol or YES/NO;
* table-body cells are not decided here: they are returned as body material and
  the runner applies the table content rules (identifiers/numbers/URLs/code are
  kept, other ordinary text is sent for translation);
* when header and body roles cannot be told apart (no data row, generic labels,
  ...) the table is reported as uncertain: no headers are marked and every cell
  goes to the body list, so it follows the same content rules. 0.7.0 起不再是
  “整表保留原文”（见 ADR 0006）。

The module has no BabelDOC imports and accepts either attribute objects (the
real BabelDOC IR) or dictionaries (tests and debug JSON).
"""

from __future__ import annotations

import re
import statistics

_PLACEHOLDER_RE = re.compile(
    r"<\s*style\b[^>]*>|<\s*/\s*style\s*>|\{\s*v\s*\d+\s*\}|"
    r"%[sdif]|\[\[[^\]\r\n]+\]\]|%%[^%\r\n]+%%",
    re.I,
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9@./%+\-:]+")
_TABLE_NUMBER_RE = re.compile(
    r"(?i)\b(?:table|tab\.?)\s*([ivxlcdm]+|\d+[a-z]?|[a-z])\b"
)
_NOTE_START_RE = re.compile(
    r"^\s*(?:\*|†|‡|§|note\b|notes\b|table\b|tab\.|fig\.|figure\b)",
    re.I,
)
_SENTENCE_RE = re.compile(r"[.!?。！？]\s*$")

# General table semantics: these labels may be translated.  Keys are compared
# after lower-casing and removing punctuation (f1-score -> f1score).
_GENERIC_KEYS = {
    "method", "methods", "approach", "approaches", "model", "models",
    "network", "networks", "backbone", "backbones", "dataset", "datasets",
    "data", "type", "types", "setting", "settings", "config", "configuration",
    "parameter", "parameters", "param", "params", "metric", "metrics",
    "score", "scores", "result", "results", "performance", "efficiency",
    "speed", "size", "input", "inputs", "output", "outputs", "feature",
    "features", "resolution", "modality", "task", "tasks", "stage", "stages",
    "block", "blocks", "module", "modules", "component", "components",
    "layer", "layers", "head", "heads", "encoder", "decoder", "branch",
    "branches", "training", "train", "testing", "test", "validation", "val",
    "inference", "epoch", "epochs", "iteration", "iterations", "batch",
    "learning", "rate", "lr", "loss", "losses", "accuracy", "acc",
    "precision", "recall", "real", "time", "realtime", "category",
    "categories", "class", "classes", "label", "labels", "value", "values",
    "name", "names", "detection", "segmentation", "classification",
    "tracking", "recognition", "matching", "localization", "localisation",
    "regression", "fusion", "attention", "convolution", "transformer",
    "total", "average", "avg", "median", "mean", "sum", "count", "number",
    "index", "rank", "order", "dimension", "dimensions", "length", "width",
    "height", "depth", "channel", "channels", "frame", "frames", "sample",
    "samples", "scene", "scenes", "object", "objects", "region", "regions",
    "point", "points", "voxel", "voxels", "image", "images", "range",
    "threshold", "ratio", "rate", "percentage", "percent", "gain", "drop",
    "improvement", "reduction", "baseline", "proposed", "ours", "variant",
    "variants", "ablation", "comparison", "description", "notation",
    "symbol", "meaning", "unit", "units", "remark", "remarks", "source",
    "sources", "target", "targets", "domain", "domains", "split", "splits",
    "trainval", "calibration", "sensitivity", "specificity", "auc", "roc",
    "psnr", "ssim", "bleu", "rouge", "meteor", "perplexity", "latency",
    "memory", "throughput", "bandwidth", "cost", "price", "year", "date",
    "version", "status", "state", "methodology", "protocol",
    "benchmark", "benchmarks", "evaluation", "experiment", "experiments",
    "table", "tables", "figure", "figures", "column", "columns", "row",
    "rows", "entry", "entries", "item", "items", "term", "terms",
    "abbreviation", "abbreviations", "definition", "definitions", "notes",
    "top",
}
_METRIC_KEYS = {
    "ap", "ap50", "ap75", "ap5095", "map", "map50", "map75",
    "miou", "iou", "f1", "f1score", "f1macro", "f1micro", "top1", "top5",
    "precision", "recall", "accuracy", "acc", "auc", "roc", "psnr", "ssim",
    "bleu", "rouge", "meteor", "perplexity", "params", "param", "flops",
    "gflops", "mparams", "fps", "latency", "memory", "speed", "size",
}
_CONNECTOR_KEYS = {
    "of", "on", "for", "and", "or", "with", "without", "in", "at", "by",
    "per", "to", "from", "the", "a", "an", "vs", "versus", "under", "over",
    "across", "between", "all", "each", "based",
    # Unit suffixes in table headers: "Params (M)", "F1 (%)".
    "m", "k", "g", "b", "ms", "s", "hz", "kb", "mb", "gb", "tb",
}
_KNOWN_PROPER_NAMES = {
    "changemamba", "mamba", "swin", "swinir", "second", "pointnet",
    "pointnet++", "pointnet2", "voxelnet", "centerpoint", "pvrcnn",
    "pointpillars", "randlanet", "randla-net", "kpconv", "cylinder3d",
    "minkowskinet", "spvcnn", "rangenet", "squeezeseg", "polarnet",
    "sparseconv", "detr", "yolo", "yolov3", "yolov5", "yolov8", "fasterrcnn",
    "maskrcnn", "resnet", "resnet50", "resnet101", "vgg", "vgg16",
    "efficientnet", "mobilenet", "bert", "roberta", "vit", "deit",
    "segformer", "deeplab", "unet", "fcn", "pspnet", "hrnet", "dla",
    "kitti", "nuscenes", "waymo", "cityscapes", "coco", "imagenet",
    "voc", "ade20k", "scannet", "s3dis", "semantickitti", "carla",
    "lyft", "argoverse", "a2d2", "bdd100k", "mapillary",
    "cifar", "mnist", "celeba", "lvis", "visualgenome", "refcoco",
    "gpt", "gpt4", "clip", "sam", "llama", "gemini", "paddle",
}
_YES_NO = {"yes", "no", "true", "false", "y", "n"}


def _raw(value, key, default=None):
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _seq(value):
    return list(value) if value else []


def _box(value):
    box = value if isinstance(value, (tuple, list)) else _raw(value, "box", value)
    if box is None:
        return None
    if isinstance(box, (tuple, list)) and len(box) >= 4:
        try:
            return (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        except (TypeError, ValueError):
            return None
    try:
        return (
            float(_raw(box, "x")),
            float(_raw(box, "y")),
            float(_raw(box, "x2")),
            float(_raw(box, "y2")),
        )
    except (TypeError, ValueError):
        return None


def _box_width(box):
    return max(0.0, box[2] - box[0])


def _box_height(box):
    return max(0.0, box[3] - box[1])


def _center_x(box):
    return (box[0] + box[2]) / 2


def _center_y(box):
    return (box[1] + box[3]) / 2


def _center_inside(box, container, tolerance=0.5):
    return (
        container[0] - tolerance <= _center_x(box) <= container[2] + tolerance
        and container[1] - tolerance <= _center_y(box) <= container[3] + tolerance
    )


def _vertical_overlap(left, right):
    return max(0.0, min(left[3], right[3]) - max(left[1], right[1]))


def _horizontal_overlap(left, right):
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0]))


def _plain_text(text):
    return " ".join(_PLACEHOLDER_RE.sub(" ", str(text or "")).split())


def _word_tokens(text):
    tokens = [
        token for token in _WORD_SPLIT_RE.split(_plain_text(text)) if token
    ]
    # Punctuation-only runs ("(%)", ":", "@") are units, not label words.
    return [token for token in tokens if re.search(r"[A-Za-z0-9]", token)]


def _metric_key(token):
    return re.sub(r"[^a-z0-9]", "", str(token).casefold())


def _paragraph_text(paragraph):
    return str(_raw(paragraph, "unicode", "") or "")


def _paragraph_usable(paragraph):
    if _raw(paragraph, "debug_id") is None:
        return False
    if _raw(paragraph, "vertical", False):
        return False
    return bool(_paragraph_text(paragraph).strip()) and _box(paragraph) is not None


def _is_cjk(text):
    return bool(_CJK_RE.search(text or ""))


def _is_data_value(text):
    plain = _plain_text(text)
    if not plain or _is_cjk(plain):
        return False
    compact = re.sub(r"\s+", "", plain).casefold()
    if compact in _YES_NO:
        return True
    if re.fullmatch(r"[\u2713\u2717\u2714\u2718\u25cf\u25cb\u2013\u2014\-+*/=<>]+", plain):
        return True
    if re.fullmatch(r"[+\-\u2212]?\d+(?:[.,]\d+)*(?:\s*[%％])?", plain):
        return True
    if re.fullmatch(
        r"[+\-\u2212]?\d+(?:\.\d+)?\s*(?:[kKmMgGbB]|ms|s|fps|hz|%|％|x|×)?",
        plain,
    ):
        return True
    if re.fullmatch(r"[<>=≈≤≥~±+\u2212\-]?\s*\d+(?:\.\d+)?", plain):
        return True
    if not re.search(r"[A-Za-z]", plain):
        return True
    if len(plain.split()) <= 1 and re.search(r"\d", plain):
        return True
    return False


def _is_metric_label(text):
    tokens = _word_tokens(text)
    if not tokens:
        return False
    keys = [_metric_key(token) for token in tokens]
    if len(keys) == 1 and (
        keys[0] in _METRIC_KEYS
        or re.fullmatch(r"(?:ap|map|f1|top|acc|auc)\d*", keys[0])
    ):
        return True
    if len(keys) >= 2 and keys[0] == "top" and all(
        key.isdigit() for key in keys[1:]
    ):
        return True
    return bool(keys and all(
        key in _METRIC_KEYS or key in _CONNECTOR_KEYS for key in keys
    ) and any(key in _METRIC_KEYS for key in keys))


def _is_generic_label(text):
    """A label with general table meaning rather than a name/datum."""
    plain = _plain_text(text)
    if not plain or _is_cjk(plain):
        return False
    if _is_metric_label(plain):
        return True
    if _is_data_value(plain):
        return False
    tokens = _word_tokens(plain)
    if not tokens:
        return False
    keys = [_metric_key(token) for token in tokens]
    if len(keys) == 1 and keys[0] in _METRIC_KEYS:
        return True
    if any(key in _METRIC_KEYS for key in keys) and all(
        key in _METRIC_KEYS or key in _GENERIC_KEYS or key in _CONNECTOR_KEYS
        for key in keys
    ):
        return True
    if all(
        key in _GENERIC_KEYS or key in _CONNECTOR_KEYS for key in keys
    ) and any(key in _GENERIC_KEYS for key in keys):
        return True
    # 表头带中的普通英文短语（如 "Published Flows"）即使不在固定词表，
    # 也是可翻译的列标题；专名、缩写与数据已由 _is_name_like 排除。
    return (
        len(plain) <= 40
        and not _is_name_like(plain)
        and any(re.fullmatch(r"[A-Za-z]{3,}", token) for token in tokens)
    )


def _is_name_like(text):
    plain = _plain_text(text)
    if not plain or _is_cjk(plain) or _is_data_value(plain):
        return False
    compact = plain.casefold()
    if compact in _KNOWN_PROPER_NAMES:
        return True
    token = plain.strip()
    if re.fullmatch(r"[A-Z]{2,10}(?:[-/][A-Z0-9]+)*", token):
        return True
    if re.search(r"[a-z][A-Z]", token):
        return True
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+", token):
        return True
    if re.search(r"\d", token) and re.search(r"[A-Za-z]", token):
        return True
    # A short dataset/model token with mixed case and no sentence punctuation.
    tokens = plain.split()
    if (
        len(tokens) == 1
        and 2 <= len(token) <= 24
        and not _SENTENCE_RE.search(token)
        and not re.search(r"[a-z]{4,}", token)
    ):
        return True
    return False


def _is_sentence_like(text):
    plain = _plain_text(text)
    if len(plain.split()) < 6:
        return False
    return bool(_SENTENCE_RE.search(plain)) or len(plain.split()) >= 10


class _Cell:
    __slots__ = (
        "paragraph", "box", "text", "row", "cell_left", "cell_right",
        "left_column", "role",
    )

    def __init__(self, paragraph, box, text):
        self.paragraph = paragraph
        self.box = box
        self.text = text
        self.row = -1
        self.cell_left = box[0]
        self.cell_right = box[2]
        self.left_column = False
        self.role = "body"


class _Row:
    __slots__ = ("cells", "box", "center")

    def __init__(self, cells):
        self.cells = list(cells)
        self.box = None
        self.center = 0.0
        self._finalize()

    def add(self, cell):
        self.cells.append(cell)
        self._finalize()

    def _finalize(self):
        boxes = [cell.box for cell in self.cells if cell.box is not None]
        if not boxes:
            self.box = (0.0, 0.0, 0.0, 0.0)
            self.center = 0.0
            return
        self.box = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        self.center = statistics.median(_center_y(box) for box in boxes)


def _group_rows(cells):
    if not cells:
        return []
    heights = sorted(_box_height(cell.box) for cell in cells)
    median_height = heights[len(heights) // 2]
    tolerance = max(2.0, median_height * 0.55)
    rows: list[_Row] = []
    for cell in sorted(cells, key=lambda item: (-_center_y(item.box), item.box[0])):
        target = None
        for row in rows:
            overlap = _vertical_overlap(cell.box, row.box)
            minimum = min(_box_height(cell.box), _box_height(row.box))
            if (
                (minimum > 0 and overlap >= minimum * 0.45)
                or abs(_center_y(cell.box) - row.center) <= tolerance
            ):
                target = row
                break
        if target is None:
            rows.append(_Row([cell]))
        else:
            target.add(cell)
    rows.sort(key=lambda row: -row.center)
    for index, row in enumerate(rows):
        for cell in row.cells:
            cell.row = index
    return rows


def _grid_box(page, table_box):
    """The drawn grid inside a table layout, when enough lines are visible."""
    table_width = _box_width(table_box)
    table_height = _box_height(table_box)
    if table_width <= 0 or table_height <= 0:
        return table_box
    verticals = []
    horizontals = []
    for curve in _seq(_raw(page, "pdf_curve")):
        box = _box(curve)
        if box is None:
            continue
        width = _box_width(box)
        height = _box_height(box)
        if (
            width <= 0.6
            and height >= max(2.0, table_height * 0.08)
            and table_box[0] - 1.0 <= _center_x(box) <= table_box[2] + 1.0
            and table_box[1] - 1.0 <= _center_y(box) <= table_box[3] + 1.0
        ):
            verticals.append(_center_x(box))
        if (
            height <= 0.6
            and width >= table_width * 0.5
            and table_box[1] - 1.0 <= _center_y(box) <= table_box[3] + 1.0
            and table_box[0] - 1.0 <= _center_x(box) <= table_box[2] + 1.0
        ):
            horizontals.append(_center_y(box))
    if len(verticals) < 2 or len(horizontals) < 2:
        return table_box
    return (min(verticals), min(horizontals), max(verticals), max(horizontals))


def _bounds_for_cell(page, table_box, cell):
    center_y = _center_y(cell.box)
    center_x = _center_x(cell.box)
    xs = {table_box[0], table_box[2]}
    for curve in _seq(_raw(page, "pdf_curve")):
        box = _box(curve)
        if box is None:
            continue
        if (
            _box_width(box) <= 0.6
            and _box_height(box) >= 2.0
            and box[1] - 0.6 <= center_y <= box[3] + 0.6
            and table_box[0] - 1.0 <= _center_x(box) <= table_box[2] + 1.0
        ):
            xs.add(min(table_box[2], max(table_box[0], _center_x(box))))
    ordered = sorted(xs)
    left = max((value for value in ordered if value <= center_x), default=table_box[0])
    right = min((value for value in ordered if value > center_x), default=table_box[2])
    return left, right


def _assign_geometry(page, table_box, cells):
    boundaries_present = False
    for cell in cells:
        left, right = _bounds_for_cell(page, table_box, cell)
        cell.cell_left = left
        cell.cell_right = right
        if left > table_box[0] + 0.75 or right < table_box[2] - 0.75:
            boundaries_present = True
    if boundaries_present:
        for cell in cells:
            cell.left_column = cell.cell_left <= table_box[0] + 1.0
        return True
    # No usable vertical lines: use the leftmost x cluster as the row-header
    # column, but keep it narrow so a two-column text table does not put every
    # cell in the first column.
    widths = sorted(_box_width(cell.box) for cell in cells)
    median_width = widths[len(widths) // 2] if widths else 0.0
    threshold = table_box[0] + max(6.0, min(median_width, _box_width(table_box) * 0.28))
    for cell in cells:
        cell.left_column = cell.box[0] <= threshold
    return False


def _assign_row_bounds(rows, table_box):
    """Fallback cell bounds from the gaps between cells in the same row."""
    for row in rows:
        ordered = sorted(
            (cell for cell in row.cells if cell.box is not None),
            key=lambda cell: (cell.box[0], cell.box[2]),
        )
        for index, cell in enumerate(ordered):
            if index == 0:
                left = table_box[0]
            else:
                left = (ordered[index - 1].box[2] + cell.box[0]) / 2
            if index + 1 == len(ordered):
                right = table_box[2]
            else:
                right = (cell.box[2] + ordered[index + 1].box[0]) / 2
            cell.cell_left = min(table_box[2], max(table_box[0], left))
            cell.cell_right = min(table_box[2], max(table_box[0], right))
            cell.left_column = index == 0


def _header_cell_box(cell, table_box):
    left = max(cell.box[0], cell.cell_left + 0.5)
    right = cell.cell_right - 0.5
    bottom = max(cell.box[1], table_box[1])
    top = min(cell.box[3], table_box[3])
    if right <= left or top <= bottom:
        return None
    return (round(left, 2), round(bottom, 2), round(right, 2), round(top, 2))


def _row_is_data(row):
    nonempty = [cell for cell in row.cells if _plain_text(cell.text)]
    if not nonempty:
        return False
    data_count = sum(
        _is_data_value(cell.text) and not _is_generic_label(cell.text)
        for cell in nonempty
    )
    ratio = data_count / len(nonempty)
    if ratio >= 0.4:
        return True
    left = [cell for cell in nonempty if cell.left_column]
    if left and _is_name_like(left[0].text):
        return any(
            _is_data_value(cell.text)
            for cell in nonempty
            if cell is not left[0]
        ) and ratio >= 0.2
    return False


def _caption_text(page, table_box, page_index):
    candidates = []
    for paragraph in _seq(_raw(page, "pdf_paragraph")):
        label = str(_raw(paragraph, "layout_label", "") or "").strip().lower()
        if label not in ("table_caption", "table_title"):
            continue
        box = _box(paragraph)
        if box is None:
            continue
        if _horizontal_overlap(box, table_box) <= 0:
            continue
        if box[1] >= table_box[3] - 1.0:
            distance = box[1] - table_box[3]
            order = 0
        elif box[3] <= table_box[1] + 1.0:
            distance = table_box[1] - box[3]
            order = 1
        else:
            continue
        if distance > max(40.0, _box_height(table_box) * 0.5 + 20.0):
            continue
        candidates.append((order, distance, _paragraph_text(paragraph)))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _table_identity(page, table_box, page_index, table_index):
    caption = _caption_text(page, table_box, page_index)
    match = _TABLE_NUMBER_RE.search(caption or "")
    number = match.group(1) if match else ""
    if number:
        label = f"表 {number}"
    else:
        label = f"第 {page_index + 1} 页第 {table_index + 1} 个表"
    return label, number, caption


def _looks_like_table_note(paragraph, box, grid_box, table_box):
    label = str(_raw(paragraph, "layout_label", "") or "").strip().lower()
    if label in ("table_caption", "table_title", "table_footnote", "caption"):
        return True
    text = _paragraph_text(paragraph)
    if not _plain_text(text):
        return True
    # Captions and notes that the layout model put inside the table frame are
    # outside the drawn grid; translate them as ordinary text, never as cells.
    if not _center_inside(box, grid_box):
        return True
    if _NOTE_START_RE.search(text) and _is_sentence_like(text):
        return True
    return False


def _make_context(label, role, header_cells, body_cells):
    header_labels = []
    for cell in header_cells:
        text = _plain_text(cell.text)
        if text and text not in header_labels:
            header_labels.append(text)
    body_examples = []
    for cell in body_cells:
        text = _plain_text(cell.text)
        if text and text not in body_examples and (
            _is_data_value(text) or _is_name_like(text)
        ):
            body_examples.append(text)
        if len(body_examples) >= 6:
            break
    return {
        "table": label,
        "role": role,
        "header_labels": header_labels[:10],
        "body_examples": body_examples,
    }


def _plan(page, table, page_index, table_index):
    table_box = _box(table)
    if table_box is None or _box_width(table_box) <= 0 or _box_height(table_box) <= 0:
        return None
    grid_box = _grid_box(page, table_box)
    cells = []
    for paragraph in _seq(_raw(page, "pdf_paragraph")):
        if not _paragraph_usable(paragraph):
            continue
        box = _box(paragraph)
        if not _center_inside(box, grid_box):
            continue
        if _looks_like_table_note(paragraph, box, grid_box, table_box):
            continue
        cells.append(_Cell(paragraph, box, _paragraph_text(paragraph)))
    if not cells:
        return None

    boundaries_present = _assign_geometry(page, grid_box, cells)
    rows = _group_rows(cells)
    if not boundaries_present:
        _assign_row_bounds(rows, grid_box)
    else:
        # A merged/multi-level header row can have no internal vertical lines
        # even when body rows do. Split that row between its own cells so one
        # header translation cannot expand across the neighbouring header.
        for row in rows:
            if (
                len(row.cells) > 1
                and all(
                    cell.cell_left <= grid_box[0] + 1.0
                    and cell.cell_right >= grid_box[2] - 1.0
                    for cell in row.cells
                )
            ):
                _assign_row_bounds([row], grid_box)
    label, number, caption = _table_identity(
        page, table_box, page_index, table_index
    )
    base = {
        "page": page_index + 1,
        "page_index": page_index,
        "table_index": table_index,
        "table": label,
        "table_number": number,
        "caption": caption,
        "table_box": table_box,
        "cell_count": len(cells),
        "rows": rows,
        "headers": [],
        "body": [],
        "status": "preserve",
        "reason": "",
    }
    if len(rows) < 2 or len({round(cell.cell_left, 1) for cell in cells}) < 2:
        if any(_is_generic_label(cell.text) for cell in cells):
            base["status"] = "uncertain"
            base["reason"] = "表格没有可分辨的行列结构"
        return _finish(base)

    first_data = next(
        (index for index, row in enumerate(rows) if _row_is_data(row)), None
    )
    if first_data is None:
        if any(_is_generic_label(cell.text) for cell in cells):
            base["status"] = "uncertain"
            base["reason"] = "表格没有数据行，无法区分表头与表体"
        return _finish(base)
    header_rows = rows[:first_data]
    if len(header_rows) > 3:
        base["status"] = "uncertain"
        base["reason"] = "表头候选超过三层，无法可靠定位表头"
        return _finish(base)

    for row_index, row in enumerate(rows):
        for cell in row.cells:
            if not _is_generic_label(cell.text):
                continue
            if row_index < first_data:
                cell.role = "column_header"
            elif cell.left_column:
                cell.role = "row_header"
    header_cells = [cell for cell in cells if cell.role != "body"]
    body_cells = [cell for cell in cells if cell.role == "body"]

    if not header_cells:
        # 名称/数字/符号表没有可翻译表头；这些单元格进入 body 由内容规则处理，
        # 不单独告警。
        if any(_is_generic_label(cell.text) for cell in cells):
            base["status"] = "uncertain"
            base["reason"] = "存在通用标签，但无法确定它属于表头还是表体"
        return _finish(base)

    for cell in header_cells:
        role = "column_header" if cell.row < first_data else "row_header"
        cell.role = role
        base["headers"].append(
            {
                "paragraph": cell.paragraph,
                "text": cell.text,
                "role": role,
                "row": cell.row,
                "cell_box": _header_cell_box(cell, table_box),
                "context": _make_context(label, role, header_cells, body_cells),
            }
        )
    for cell in body_cells:
        base["body"].append({"paragraph": cell.paragraph, "text": cell.text})
    base["status"] = "clear"
    return _finish(base)


def _finish(base):
    if base["status"] == "uncertain":
        # 不确定表不标记任何表头：整表单元格进入 body，由内容规则决定保留还是送译。
        base["body"] = [
            {"paragraph": cell.paragraph, "text": cell.text}
            for row in base["rows"]
            for cell in row.cells
        ]
        base["headers"] = []
    elif base["status"] != "clear":
        base["body"] = [
            {"paragraph": cell.paragraph, "text": cell.text}
            for row in base["rows"]
            for cell in row.cells
        ]
    base.pop("rows", None)
    return base


def analyze_document(document):
    """Return one plan per table layout that contains text paragraphs."""
    plans = []
    for page_index, page in enumerate(_seq(_raw(document, "page"))):
        tables = [
            layout
            for layout in _seq(_raw(page, "page_layout"))
            if str(_raw(layout, "class_name", "") or "").strip().lower() == "table"
            and _box(layout) is not None
        ]
        tables.sort(key=lambda layout: (-_box(layout)[3], _box(layout)[0]))
        for table_index, table in enumerate(tables):
            plan = _plan(page, table, page_index, table_index)
            if plan is not None:
                plans.append(plan)
    return plans


def summary(plans):
    """JSON-safe diagnostics for the runner's job log."""
    records = []
    for plan in plans:
        records.append(
            {
                "page": plan["page"],
                "table": plan["table"],
                "table_number": plan["table_number"],
                "caption": plan["caption"],
                "status": plan["status"],
                "reason": plan["reason"],
                "cell_count": plan["cell_count"],
                "header_count": len(plan["headers"]),
                "body_count": len(plan["body"]),
                "headers": [
                    {"text": header["text"], "role": header["role"]}
                    for header in plan["headers"]
                ],
            }
        )
    return records
