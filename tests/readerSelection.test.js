const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// 仅测试用：逐函数复制 Zotero 10.0.6 打包源码，不依赖 build/research 在运行时存在。

// 来源：build/research/omni/resource/reader/reader.js；保留原生半字符偏移、隔离过滤及区间重建。

// 原始来源：reader.js:42658–42681

function rectsDist([ax1, ay1, ax2, ay2], [bx1, by1, bx2, by2]) {
  let left = bx2 < ax1;
  let right = ax2 < bx1;
  let bottom = by2 < ay1;
  let top = ay2 < by1;
  if (top && left) {
    return Math.hypot(ax1 - bx2, ay2 - by1);
  } else if (left && bottom) {
    return Math.hypot(ax1 - bx2, ay1 - by2);
  } else if (bottom && right) {
    return Math.hypot(ax2 - bx1, ay1 - by2);
  } else if (right && top) {
    return Math.hypot(ax2 - bx1, ay2 - by1);
  } else if (left) {
    return ax1 - bx2;
  } else if (right) {
    return bx1 - ax2;
  } else if (bottom) {
    return ay1 - by2;
  } else if (top) {
    return by1 - ay2;
  }
  return 0;
}

// 原始来源：reader.js:42682–42694

function getClosestOffset(chars, rect) {
  let dist = Infinity;
  let idx = 0;
  for (let i = 0; i < chars.length; i++) {
    let ch = chars[i];
    let distance = rectsDist(ch.rect, rect);
    if (distance < dist) {
      dist = distance;
      idx = i;
    }
  }
  return idx;
}

// 原始来源：reader.js:42701–42703

function isRTL() {
  return false;
}

// 原始来源：reader.js:42707–42759

function getRangeBySelection({
  chars,
  anchor,
  head,
  reverse
}) {
  // 偏移范围为 0 到 chars.length（光标可以位于最后一个字符之后）。
  if (!chars.length) {
    return null;
  }
  let anchorOffset = reverse ? chars.length : 0;
  let headOffset = reverse ? 0 : chars.length;
  let x1, y1, x2, y2;
  if (Number.isInteger(anchor)) {
    anchorOffset = anchor;
    anchorOffset = anchorOffset < 0 ? 0 : anchorOffset;
    anchorOffset = anchorOffset > chars.length ? chars.length : anchorOffset;
  } else if (Array.isArray(anchor)) {
    [x1, y1] = anchor;
    anchorOffset = getClosestOffset(chars, [x1, y1, x1, y1]);
  }
  if (Number.isInteger(head)) {
    headOffset = head;
    headOffset = headOffset < 0 ? 0 : headOffset;
    headOffset = headOffset > chars.length ? chars.length : headOffset;
  } else if (Array.isArray(head)) {
    [x2, y2] = head;
    headOffset = getClosestOffset(chars, [x2, y2, x2, y2]);
  }
  if (Array.isArray(anchor)) {
    let {
      rotation,
      rect,
      c
    } = chars[anchorOffset];
    let rtl = isRTL(c);
    if (rtl && !rotation && x1 < rect[0] + (rect[2] - rect[0]) / 2 || !rtl && (!rotation && x1 > rect[0] + (rect[2] - rect[0]) / 2 || rotation === 90 && y1 > rect[1] + (rect[3] - rect[1]) / 2 || rotation === 180 && x1 < rect[0] + (rect[2] - rect[0]) / 2 || rotation === 270 && y1 < rect[1] + (rect[3] - rect[1]) / 2)) {
      anchorOffset++;
    }
  }
  if (Array.isArray(head)) {
    let {
      rotation,
      rect,
      c
    } = chars[headOffset];
    let rtl = isRTL(c);
    if (rtl && !rotation && x2 < rect[0] + (rect[2] - rect[0]) / 2 || !rtl && (!rotation && x2 > rect[0] + (rect[2] - rect[0]) / 2 || rotation === 90 && y2 > rect[1] + (rect[3] - rect[1]) / 2 || rotation === 180 && x2 < rect[0] + (rect[2] - rect[0]) / 2 || rotation === 270 && y2 < rect[1] + (rect[3] - rect[1]) / 2)) {
      headOffset++;
    }
  }
  return getRange(chars, anchorOffset, headOffset);
}

// 原始来源：reader.js:42799–42816

function getRectsFromChars(chars) {
  let lineRects = [];
  let currentLineRect = null;
  for (let char of chars) {
    if (!currentLineRect) {
      currentLineRect = char.inlineRect.slice();
    }
    currentLineRect = [Math.min(currentLineRect[0], char.inlineRect[0]), Math.min(currentLineRect[1], char.inlineRect[1]), Math.max(currentLineRect[2], char.inlineRect[2]), Math.max(currentLineRect[3], char.inlineRect[3])];
    if (char.lineBreakAfter) {
      lineRects.push(currentLineRect);
      currentLineRect = null;
    }
  }
  if (currentLineRect) {
    lineRects.push(currentLineRect);
  }
  return lineRects;
}

// 原始来源：reader.js:42817–42833

function getTextFromChars(chars) {
  let text = [];
  for (let char of chars) {
    if (!char.ignorable) {
      text.push(char.c);
      if (char.spaceAfter || char.lineBreakAfter) {
        text.push(' ');
      }
    }
    // OCR PDF 有时会把每行识别成独立段落；
    // 普通 PDF 则只在段落折到另一栏时需要这个空格。
    if (!char.ignorable && char.paragraphBreakAfter) {
      text.push(' ');
    }
  }
  return text.join('').trim();
}

// 原始来源：reader.js:42834–42862

function getRange(chars, anchorOffset, headOffset) {
  let charStart;
  let charEnd;
  if (anchorOffset < headOffset) {
    charStart = anchorOffset;
    charEnd = headOffset - 1;
  } else if (anchorOffset > headOffset) {
    charStart = headOffset;
    charEnd = anchorOffset - 1;
  } else {
    return {
      collapsed: true,
      anchorOffset,
      headOffset,
      rects: [],
      text: ''
    };
  }
  let rangeChars = chars.slice(charStart, charEnd + 1);
  let text = getTextFromChars(rangeChars);
  let rects = getRectsFromChars(rangeChars);
  rects = rects.map(rect => rect.map(value => parseFloat(value.toFixed(3))));
  return {
    anchorOffset,
    headOffset,
    rects,
    text
  };
}

// 原始来源：reader.js:42863–42881

function getNextLineClosestOffset(chars, offset) {
  let currentLineEnd = chars.findIndex((x, idx) => idx >= offset && x.lineBreakAfter);
  if (currentLineEnd === -1 || currentLineEnd === chars.length - 1) {
    return chars.length;
  }
  let nextLineStart = currentLineEnd + 1;
  let nextLineEnd = chars.findIndex((x, idx) => idx >= nextLineStart && x.lineBreakAfter);
  let closestOffset = null;
  let closestDist = null;
  let currentChar = chars[offset];
  for (let i = nextLineStart; i <= nextLineEnd; i++) {
    let char = chars[i];
    if (closestDist === null || rectsDist(char.rect, currentChar.rect) < closestDist) {
      closestDist = rectsDist(char.rect, currentChar.rect);
      closestOffset = i;
    }
  }
  return closestOffset;
}

// 原始来源：reader.js:42882–42907

function getPrevLineClosestOffset(chars, offset) {
  if (offset === chars.length) {
    offset--;
  }
  let prevLineEnd = chars.findLastIndex((x, idx) => idx < offset && x.lineBreakAfter);
  if (prevLineEnd === -1) {
    return 0;
  }
  let prevLineStart = chars.findLastIndex((x, idx) => idx < prevLineEnd && x.lineBreakAfter);
  if (prevLineStart === -1) {
    prevLineStart = 0;
  } else {
    prevLineStart++;
  }
  let closestOffset = null;
  let closestDist = null;
  let currentChar = chars[offset];
  for (let i = prevLineStart; i <= prevLineEnd; i++) {
    let char = chars[i];
    if (closestDist === null || rectsDist(char.rect, currentChar.rect) < closestDist) {
      closestDist = rectsDist(char.rect, currentChar.rect);
      closestOffset = i;
    }
  }
  return closestOffset;
}

// 原始来源：reader.js:42958–42980

function extractRange({
  chars,
  pageIndex,
  anchor,
  head,
  reverse
}) {
  let range = getRangeBySelection({
    chars,
    anchor,
    head,
    reverse
  });
  if (!range) {
    return null;
  }
  range.position = {
    pageIndex,
    rects: range.rects
  };
  delete range.rects;
  return range;
}

// 原始来源：reader.js:42997–43000

function getTopMostRectFromPosition(position) {
  // 按矩形的 y2 值降序排列并返回首项（保留源码实际使用的下标 2）。
  return position?.rects?.slice().sort((a, b) => b[2] - a[2])[0];
}

// 原始来源：reader.js:43001–43021

function getSortIndex(pdfPages, position) {
  let {
    pageIndex
  } = position;
  let offset = 0;
  let top = 0;
  if (pdfPages[position.pageIndex]) {
    let {
      chars
    } = pdfPages[position.pageIndex];
    let viewBox = pdfPages[position.pageIndex].viewBox;
    let rect = getTopMostRectFromPosition(position) || getPositionBoundingRect(position);
    offset = chars.length && getClosestOffset(chars, rect) || 0;
    let pageHeight = viewBox[3] - viewBox[1];
    top = pageHeight - rect[3];
    if (top < 0) {
      top = 0;
    }
  }
  return [pageIndex.toString().slice(0, 5).padStart(5, '0'), offset.toString().slice(0, 6).padStart(6, '0'), Math.floor(top).toString().slice(0, 5).padStart(5, '0')].join('|');
}

// 原始来源：reader.js:43022–43115

function getModifiedSelectionRanges(pdfPages, selectionRanges, modifier) {
  if (!selectionRanges.length) {
    return [];
  }
  let range = selectionRanges.find(x => x.anchor);
  let anchor = {
    pageIndex: range.position.pageIndex,
    offset: range.anchorOffset
  };
  range = selectionRanges.find(x => x.head);
  let head = {
    pageIndex: range.position.pageIndex,
    offset: range.headOffset
  };
  if (!pdfPages[head.pageIndex]) {
    return [];
  }
  if (modifier === 'left') {
    if (head.offset === 0) {
      if (pdfPages[head.pageIndex - 1]) {
        let {
          chars
        } = pdfPages[head.pageIndex - 1];
        if (chars.length) {
          head.pageIndex--;
          head.offset = chars.length - 1;
        }
      }
    } else {
      head.offset--;
    }
  } else if (modifier === 'right') {
    let {
      chars
    } = pdfPages[head.pageIndex];
    if (head.offset === chars.length) {
      if (pdfPages[head.pageIndex + 1]) {
        let {
          chars
        } = pdfPages[head.pageIndex + 1];
        if (chars.length) {
          head.pageIndex++;
          head.offset = 1;
        }
      }
    } else {
      head.offset++;
    }
  } else if (modifier === 'up') {
    if (head.offset === 0) {
      if (pdfPages[head.pageIndex - 1]) {
        let {
          chars
        } = pdfPages[head.pageIndex - 1];
        if (chars.length) {
          head.pageIndex--;
          head.offset = chars.length - 1;
        }
      }
    } else {
      let {
        chars
      } = pdfPages[head.pageIndex];
      let offset = getPrevLineClosestOffset(chars, head.offset);
      if (offset !== null) {
        head.offset = offset;
      }
    }
  } else if (modifier === 'down') {
    let {
      chars
    } = pdfPages[head.pageIndex];
    if (head.offset === chars.length) {
      if (pdfPages[head.pageIndex + 1]) {
        let {
          chars
        } = pdfPages[head.pageIndex + 1];
        if (chars.length) {
          head.pageIndex++;
          head.offset = 1;
        }
      }
    } else {
      let offset = getNextLineClosestOffset(chars, head.offset);
      if (offset !== null) {
        head.offset = offset;
      }
    }
  } else if (typeof modifier === 'object') {
    let position = modifier;
    head = position;
  }
  return getSelectionRanges(pdfPages, anchor, head);
}

// 原始来源：reader.js:42908
function getClosestWord(chars, rect) {
  let closestWordStart;
  let closestWordEnd;
  let closestWordDistance = null;
  let start = 0;
  for (let i = 0; i < chars.length; i++) {
    let char = chars[i];
    if (char.wordBreakAfter) {
      let end = i;
      let wordChars = chars.slice(start, end + 1);
      let wordRect = [Math.min(...wordChars.map(x => x.inlineRect[0])), Math.min(...wordChars.map(x => x.inlineRect[1])), Math.max(...wordChars.map(x => x.inlineRect[2])), Math.max(...wordChars.map(x => x.inlineRect[3]))];
      let distance = rectsDist(rect, wordRect);
      if (closestWordDistance === null || distance < closestWordDistance) {
        closestWordDistance = distance;
        closestWordStart = start;
        closestWordEnd = end;
      }
      start = end + 1;
    }
  }
  return {
    anchorOffset: closestWordStart,
    headOffset: closestWordEnd + 1
  };
}

// 原始来源：reader.js:42933
function getClosestLine(chars, rect) {
  let closestLineStart;
  let closestLineEnd;
  let closestLineDistance = null;
  let start = 0;
  for (let i = 0; i < chars.length; i++) {
    let char = chars[i];
    if (char.lineBreakAfter) {
      let end = i;
      let lineChars = chars.slice(start, end + 1);
      let lineRect = [Math.min(...lineChars.map(x => x.inlineRect[0])), Math.min(...lineChars.map(x => x.inlineRect[1])), Math.max(...lineChars.map(x => x.inlineRect[2])), Math.max(...lineChars.map(x => x.inlineRect[3]))];
      let distance = rectsDist(rect, lineRect);
      if (closestLineDistance === null || distance < closestLineDistance) {
        closestLineDistance = distance;
        closestLineStart = start;
        closestLineEnd = end;
      }
      start = end + 1;
    }
  }
  return {
    anchorOffset: closestLineStart,
    headOffset: closestLineEnd + 1
  };
}

// 原始来源：reader.js:43116
function getWordSelectionRanges(pdfPages, anchorPosition, headPosition) {
  if (!pdfPages[anchorPosition.pageIndex]) {
    return [];
  }
  let {
    chars
  } = pdfPages[anchorPosition.pageIndex];
  let anchorWord = getClosestWord(chars, anchorPosition.rects[0]);
  chars = pdfPages[headPosition.pageIndex].chars;
  let headWord = getClosestWord(chars, headPosition.rects[0]);
  if (!anchorWord || !headWord) {
    return [];
  }
  let anchor = {
    pageIndex: anchorPosition.pageIndex
  };
  let head = {
    pageIndex: headPosition.pageIndex
  };
  if (anchorWord.anchorOffset <= headWord.anchorOffset && anchor.pageIndex === head.pageIndex || anchor.pageIndex < head.pageIndex) {
    anchor.offset = anchorWord.anchorOffset;
    head.offset = headWord.headOffset;
  } else {
    anchor.offset = anchorWord.headOffset;
    head.offset = headWord.anchorOffset;
  }
  return getSelectionRanges(pdfPages, anchor, head);
}

// 原始来源：reader.js:43144
function getLineSelectionRanges(pdfPages, anchorPosition, headPosition) {
  if (!pdfPages[anchorPosition.pageIndex]) {
    return [];
  }
  let {
    chars
  } = pdfPages[anchorPosition.pageIndex];
  let anchorLine = getClosestLine(chars, anchorPosition.rects[0]);
  chars = pdfPages[headPosition.pageIndex].chars;
  let headLine = getClosestLine(chars, headPosition.rects[0]);
  if (!anchorLine || !headLine) {
    return [];
  }
  let anchor = {
    pageIndex: anchorPosition.pageIndex
  };
  let head = {
    pageIndex: headPosition.pageIndex
  };
  if (anchorLine.anchorOffset <= headLine.anchorOffset && anchor.pageIndex === head.pageIndex || anchor.pageIndex < head.pageIndex) {
    anchor.offset = anchorLine.anchorOffset;
    head.offset = headLine.headOffset;
  } else {
    anchor.offset = anchorLine.headOffset;
    head.offset = headLine.anchorOffset;
  }
  return getSelectionRanges(pdfPages, anchor, head);
}

// 根据 selectionRanges 和 pdfPages 提取字符数组。

// 原始来源：reader.js:43174–43197

function getCharsFromSelectionRanges(pdfPages, selectionRanges) {
  const charsArray = [];

  // 遍历每个选区，直接读取字符。
  selectionRanges.forEach(selection => {
    const pageIndex = selection.position.pageIndex;
    const page = pdfPages[pageIndex];
    if (!page) return; // 跳过尚未加载的页面。

    let {
      chars
    } = page;

    // 用 anchor 和 head 偏移计算字符范围。
    const anchorOffset = selection.anchorOffset;
    const headOffset = selection.headOffset;
    const start = Math.min(anchorOffset, headOffset);
    const end = Math.max(anchorOffset, headOffset);

    // 读取计算范围内的字符。
    charsArray.push(...chars.slice(start, end));
  });
  return charsArray;
}

// 原始来源：reader.js:43198–43292

function applySelectionRangeIsolation(pdfPages, selectionRanges) {
  if (!selectionRanges.length) {
    return [];
  }
  if (selectionRanges[0].collapsed === true) {
    return selectionRanges;
  }
  let chars = getCharsFromSelectionRanges(pdfPages, selectionRanges);
  let isolated;
  let start;
  if (selectionRanges[0].anchor) {
    if (selectionRanges[0].head) {
      start = selectionRanges[0].anchorOffset < selectionRanges[0].headOffset;
    } else {
      start = true;
    }
  } else {
    start = selectionRanges.at(-1).anchorOffset < selectionRanges.at(-1).headOffset;
  }
  if (start) {
    isolated = !!chars[0].isolated;
  } else {
    isolated = !!chars.at(-1).isolated;
  }
  if (isolated) {
    chars = chars.filter(x => x.isolated);
  } else {
    chars = chars.filter(x => !x.isolated);
  }
  if (chars.length === 0) return [];
  let reversed = selectionRanges[0].headOffset < selectionRanges[0].anchorOffset;
  selectionRanges = [];
  let currentRange = {
    pageIndex: chars[0].pageIndex,
    anchorOffset: chars[0].offset,
    headOffset: chars[0].offset
  };
  for (let i = 0; i < chars.length; i++) {
    const char = chars[i];
    if (char.pageIndex !== currentRange.pageIndex) {
      // 结束当前区间并加入数组。
      selectionRanges.push(currentRange);

      // 开始新区间。
      currentRange = {
        pageIndex: char.pageIndex,
        anchorOffset: char.offset,
        headOffset: char.offset + 1
      };
    } else {
      // 扩展当前区间。
      currentRange.headOffset = char.offset + 1;
    }
  }

  // 循环结束后加入最后一个区间。
  selectionRanges.push(currentRange);
  if (reversed) {
    for (let selectionRange of selectionRanges) {
      let tmp = selectionRange.anchorOffset;
      selectionRange.anchorOffset = selectionRange.headOffset;
      selectionRange.headOffset = tmp;
    }
  }
  if (selectionRanges.length === 1) {
    selectionRanges[0].anchor = true;
    selectionRanges[0].head = true;
  } else {
    if (start) {
      selectionRanges[0].anchor = true;
      selectionRanges.at(-1).head = true;
    } else {
      selectionRanges.at(-1).anchor = true;
      selectionRanges[0].head = true;
    }
  }
  for (let selectionRange of selectionRanges) {
    let {
      chars
    } = pdfPages[selectionRange.pageIndex];
    let from = Math.min(selectionRange.anchorOffset, selectionRange.headOffset);
    let to = Math.max(selectionRange.anchorOffset, selectionRange.headOffset);
    let rects = getRectsFromChars(chars.slice(from, to));
    selectionRange.position = {
      pageIndex: selectionRange.pageIndex,
      rects
    };
    selectionRange.sortIndex = getSortIndex(pdfPages, selectionRange.position);
    selectionRange.text = getTextFromChars(chars.slice(from, to));
    if (selectionRange.anchorOffset === selectionRange.headOffset) {
      selectionRange.collapsed = true;
    }
  }
  return selectionRanges;
}

// 原始来源：reader.js:43293–43335

function getSelectionRanges(pdfPages, anchor, head) {
  let selectionRanges = [];
  let fromPageIndex = Math.min(anchor.pageIndex, head.pageIndex);
  let toPageIndex = Math.max(anchor.pageIndex, head.pageIndex);
  let reverse = anchor.pageIndex > head.pageIndex;
  for (let i = fromPageIndex; i <= toPageIndex; i++) {
    if (!pdfPages[i]) {
      continue;
    }
    let a, h;
    if (i === anchor.pageIndex) {
      a = anchor.offset !== undefined ? anchor.offset : [anchor.rects[0][0], anchor.rects[0][1]];
    }
    if (i === head.pageIndex) {
      h = head.offset !== undefined ? head.offset : [head.rects[0][0], head.rects[0][1]];
    }
    let {
      chars
    } = pdfPages[i];
    let selectionRange = extractRange({
      chars,
      pageIndex: i,
      anchor: a,
      head: h,
      reverse
    });
    if (!selectionRange) {
      return [];
    }
    if (i === anchor.pageIndex) {
      selectionRange.anchor = true;
    }
    if (i === head.pageIndex) {
      selectionRange.head = true;
    }
    if (!selectionRange.collapsed) {
      // 当前根据 position.rects 生成 sortIndex，可能没有使用字符偏移精确。
      selectionRange.sortIndex = getSortIndex(pdfPages, selectionRange.position);
    }
    selectionRanges.push(selectionRange);
  }
  return applySelectionRangeIsolation(pdfPages, selectionRanges);
}

// reader.js:32305–32348 的 rects 分支；测试位置无 rotation/paths。

function getPositionBoundingRect(position) {
  const rects = position.rects;
  return [Math.min(...rects.map(r => r[0])), Math.min(...rects.map(r => r[1])),
    Math.max(...rects.map(r => r[2])), Math.max(...rects.map(r => r[3]))];
}

// 原始来源：reader.js:31357–31393，原生拖选的 leading/trailing 节流。
function throttle(func, wait, options) {
  var context, args, result;
  var timeout = null;
  var previous = 0;
  if (!options) options = {};

  // 动态获取等待时间。
  var getWaitTime = function () {
    return typeof wait === 'function' ? wait() : wait;
  };
  var later = function () {
    previous = options.leading === false ? 0 : Date.now();
    timeout = null;
    result = func.apply(context, args);
    if (!timeout) context = args = null;
  };
  return function () {
    var now = Date.now();
    if (!previous && options.leading === false) previous = now;
    var remaining = getWaitTime() - (now - previous); // 调用辅助函数。
    context = this;
    args = arguments;
    if (remaining <= 0 || remaining > getWaitTime()) {
      // 调用辅助函数。
      if (timeout) {
        clearTimeout(timeout);
        timeout = null;
      }
      previous = now;
      result = func.apply(context, args);
      if (!timeout) context = args = null;
    } else if (!timeout && options.trailing !== false) {
      timeout = setTimeout(later, remaining);
    }
    return result;
  };
}

// 原始来源：reader.js:78228–78250；验证每页一个区间的批注消费约定。
// 源码摘要：保留原缩进、换行统一 LF、无末尾换行，832 字符；SHA-256：
// e1524b05f4b6b143d79176f3b3ad8fd4df19b4fb36406f6aba4b0b83a84dd3c2
function nativeAnnotation(selectionRanges, type, color) {
    if (selectionRanges[0].collapsed) {
      return null;
    }
    selectionRanges = selectionRanges.slice();
    selectionRanges.sort((a, b) => a.pageIndex - b.pageIndex);
    selectionRanges = selectionRanges.slice(0, 2);
    let selectionRange = selectionRanges[0];
    let annotation = {
      type,
      color,
      sortIndex: selectionRange.sortIndex,
      pageLabel: this._getPageLabel(selectionRange.position.pageIndex, true),
      position: selectionRange.position,
      text: selectionRange.text
    };
    if (selectionRanges.length === 2) {
      let selectionRange = selectionRanges[1];
      annotation.position.nextPageRects = selectionRange.position.rects;
      annotation.text += ' ' + selectionRange.text;
    }
    return annotation;
  }

// 原始来源：reader.js:76051–76061；区间数直接决定 nextPageRects。
// 源码摘要：保留原缩进、换行统一 LF、无末尾换行，394 字符；SHA-256：
// 5d9dbf6f2f85bfccb9c833cd1be16a05861062b8af5489de21612c284471c862
function nativeSelectionPosition() {
    if (!this._selectionRanges?.length || this._selectionRanges[0].collapsed) return null;
    let ranges = this._selectionRanges.slice().sort((a, b) => a.pageIndex - b.pageIndex).slice(0, 2);
    let position = {
      ...ranges[0].position
    };
    if (ranges.length === 2) {
      position.nextPageRects = ranges[1].position.rects;
    }
    return position;
}

// 原始来源：reader.js:76726–76739；菜单缓存批注，不能用当前选区替代该缓存做断言。
// 源码摘要：保留原缩进、换行统一 LF、无末尾换行，501 字符；SHA-256：
// 63327ae2b8618ba8a13ac5e939bc56f6ab8805b5aec568fae8a4e8037818131c
function nativeSetSelectionRanges(selectionRanges) {
    this._selectionRanges = selectionRanges || [];
    let selectionRange = this._selectionRanges[0];
    if (selectionRange && !selectionRange.collapsed) {
      let rect = this.getClientRectForPopup(selectionRange.position);
      let annotation = this._getAnnotationFromSelectionRanges(this._selectionRanges, 'highlight');
      this._onSetSelectionPopup({
        rect,
        annotation
      });
    } else {
      this._onSetSelectionPopup();
    }
}

// 原始来源：Zotero 10.0.6 reader.js:76747–76761；保留直接索引 headOffset 的行为。
function nativeScrollSelectionHeadIntoView(selectionRanges) {
    let selectionRange = selectionRanges.find(x => !x.collapsed && x.head);
    if (selectionRange) {
      let {
        chars
      } = this._pdfPages[selectionRange.pageIndex];
      let char = chars[selectionRange.headOffset];
      this.navigateToPosition({
        pageIndex: selectionRange.pageIndex,
        rects: [char.rect]
      }, {
        block: 'nearest'
      });
    }
}

// 原始来源：reader.js:43414–43419。
function getTextFromSelectionRanges(selectionRanges) {
  if (!selectionRanges.length || selectionRanges[0].collapsed) {
    return '';
  }
  return selectionRanges.map(x => x.text).join('\n');
}

// 原始来源：reader.js:79248–79282；保留原生批注和超过两页的纯文本分支。
function nativeHandleCopy(event) {
    if (this._textAnnotationFocused()) {
      return;
    }
    if (this._nativeTextSelection?.hasSelection() && !this._nativeTextSelection.syncNow()) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    if (!event.clipboardData) {
      return;
    }
    // 复制批注。
    if (this._selectedAnnotationIDs.length) {
      let annotation = this._annotations.find(x => x.id === this._selectedAnnotationIDs[0]);
      if (!annotation) {
        return;
      }
      this._onSetDataTransferAnnotations(event.clipboardData, annotation);
    }
    // 复制文本。
    else {
      if (this._selectionRanges.length <= 2) {
        let annotation = this._getAnnotationFromSelectionRanges(this._selectionRanges, 'highlight');
        if (!annotation) {
          return;
        }
        this._onSetDataTransferAnnotations(event.clipboardData, annotation, true);
      } else {
        // 超过两页的选区只复制纯文本。
        let fullText = getTextFromSelectionRanges(this._selectionRanges);
        event.clipboardData.setData('text/plain', fullText);
      }
    }
}

const tests = [];
function test(name, run) { tests.push({ name, run }); }

function windowMock() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(type, handler, capture) {
      const entries = listeners.get(type) || [];
      assert.ok(!entries.some(entry => entry.handler === handler), "监听不得重复注册");
      entries.push({ handler, capture });
      listeners.set(type, entries);
    },
    removeEventListener(type, handler, capture) {
      listeners.set(type, (listeners.get(type) || []).filter(entry =>
        entry.handler !== handler || entry.capture !== capture));
    },
    fire(type, event = {}) {
      for (const { handler } of [...(listeners.get(type) || [])].sort((a, b) =>
        Number(!!b.capture) - Number(!!a.capture))) handler(event);
    },
    count(type) { return (listeners.get(type) || []).length; },
  };
}

function layout() {
  return { v: 1, pages: [0, 1, 2].map(src => ({
    src, part: "a", regions: [
      { k: "en", r: [0, 100, 300, 700] },
      { k: "zh", r: [300, 100, 600, 700] },
      { k: "span", r: [0, 700, 600, 780] },
      { k: "hf", r: [0, 0, 600, 100] },
    ],
  })) };
}

function page() {
  return { viewBox: [0, 0, 600, 800], chars: [
    { rect: [50, 200, 60, 210] },
    { rect: [350, 200, 360, 210], isolated: false },
    { rect: [50, 730, 60, 740] },
    { rect: [50, 20, 60, 30] },
    { rect: [80, 200, 90, 210], isolated: true },
  ] };
}

function fixture(metadata = { info: { Custom: { ZTFSplitLayout: JSON.stringify(layout()) } } }) {
  const clones = new WeakSet();
  const cloneCalls = [];
  const logs = [];
  const outer = windowMock();
  const pdfDocument = {
    calls: 0,
    async getMetadata() { this.calls++; return metadata; },
  };
  function makeView() {
    const win = windowMock();
    win.PDFViewerApplication = { pdfDocument };
    return {
      _iframeWindow: win,
      initializedPromise: Promise.resolve(),
      _pdfPages: { 0: page(), 1: page() },
      _processedPageIsolatedCharIndexes: {},
      _selectionRanges: [],
      _getPageLabel(index) { return String(index + 1); },
      _getAnnotationFromSelectionRanges: nativeAnnotation,
      pointerEventToPosition(event) {
        assert.equal(this._iframeWindow, win, "方法必须保留 this");
        // 原生事件在视图自己的 realm 中；插件传入的定位对象则必须克隆。
        if (!event.type) {
          assert.ok(clones.has(event), "chrome 参数必须先 cloneInto");
          assert.deepEqual(Object.keys(event).sort(), ["clientX", "clientY"]);
        }
        return event.offPage || event.clientX < 0 ? null : {
          pageIndex: Math.floor(event.clientX / 600),
          rects: [[event.clientX % 600, event.clientY, event.clientX % 600, event.clientY]],
        };
      },
      pointerEventToAltPosition(event, pageIndex) {
        const x = event.clientX - pageIndex * 600;
        return { pageIndex, rects: [[x, event.clientY, x, event.clientY]] };
      },
      _setSelectionRanges(ranges) { this._selectionRanges = ranges || []; },
      _scrollSelectionHeadIntoView() {},
      _onSetSelectionPopup() {},
      _render() {},
      _mergeProcessedPageData(index, data) {
        assert.equal(this._iframeWindow, win);
        if (!data) return data;
        const isolated = this._processedPageIsolatedCharIndexes[index];
        if (!isolated) return data;
        return { ...data, chars: data.chars.map((char, i) =>
          isolated.includes(i) ? { ...char, isolated: true } : char) };
      },
    };
  }
  const internal = {
    _primaryView: makeView(), _secondaryView: null,
    _createView() { return makeView(); },
  };
  const reader = { _iframeWindow: outer, _internalReader: internal };
  const context = {
    Components: { utils: { cloneInto(value, target) {
      const cloned = structuredClone(value);
      clones.add(cloned);
      cloneCalls.push({ value, target });
      return cloned;
    } } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,
    "../addon/content/modules/readerSelection.js"), "utf8"), context);
  const module = context.ZoteroTranslateFilesReaderSelection;
  module.init({ Zotero: { debug: line => logs.push(line) }, log: line => logs.push(line) });
  return { module, reader, internal, outer, makeView, logs, pdfDocument, cloneCalls, context };
}

function flags(view, index = 0) { return view._pdfPages[index].chars.map(char => !!char.isolated); }
function down(view, x, y) { view._iframeWindow.fire("pointerdown", { clientX: x, clientY: y, button: 0 }); }

test("冻结元数据与左下原点坐标，四类起点及跨页隔离", async () => {
  const f = fixture();
  assert.equal(await f.module.attachToReader(f.reader), true);
  const v = f.internal._primaryView;
  assert.equal(v._iframeWindow.listeners.get("pointerdown")[0].capture, true);
  for (const [x, y, expected] of [
    [55, 205, [false, true, true, true, true]],
    [355, 205, [true, false, true, true, true]],
    [55, 735, [true, true, false, true, true]],
    [85, 205, [true, true, true, true, false]],
    [55, 25, [true, true, true, false, true]],
  ]) {
    down(v, x, y);
    assert.deepEqual(flags(v), expected);
    assert.deepEqual(flags(v, 1), y === 735 ? [true, true, true, true, true] : expected,
      "跨栏只开放起点页的本元素，其他类别在已加载页保持同类");
  }
  assert.equal(f.cloneCalls.length, 5);
  assert.ok(f.cloneCalls.every(call => call.target === v._iframeWindow));
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

test("普通 PDF 完全不挂钩、不包装方法", async () => {
  const f = fixture({ info: { Title: "ordinary", ZTFSplitLayout: JSON.stringify(layout()) } });
  const v = f.internal._primaryView;
  const merge = v._mergeProcessedPageData;
  const create = f.internal._createView;
  assert.equal(await f.module.attachToReader(f.reader), false);
  assert.equal(v._iframeWindow.count("pointerdown"), 0);
  assert.equal(v._mergeProcessedPageData, merge);
  assert.equal(f.internal._createView, create);
  assert.deepEqual(flags(v), [false, false, false, false, true]);
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

test("原始 isolated 的有无和值在切换起点、卸载后精确保留", async () => {
  const f = fixture();
  const v = f.internal._primaryView;
  const before = structuredClone(v._pdfPages);
  await f.module.attachToReader(f.reader);
  down(v, 55, 205);
  down(v, 85, 205);
  down(v, 355, 205);
  f.module.shutdown();
  assert.deepEqual(v._pdfPages, before);
});

test("合并已标记页面与延迟原生隔离，随后新加载页使用当前类别", async () => {
  const f = fixture();
  const v = f.internal._primaryView;
  const merge = v._mergeProcessedPageData;
  await f.module.attachToReader(f.reader);
  down(v, 55, 205);
  v._processedPageIsolatedCharIndexes[0] = [0, 4];
  v._pdfPages[0] = v._mergeProcessedPageData(0, v._pdfPages[0]);
  assert.deepEqual(flags(v), [false, true, true, true, false], "起点变成原生隔离类后重新标记所有页面");
  down(v, 85, 205);
  assert.deepEqual(flags(v), [false, true, true, true, false]);
  v._pdfPages[2] = v._mergeProcessedPageData(2, page());
  assert.deepEqual(flags(v, 2), [true, true, true, true, false]);
  down(v, 355, 205);
  v._pdfPages[2] = v._mergeProcessedPageData(2, page());
  assert.deepEqual(flags(v, 2), [true, false, true, true, true]);
  assert.equal(v._mergeProcessedPageData(2, null), null);
  f.module.shutdown();
  assert.equal(v._mergeProcessedPageData, merge);
  assert.deepEqual(flags(v), [true, false, false, false, true], "卸载保留后来发现的原生隔离");
});

test("未选起点前的页面合并不改原生行为", async () => {
  const f = fixture();
  const v = f.internal._primaryView;
  await f.module.attachToReader(f.reader);
  const data = page();
  assert.equal(v._mergeProcessedPageData(2, data), data);
  assert.deepEqual(data, page());
  f.module.shutdown();
});

test("现有分屏、后来创建的分屏独立挂钩，关闭后移除", async () => {
  const f = fixture();
  f.internal._secondaryView = f.makeView();
  const create = f.internal._createView;
  await f.module.attachToReader(f.reader);
  const primary = f.internal._primaryView;
  const secondary = f.internal._secondaryView;
  down(primary, 55, 205);
  down(secondary, 355, 205);
  assert.deepEqual(flags(primary), [false, true, true, true, true]);
  assert.deepEqual(flags(secondary), [true, false, true, true, true]);
  secondary._iframeWindow.fire("unload");
  assert.equal(secondary._iframeWindow.count("pointerdown"), 0);
  f.internal._secondaryView = f.internal._createView(false);
  await new Promise(resolve => setImmediate(resolve));
  const next = f.internal._secondaryView;
  assert.equal(next._iframeWindow.count("pointerdown"), 1);
  down(next, 55, 735);
  assert.deepEqual(flags(next), [true, true, false, true, true]);
  f.module.shutdown();
  assert.equal(primary._iframeWindow.count("pointerdown"), 0);
  assert.equal(next._iframeWindow.count("pointerdown"), 0);
  assert.equal(f.internal._createView, create);
});

test("并发及重复 attach、关闭重开、重复 init/shutdown 不累积监听", async () => {
  const f = fixture();
  await Promise.all([f.module.attachToReader(f.reader), f.module.attachToReader(f.reader)]);
  f.module.init({ Zotero: {} });
  await f.module.attachToReader(f.reader);
  const old = f.internal._primaryView;
  assert.equal(f.pdfDocument.calls, 1);
  assert.equal(old._iframeWindow.count("pointerdown"), 1);
  assert.equal(f.outer.count("unload"), 1);
  f.outer.fire("unload");
  assert.equal(old._iframeWindow.count("pointerdown"), 0);
  assert.equal(f.outer.count("unload"), 0);
  f.internal._primaryView = f.makeView();
  await f.module.attachToReader(f.reader);
  assert.equal(f.internal._primaryView._iframeWindow.count("pointerdown"), 1);
  f.module.shutdown();
  f.module.shutdown();
  assert.equal(f.outer.count("unload"), 0);
});

for (const [name, mutate] of [
  ["reader 窗口", f => { delete f.reader._iframeWindow; }],
  ["内部 reader", f => { delete f.reader._internalReader; }],
  ["主视图", f => { delete f.internal._primaryView; }],
  ["分屏创建方法", f => { delete f.internal._createView; }],
  ["视图窗口", f => { delete f.internal._primaryView._iframeWindow; }],
  ["监听方法", f => { delete f.internal._primaryView._iframeWindow.addEventListener; }],
  ["移除方法", f => { delete f.internal._primaryView._iframeWindow.removeEventListener; }],
  ["定位方法", f => { delete f.internal._primaryView.pointerEventToPosition; }],
  ["页外定位方法", f => { delete f.internal._primaryView.pointerEventToAltPosition; }],
  ["选区更新方法", f => { delete f.internal._primaryView._setSelectionRanges; }],
  ["选区滚动方法", f => { delete f.internal._primaryView._scrollSelectionHeadIntoView; }],
  ["选区菜单回调", f => { delete f.internal._primaryView._onSetSelectionPopup; }],
  ["重绘方法", f => { delete f.internal._primaryView._render; }],
  ["批注区间方法", f => { delete f.internal._primaryView._getAnnotationFromSelectionRanges; }],
  ["页面集合", f => { delete f.internal._primaryView._pdfPages; }],
  ["选区集合", f => { delete f.internal._primaryView._selectionRanges; }],
  ["选区属性不可包装", f => { Object.defineProperty(f.internal._primaryView, "_selectionRanges", {
    configurable: false,
  }); }],
  ["合并方法", f => { delete f.internal._primaryView._mergeProcessedPageData; }],
  ["pdf 应用", f => { delete f.internal._primaryView._iframeWindow.PDFViewerApplication; }],
  ["元数据方法", f => { delete f.pdfDocument.getMetadata; }],
  ["跨域克隆", f => { delete f.context.Components.utils.cloneInto; }],
  ["次视图缺接口", f => { f.internal._secondaryView = {}; }],
]) {
  test(`缺失${name}只记一次诊断并完整回退`, async () => {
    const f = fixture();
    const primary = f.internal._primaryView;
    const merge = primary._mergeProcessedPageData;
    mutate(f);
    assert.equal(await f.module.attachToReader(f.reader), false);
    assert.equal(await f.module.attachToReader(f.reader), false);
    assert.equal(f.logs.length, 1);
    const diagnostic = JSON.parse(f.logs[0]);
    assert.equal(diagnostic.event, "reader_selection_fallback");
    assert.equal(diagnostic.count, 1);
    if (primary._iframeWindow) assert.equal(primary._iframeWindow.count("pointerdown"), 0);
    if (primary._mergeProcessedPageData) assert.equal(primary._mergeProcessedPageData, merge);
    f.module.shutdown();
  });
}

for (const bad of ["{secret invalid json", { v: 2, pages: [] }, { v: 1, pages: [
  { src: 0, part: "a", regions: [{ k: "en", r: [0, 0, null, 8] }] },
] }, { v: 1, pages: [{ src: 0, part: "a", regions: [{ k: "en", r: [4, 5, 1, 2] }] }] }]) {
  test("无效版面元数据不挂钩且不把原文写日志", async () => {
    const f = fixture({ info: { Custom: { ZTFSplitLayout: typeof bad === "string" ? bad : JSON.stringify(bad) } } });
    assert.equal(await f.module.attachToReader(f.reader), false);
    assert.equal(await f.module.attachToReader(f.reader), false);
    assert.equal(f.logs.length, 1);
    assert.ok(!f.logs.join("").includes("secret"));
    f.module.shutdown();
  });
}

test("空区域回退页不改变原生隔离；点击正文外恢复原标记", async () => {
  const data = layout();
  data.pages[1].regions = [];
  const f = fixture({ info: { Custom: { ZTFSplitLayout: JSON.stringify(data) } } });
  await f.module.attachToReader(f.reader);
  const v = f.internal._primaryView;
  down(v, 55, 205);
  assert.deepEqual(flags(v, 1), [false, false, false, false, true]);
  down(v, 655, 205);
  assert.deepEqual(flags(v), [false, false, false, false, true]);
  down(v, -1, 205);
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

test("运行中接口消失时恢复所有标记、移除钩子且只记一次", async () => {
  const f = fixture();
  const v = f.internal._primaryView;
  await f.module.attachToReader(f.reader);
  down(v, 55, 205);
  delete v.pointerEventToPosition;
  assert.doesNotThrow(() => down(v, 355, 205));
  assert.equal(v._iframeWindow.count("pointerdown"), 0);
  assert.deepEqual(flags(v), [false, false, false, false, true]);
  assert.equal(await f.module.attachToReader(f.reader), false);
  assert.equal(f.logs.length, 1);
  f.module.shutdown();
});

test("元数据读取失败不泄漏异常详情", async () => {
  const f = fixture();
  f.pdfDocument.getMetadata = async () => { throw new Error("secret file path"); };
  assert.equal(await f.module.attachToReader(f.reader), false);
  assert.equal(f.logs.length, 1);
  assert.ok(!f.logs[0].includes("secret"));
  f.module.shutdown();
});

test("元数据尚未返回时 shutdown，后续不得安装监听", async () => {
  const f = fixture();
  let resolve;
  f.pdfDocument.getMetadata = () => new Promise(done => { resolve = done; });
  const pending = f.module.attachToReader(f.reader);
  await new Promise(done => setImmediate(done));
  f.module.shutdown();
  resolve({ info: { Custom: { ZTFSplitLayout: JSON.stringify(layout()) } } });
  assert.equal(await pending, false);
  assert.equal(f.internal._primaryView._iframeWindow.count("pointerdown"), 0);
  assert.equal(f.outer.count("unload"), 0);
});

test("元数据尚未返回时关闭 reader，迟到结果不得重新挂钩", async () => {
  const f = fixture();
  let resolve;
  f.pdfDocument.getMetadata = () => new Promise(done => { resolve = done; });
  const pending = f.module.attachToReader(f.reader);
  await new Promise(done => setImmediate(done));
  f.outer.fire("unload");
  resolve({ info: { Custom: { ZTFSplitLayout: JSON.stringify(layout()) } } });
  assert.equal(await pending, false);
  assert.equal(f.internal._primaryView._iframeWindow.count("pointerdown"), 0);
  assert.equal(f.outer.count("unload"), 0);
  f.module.shutdown();
});

test("分屏尚未初始化时关闭 reader，迟到初始化不得安装监听", async () => {
  const f = fixture();
  await f.module.attachToReader(f.reader);
  const secondary = f.internal._createView(false);
  let resolve;
  secondary.initializedPromise = new Promise(done => { resolve = done; });
  // 使用新视图验证 attach 的等待路径。
  f.internal._secondaryView = f.makeView();
  f.internal._secondaryView.initializedPromise = secondary.initializedPromise;
  const pending = f.module.attachToReader(f.reader);
  await new Promise(done => setImmediate(done));
  f.outer.fire("unload");
  resolve();
  assert.equal(await pending, false);
  assert.equal(f.internal._secondaryView._iframeWindow.count("pointerdown"), 0);
  f.module.shutdown();
});

test("分屏初始化期间取消分屏，不给已被替换的视图安装监听", async () => {
  const f = fixture();
  await f.module.attachToReader(f.reader);
  const next = f.makeView();
  let resolve;
  next.initializedPromise = new Promise(done => { resolve = done; });
  f.internal._secondaryView = next;
  const pending = f.module.attachToReader(f.reader);
  await new Promise(done => setImmediate(done));
  f.internal._secondaryView = null;
  resolve();
  assert.equal(await pending, true);
  assert.equal(next._iframeWindow.count("pointerdown"), 0);
  f.module.shutdown();
});

test("页面合并返回损坏字符时，尚未入缓存的结果也不残留插件标记", async () => {
  const f = fixture();
  const v = f.internal._primaryView;
  let result;
  v._mergeProcessedPageData = function () {
    result = page();
    let reads = 0;
    Object.defineProperty(result.chars[4], "rect", {
      get() { if (++reads >= 2) throw new Error("private content"); return [80, 200, 90, 210]; },
    });
    return result;
  };
  await f.module.attachToReader(f.reader);
  down(v, 55, 205);
  assert.doesNotThrow(() => v._mergeProcessedPageData(2, page()));
  assert.deepEqual(result.chars.map(char => !!char.isolated), [false, false, false, false, true]);
  assert.equal(f.logs.length, 1);
  assert.ok(!f.logs[0].includes("private"));
  f.module.shutdown();
});



// 测试按真实内容流 hf → span → en → zh 建页，每类两个字符，边界两半均可定位。
function selectionPage(pageIndex) {
  const groups = [
    ["hf", "Hh", 50, 20], ["span", "Ss", 50, 730],
    ["en", "Ee", 50, 200], ["zh", "中译", 350, 200],
  ];
  const chars = groups.flatMap(([kind, text, x, y]) => [...text].map((c, i) => {
    const rect = [x + i * 10, y, x + i * 10 + 10, y + 10];
    return { c, kind, rect, inlineRect: rect.slice(), rotation: 0,
      pageIndex, wordBreakAfter: i === 1, lineBreakAfter: i === 1 };
  }));
  chars.forEach((char, offset) => { char.offset = offset; });
  return { chars, viewBox: [0, 0, 600, 800] };
}

// 在同一原生词/行里放入隔离空洞，保留真实字符偏移和词/行末标志。
function englishRun(view, pageIndex, text, isolatedOffsets = []) {
  const chars = view._pdfPages[pageIndex].chars;
  const template = chars[4];
  chars.splice(4, 2, ...[...text].map((c, index) => {
    const rect = [50 + index * 10, 200, 60 + index * 10, 210];
    return { ...template, c, rect, inlineRect: rect.slice(),
      isolated: isolatedOffsets.includes(index),
      wordBreakAfter: index === text.length - 1, lineBreakAfter: index === text.length - 1 };
  }));
  chars.forEach((char, offset) => { char.offset = offset; });
}

// 只模拟事件入口分支；选区运算全部调用上面固定的原生函数。
// reader.js:75510–75523、77655–77672、77803–77808、78124–78131。
function nativeSelectionDriver(view, throttled = false) {
  view._pdfPages = { 0: selectionPage(0), 1: selectionPage(1) };
  view.getSelectionPosition = nativeSelectionPosition;
  view._setSelectionRanges = nativeSetSelectionRanges;
  const navigations = [];
  const keyboardRanges = [];
  const scrollRanges = [];
  view._scrollSelectionHeadIntoView = function (ranges) {
    for (const range of ranges) {
      const length = this._pdfPages[range.pageIndex].chars.length;
      assert.ok(Number.isInteger(range.anchorOffset) && range.anchorOffset >= 0 && range.anchorOffset < length,
        "滚动参数的 anchorOffset 必须是可索引的字符位置");
      assert.ok(Number.isInteger(range.headOffset) && range.headOffset >= 0 && range.headOffset < length,
        "滚动参数的 headOffset 必须是可索引的字符位置");
    }
    scrollRanges.push(structuredClone(ranges));
    return nativeScrollSelectionHeadIntoView.call(this, ranges);
  };
  view.navigateToPosition = (position, options) => {
    assert.equal(options.block, "nearest");
    assert.ok(position.rects[0]);
    navigations.push(structuredClone(position));
  };
  view._handleCopy = nativeHandleCopy;
  view._textAnnotationFocused = () => false;
  view._selectedAnnotationIDs = [];
  view._annotations = [];
  let lastEvent;
  let releasedAnnotation;
  let releasedText;
  let popupButtons;
  const submittedAnnotations = [];
  const noteAnnotations = [];
  view.getClientRectForPopup = position => position.rects[0].map((value, index) =>
    value + (index % 2 === 0 ? position.pageIndex * 600 : 0));
  view._onAddAnnotation = annotation => submittedAnnotations.push(structuredClone(annotation));
  view._onAddToNote = annotations => noteAnnotations.push(structuredClone(annotations));
  view._onSetSelectionPopup = params => {
    view._selectionPopup = params;
    // reader.js:30947–30963：按钮闭包只读取本次菜单收到的 params.annotation。
    popupButtons = params ? {
      pickColor(type, color) { view._onAddAnnotation({ ...params.annotation, type, color }); },
      addToNote() { view._onAddToNote([params.annotation]); },
    } : null;
  };
  // 与实机一样，原生监听先于插件安装；捕获阶段排序必须真实。
  view._iframeWindow.addEventListener("pointerdown", event => {
    if (event.pointerType === "mouse") return;
    establish(event);
  }, true);
  function establish(event) {
    view.pointerDownPosition = view.pointerEventToPosition(event);
    view.action = { type: "selectText" };
    const position = view.pointerDownPosition;
    // reader.js:77701–77706：空定位先清空并返回，不进入词/行计算。
    if (!position) {
      view._setSelectionRanges();
      view.action = null;
      return;
    }
    // reader.js:77794–77808：单击的 Shift 分支沿用已有 anchor，随后切回 chars。
    if (event.detail === 1 || !event.detail) {
      if (event.shiftKey && view._selectionRanges.length) {
        view._selectionRanges = getModifiedSelectionRanges(view._pdfPages, view._selectionRanges, position);
      } else {
        view._selectionRanges = getSelectionRanges(view._pdfPages, position, position);
      }
      view.action.mode = "chars";
    } else if (event.detail === 2) {
      view._selectionRanges = getWordSelectionRanges(view._pdfPages, position, position);
      view.action.mode = "words";
    } else if (event.detail === 3) {
      view._selectionRanges = getLineSelectionRanges(view._pdfPages, position, position);
      view.action.mode = "lines";
    }
  }
  view._iframeWindow.addEventListener("mousedown", establish, true);
  const move = event => {
    if (view.action?.type === "selectText") {
      // reader.js:77969–77981：先算起点页的替代坐标，正常定位失败才采用它。
      const originalPagePosition = view.pointerEventToAltPosition(event, view.pointerDownPosition.pageIndex);
      const position = view.pointerEventToPosition(event) || originalPagePosition;
      view._selectionRanges = view.action.mode === "chars"
        ? getModifiedSelectionRanges(view._pdfPages, view._selectionRanges, position)
        : (view.action.mode === "words" ? getWordSelectionRanges : getLineSelectionRanges)(
          view._pdfPages, view.pointerDownPosition, position);
    }
  };
  view._iframeWindow.addEventListener("pointermove", throttled ? throttle(move, 50) : move, false);
  view._iframeWindow.addEventListener("pointerup", event => {
    // reader.js:78265、78414–78440：松开时定位，并用最终区间建立批注和复制文本。
    view.pointerEventToPosition(event);
    if (view._selectionRanges.length && !view._selectionRanges[0].collapsed) {
      releasedAnnotation = view._getAnnotationFromSelectionRanges(view._selectionRanges, "highlight", "#ffff00");
      releasedText = copy().data.get("text/plain");
      // reader.js:78433–78440：松开后打开菜单，批注使用原生 highlight 默认值。
      view._onSetSelectionPopup({
        rect: view.getClientRectForPopup(view._selectionRanges[0].position),
        annotation: view._getAnnotationFromSelectionRanges(view._selectionRanges, "highlight"),
      });
    }
    view.action = null;
    view.pointerDownPosition = null;
  }, false);
  // reader.js:78676–78693；原生监听先执行，赋值后立即滚动，再执行插件监听。
  view._iframeWindow.addEventListener("keydown", event => {
    if (event.shiftKey && event.key.startsWith("Arrow") && view._selectionRanges.length) {
      view._setSelectionRanges(getModifiedSelectionRanges(view._pdfPages,
        view._selectionRanges, event.key.slice(5).toLowerCase()));
      keyboardRanges.push(structuredClone(view._selectionRanges));
      view._scrollSelectionHeadIntoView(view._selectionRanges);
      view._render();
    }
  }, true);
  function copy() {
    const data = new Map();
    const result = { data };
    let prevented = false;
    let stopped = false;
    view._onSetDataTransferAnnotations = (clipboardData, annotation, textOnly) => {
      result.annotation = annotation;
      result.textOnly = textOnly;
      clipboardData.setData("text/plain", annotation.text);
    };
    view._handleCopy({
      preventDefault() { prevented = true; },
      stopPropagation() { stopped = true; },
      clipboardData: { setData(type, value) { data.set(type, value); } },
    });
    assert.ok(prevented && stopped, "执行原生复制事件处理");
    return result;
  }
  return {
    copy,
    navigations,
    keyboardRanges,
    scrollRanges,
    submittedAnnotations,
    noteAnnotations,
    submitPopup(type = "highlight", color = "#ffff00") {
      assert.ok(popupButtons, "菜单应已打开");
      popupButtons.pickColor(type, color);
    },
    addPopupToNote() {
      assert.ok(popupButtons, "菜单应已打开");
      popupButtons.addToNote();
    },
    down(page, x, y, detail = 1, pointerType = "mouse", shiftKey = false, emptyStart = false) {
      const event = { clientX: page * 600 + x, clientY: y, button: 0, pointerType, shiftKey, detail };
      lastEvent = event;
      view._iframeWindow.fire("pointerdown", { ...event, type: "pointerdown" });
      if (pointerType === "mouse") view._iframeWindow.fire("mousedown", { ...event, type: "mousedown", detail });
      if (emptyStart) assert.ok(view._selectionRanges.every(range => range.collapsed), "合法空起点不得借用异类字符");
      else if (!shiftKey) assert.equal(view._selectionRanges.length, 1);
      if (detail === 1 && !shiftKey && !emptyStart) {
        assert.equal(view._selectionRanges[0].collapsed, true, "mousedown 建立原生折叠选区");
      }
    },
    move(page, x, y) {
      lastEvent = { clientX: page * 600 + x, clientY: y, buttons: 1 };
      view._iframeWindow.fire("pointermove", { ...lastEvent, type: "pointermove" });
    },
    outside(x, y) {
      lastEvent = { clientX: x, clientY: y, buttons: 1, offPage: true };
      view._iframeWindow.fire("pointermove", { ...lastEvent, type: "pointermove" });
    },
    up() {
      view._iframeWindow.fire("pointerup", { ...lastEvent, type: "pointerup", button: 0 });
    },
    key(direction) {
      assert.doesNotThrow(() => view._iframeWindow.fire("keydown", { key: `Arrow${direction}`, shiftKey: true }));
      for (const range of view._selectionRanges) {
        const length = view._pdfPages[range.pageIndex].chars.length;
        assert.ok(Number.isInteger(range.anchorOffset) && range.anchorOffset >= 0 && range.anchorOffset <= length);
        assert.ok(Number.isInteger(range.headOffset) && range.headOffset >= 0 && range.headOffset <= length);
      }
    },
    released(expected) {
      assert.equal(releasedText, expected, "松开时复制得到完整文本");
      assert.ok(releasedAnnotation, "松开时能够建立高亮批注");
      assert.equal(releasedAnnotation.text, expected);
      const pages = [...new Set(view._selectionRanges.map(range => range.position.pageIndex))];
      assert.deepEqual(releasedAnnotation.position.rects, view._selectionRanges
        .filter(range => range.position.pageIndex === pages[0]).flatMap(range => range.position.rects));
      if (pages.length === 2) assert.deepEqual(releasedAnnotation.position.nextPageRects,
        view._selectionRanges.filter(range => range.position.pageIndex === pages[1])
          .flatMap(range => range.position.rects));
    },
    check(kind, expected, expectedRects) {
      const ranges = view._selectionRanges;
      // 按页合并后的 offset 包围异类空洞，实际选中的字符由文本和矩形表达。
      const chars = getCharsFromSelectionRanges(view._pdfPages, ranges).filter(char => !char.isolated);
      assert.ok(chars.length, "不得用空选区冒充隔离成功");
      assert.ok(chars.every(char => char.kind === kind), `最终选区只含 ${kind}，实际 ${chars.map(c => c.kind)}`);
      assert.equal(ranges.filter(range => !range.collapsed).map(range => range.text).join("|"), expected);
      assert.equal(ranges.filter(range => range.anchor).length, 1);
      assert.equal(ranges.filter(range => range.head).length, 1);
      assert.equal(new Set(ranges.map(range => range.pageIndex)).size, ranges.length, "每页最多一个区间");
      assert.ok(ranges.every(range => !range.collapsed), "非空最终选区不包含空页区间");
      for (const range of ranges) {
        assert.equal(range.pageIndex, range.position.pageIndex, "保留原生区间的顶层 pageIndex");
        assert.equal(range.sortIndex, getSortIndex(view._pdfPages, range.position));
      }
      assert.deepEqual(ranges.flatMap(range => range.position.rects),
        expectedRects || ranges.flatMap(range => getRectsFromChars(view._pdfPages[range.position.pageIndex].chars.slice(
          Math.min(range.anchorOffset, range.headOffset), Math.max(range.anchorOffset, range.headOffset)))));
    },
  };
}

test("T7：两侧存在原生隔离字符时，英文跨页仍隔离本页中文", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  // 最小复现：直接保留 worker 的原生标记，排除页面合并和页高的影响。
  for (const index of [0, 1]) {
    view._pdfPages[index].chars = view._pdfPages[index].chars.filter(char =>
      char.c === "E" || char.c === "中");
    view._pdfPages[index].chars.forEach((char, offset) => {
      char.offset = offset;
      char.isolated = true;
      char.wordBreakAfter = char.lineBreakAfter = true;
    });
  }
  await f.module.attachToReader(f.reader);
  driver.down(0, 51, 205);
  driver.move(1, 59, 205);
  assert.ok(view._pdfPages[0].chars.filter(char => char.kind === "zh")
    .every(char => char.isolated), "从第 N 页英文拖到 N+1 页英文，本页中文必须保持 isolated");
  driver.check("en", "E|E");
  assert.deepEqual(f.logs, [], "复现不依赖钩子回退");
  f.module.shutdown();
});

for (const heights of [[800, 800], [800, 1200], [1200, 800]]) {
  for (const kind of ["en", "zh"]) {
    for (const reverse of [false, true]) {
      for (const timing of ["起拖前", "起点页先迟到", "终点页先迟到"]) {
        test(`T7：${kind} ${reverse ? "反向" : "正向"}跨页，页高 ${heights.join("/")}，原生隔离${timing}`, async () => {
          const metadataLayout = layout();
          for (const index of [0, 1]) {
            for (const region of metadataLayout.pages[index].regions) {
              region.r[1] += heights[index] - 800;
              region.r[3] += heights[index] - 800;
            }
          }
          const f = fixture({ info: { Custom: { ZTFSplitLayout: JSON.stringify(metadataLayout) } } });
          const view = f.internal._primaryView;
          const driver = nativeSelectionDriver(view);
          // 加高页的字符和元数据一起移入增加的高度，不能只改未用到的 viewBox。
          for (const index of [0, 1]) {
            const page = view._pdfPages[index];
            page.viewBox[3] = heights[index];
            for (const char of page.chars) {
              for (const rect of [char.rect, char.inlineRect]) {
                rect[1] += heights[index] - 800;
                rect[3] += heights[index] - 800;
              }
            }
          }
          const originalPages = structuredClone(view._pdfPages);
          // 使用原生合并入口；覆盖复用字符与 {...char, isolated: true} 的复制路径。
          const mergeNativeIsolation = index => {
            view._processedPageIsolatedCharIndexes[index] = [0, 1, 2, 3, 4, 5, 6, 7];
            view._pdfPages[index] = view._mergeProcessedPageData(index, view._pdfPages[index]);
          };
          if (timing === "起拖前") for (const index of [0, 1]) mergeNativeIsolation(index);
          await f.module.attachToReader(f.reader);
          const startPage = reverse ? 1 : 0;
          const headPage = 1 - startPage;
          const x = kind === "en" ? 50 : 350;
          driver.down(startPage, x + (reverse ? 19 : 1), heights[startPage] - 595);
          driver.move(headPage, x + (reverse ? 1 : 19), heights[headPage] - 595);
          const assertOtherRegionsIsolated = () => {
            for (const index of [0, 1]) {
              assert.ok(view._pdfPages[index].chars.filter(char => char.kind !== kind)
                .every(char => char.isolated), "另一侧、跨栏和页眉页脚不能被开放");
            }
          };
          assertOtherRegionsIsolated();
          if (timing !== "起拖前") {
            const order = timing === "起点页先迟到" ? [startPage, headPage] : [headPage, startPage];
            for (const index of order) {
              mergeNativeIsolation(index);
              assertOtherRegionsIsolated();
            }
          }
          const text = kind === "en" ? "Ee|Ee" : "中译|中译";
          const rects = [0, 1].map(index => [x, heights[index] - 600, x + 20, heights[index] - 590]);
          driver.check(kind, text, rects);
          driver.up();
          driver.check(kind, text, rects);
          driver.released(text.replaceAll("|", " "));
          checkPopupMatchesCopy(driver, view, text.replaceAll("|", " "));
          assert.deepEqual(f.logs, [], "整条路径无 fallback");
          f.module.shutdown();
          for (const index of [0, 1]) {
            assert.deepEqual(view._pdfPages[index], { ...originalPages[index],
              chars: originalPages[index].chars.map(char => ({ ...char, isolated: true })) },
            "卸载保留原生隔离，不残留插件按侧改写的标记");
          }
        });
      }
    }
  }
}

test("原生夹具可复现原实现的三个错误分组", () => {
  for (const [start, end, kind] of [
    [[0, 69, 205], [1, 69, 205], "en"],
    [[0, 51, 205], [0, 51, 735], "en"],
    [[0, 69, 735], [0, 69, 205], "span"],
  ]) {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    // 原实现只有坐标分类和标记，没有锚点校正及最终区间过滤。
    view._iframeWindow.addEventListener("pointerdown", () => {
      for (const page of Object.values(view._pdfPages)) {
        for (const char of page.chars) char.isolated = char.kind !== kind;
      }
    }, true);
    driver.down(...start);
    driver.move(...end);
    const chars = getCharsFromSelectionRanges(view._pdfPages, view._selectionRanges);
    assert.ok(chars.length && chars.some(char => char.kind !== kind));
    f.module.shutdown();
  }
});

for (const [name, start, end, kind, expected] of [
  ["英文末字右半跨页", [0, 69, 205], [1, 69, 205], "en", "e|Ee"],
  ["英文首字左半反向到跨栏", [0, 51, 205], [0, 51, 735], "en", "E"],
  ["跨栏末字右半到英文", [0, 69, 735], [0, 69, 205], "span", "s"],
  ["英文首字左半反向跨页", [1, 51, 205], [0, 51, 205], "en", "Ee|E"],
  ["反向跨页首页末字右半形成折叠区间", [1, 51, 205], [0, 369, 205], "en", "E"],
  ["中文末字右半跨页", [0, 369, 205], [1, 369, 205], "zh", "译|中译"],
  ["中文首字左半反向跨页", [1, 351, 205], [0, 351, 205], "zh", "中译|中"],
  ["跨栏首字左半反向跨页仍只选本元素", [1, 51, 735], [0, 51, 735], "span", "S"],
  ["跨栏末字右半跨页仍只选本元素", [0, 69, 735], [1, 69, 735], "span", "s"],
  ["页眉末字右半跨页", [0, 69, 25], [1, 69, 25], "hf", "h|Hh"],
  ["页眉首字左半反向跨页", [1, 51, 25], [0, 51, 25], "hf", "Hh|H"],
]) {
  test(`原生最终选区：${name}`, async () => {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    await f.module.attachToReader(f.reader);
    driver.down(...start);
    driver.move(...end);
    driver.check(kind, expected);
    driver.up();
    driver.check(kind, expected);
    driver.released(expected.replaceAll("|", " "));
    assert.equal(f.logs.length, 0);
    f.module.shutdown();
  });
}

for (const [kind, point] of [["span", [51, 735]], ["en", [51, 205]],
  ["zh", [351, 205]], ["hf", [51, 25]]]) {
  for (const emptyPage of [false, true]) {
    for (const action of ["单击", "拖选", "双击", "三击", "Shift 单击", "空起点后 Shift 单击",
      "键盘 Left", "键盘 Right", "键盘 Up", "键盘 Down", "迟到合并"]) {
      test(`合法空起点：${kind}${emptyPage ? "整页无字符" : "区域无字符"}${action}后仍隔离`, async () => {
        const f = fixture();
        const view = f.internal._primaryView;
        const driver = nativeSelectionDriver(view);
        view._pdfPages[0].chars = view._pdfPages[0].chars.filter(char => !emptyPage && char.kind !== kind);
        view._pdfPages[0].chars.forEach((char, offset) => { char.offset = offset; });
        await f.module.attachToReader(f.reader);
        const hooks = [view.pointerEventToPosition, view._mergeProcessedPageData,
          Object.getOwnPropertyDescriptor(view, "_selectionRanges").set];
        const detail = action === "双击" ? 2 : action === "三击" ? 3 : 1;
        assert.doesNotThrow(() => driver.down(0, ...point, detail, "mouse", action === "Shift 单击", true));
        if (["拖选", "双击", "三击"].includes(action)) {
          for (const end of [[0, 369, 205], [1, 51, 205], [0, ...point]]) {
            assert.doesNotThrow(() => driver.move(...end));
          }
        }
        driver.up();
        if (action === "空起点后 Shift 单击") {
          driver.down(1, 369, 205, 1, "mouse", true, true);
          driver.up();
        }
        if (action.startsWith("键盘")) driver.key(action.split(" ")[1]);
        if (action === "迟到合并") {
          view._pdfPages[0] = view._mergeProcessedPageData(0, view._pdfPages[0]);
          view._pdfPages[1] = view._mergeProcessedPageData(1, view._pdfPages[1]);
        }
        assert.ok(view._selectionRanges.every(range => range.collapsed), "无字符区域保持空选区");
        assert.ok(view._pdfPages[1].chars.every(char => char.isolated ===
          (kind === "span" || char.kind !== kind)), "空起点仍保留区域类别及单个跨栏元素边界");
        assert.equal(driver.copy().data.get("text/plain"), undefined);
        assert.equal(view._selectionPopup, undefined);
        assert.deepEqual(driver.submittedAnnotations, []);
        assert.deepEqual(driver.noteAnnotations, []);
        assert.deepEqual(f.logs.map(JSON.parse), [{
          event: "reader_selection_fallback", reason: "selection_empty_start", count: 1,
        }], "空起点记录固定脱敏诊断，不得报告接口故障");
        assert.equal(await f.module.attachToReader(f.reader), true);
        assert.equal(view._iframeWindow.count("pointerdown"), 2);
        assert.deepEqual([view.pointerEventToPosition, view._mergeProcessedPageData,
          Object.getOwnPropertyDescriptor(view, "_selectionRanges").set], hooks, "空内容不能撤销或重装钩子");
        // 第 9 轮精确复现的后半段；复制、批注及菜单按钮必须仍只含英文。
        driver.down(1, 51, 205);
        driver.move(1, 369, 205);
        driver.up();
        driver.check("en", "Ee", [[50, 200, 70, 210]]);
        driver.released("Ee");
        checkPopupMatchesCopy(driver, view, "Ee");
        assert.equal(f.logs.length, 1);
        f.module.shutdown();
      });
    }
  }
}

for (const kind of ["en", "zh"]) {
  test(`合法空起点：${kind}不能借用同页另一区域的同类字符`, async () => {
    const data = layout();
    data.pages[0].regions.find(region => region.k === kind).r[1] = 300;
    data.pages[0].regions.push({ k: kind, r: kind === "en" ? [0, 100, 300, 300] : [300, 100, 600, 300] });
    const f = fixture({ info: { Custom: { ZTFSplitLayout: JSON.stringify(data) } } });
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    await f.module.attachToReader(f.reader);
    driver.down(0, kind === "en" ? 51 : 351, 500, 2, "mouse", false, true);
    driver.move(0, kind === "en" ? 69 : 369, 205);
    driver.up();
    assert.ok(view._selectionRanges.every(range => range.collapsed));
    assert.equal(await f.module.attachToReader(f.reader), true);
    assert.equal(JSON.parse(f.logs[0]).reason, "selection_empty_start");
    driver.down(1, 51, 205);
    driver.move(1, 369, 205);
    driver.up();
    driver.check("en", "Ee");
    checkPopupMatchesCopy(driver, view, "Ee");
    f.module.shutdown();
  });
}

for (const detail of [2, 3]) {
  test(`合法空数据：第 ${detail} 次点击没有词行单元时保持空选区和钩子`, async () => {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    for (const char of view._pdfPages[0].chars) {
      char.wordBreakAfter = false;
      char.lineBreakAfter = false;
    }
    await f.module.attachToReader(f.reader);
    driver.down(0, 51, 205, detail, "mouse", false, true);
    driver.up();
    driver.key("Right");
    assert.ok(view._selectionRanges.every(range => range.collapsed));
    assert.equal(await f.module.attachToReader(f.reader), true);
    assert.equal(JSON.parse(f.logs[0]).reason, "selection_empty_unit");
    driver.down(1, 51, 205);
    driver.move(1, 369, 205);
    driver.up();
    driver.check("en", "Ee");
    checkPopupMatchesCopy(driver, view, "Ee");
    f.module.shutdown();
  });
  test(`合法空数据：第 ${detail} 次点击后拖入无词行单元的页面不影响隔离`, async () => {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    for (const char of view._pdfPages[1].chars) {
      char.wordBreakAfter = false;
      char.lineBreakAfter = false;
    }
    await f.module.attachToReader(f.reader);
    driver.down(0, 51, 205, detail);
    assert.doesNotThrow(() => driver.move(1, 369, 205));
    driver.up();
    driver.check("en", "Ee|Ee");
    checkPopupMatchesCopy(driver, view, "Ee Ee");
    assert.equal(await f.module.attachToReader(f.reader), true);
    assert.deepEqual(f.logs.map(line => JSON.parse(line).reason), ["selection_empty_unit"]);
    f.module.shutdown();
  });
}

test("合法空数据：滚动跳过空字符页；起点页合并为空也不撤销钩子", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  view._pdfPages[0].chars = [];
  await f.module.attachToReader(f.reader);
  driver.down(1, 51, 205);
  driver.move(1, 369, 205);
  driver.up();
  const ranges = [{ pageIndex: 0, anchorOffset: 0, headOffset: 0, head: true }];
  assert.doesNotThrow(() => view._scrollSelectionHeadIntoView(ranges));
  assert.deepEqual(driver.scrollRanges.at(-1), []);
  view._pdfPages[1] = view._mergeProcessedPageData(1, { ...view._pdfPages[1], chars: [] });
  assert.ok(view._selectionRanges.every(range => range.collapsed));
  assert.equal(view._selectionPopup, undefined);
  driver.key("Right");
  driver.down(0, 51, 205, 1, "mouse", true, true);
  driver.move(0, 51, 205);
  driver.up();
  assert.equal(await f.module.attachToReader(f.reader), true);
  assert.deepEqual(f.logs.map(line => JSON.parse(line).reason), ["selection_empty_page_scroll", "selection_empty_start"]);
  // 页面随后重新提供字符，新的拖选必须恢复正常隔离。
  view._pdfPages[1] = view._mergeProcessedPageData(1, selectionPage(1));
  driver.down(1, 51, 205);
  driver.move(1, 369, 205);
  driver.up();
  driver.check("en", "Ee");
  checkPopupMatchesCopy(driver, view, "Ee");
  f.module.shutdown();
});

// 刻意把异类字符放得更近：区域内空白不能借用全页最近字符的类别或词/行。
function whitespaceFixture(kind) {
  const data = layout();
  if (kind === "outside") {
    for (const page of data.pages) {
      for (const region of page.regions) {
        if (region.k === "en" || region.k === "zh") region.r[1] = 150;
      }
    }
  }
  const f = fixture({ info: { Custom: { ZTFSplitLayout: JSON.stringify(data) } } });
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  for (const page of Object.values(view._pdfPages)) {
    if (kind === "zh" || kind === "span" || kind === "hf") {
      const x = kind === "hf" ? 50 : 280;
      const y = kind === "span" ? 680 : kind === "hf" ? 100 : 200;
      page.chars.slice(4, 6).forEach((char, index) => {
        char.rect = [x + index * 10, y, x + index * 10 + 10, y + 10];
        char.inlineRect = char.rect.slice();
      });
    }
  }
  return { f, view, driver };
}

for (const [kind, point, forwardEnd, reverseEnd, text, forwardText, reverseText] of [
  ["en", [299, 205], [369, 205], [51, 205], "Ee", "e", "Ee"],
  ["zh", [301, 205], [369, 205], [51, 735], "中译", "中译", "中"],
  ["span", [299, 701], [369, 205], [51, 25], "Ss", "s", "Ss"],
  ["hf", [69, 99], [369, 205], [51, 25], "Hh", "h", "Hh"],
  ["outside", [351, 120], [369, 205], [51, 735], "中译", "中译", "中"],
]) {
  for (const reverse of [false, true]) {
    for (const crossPage of [false, true]) {
      for (const detail of [1, 2, 3]) {
        test(`区域空白起点：${kind}${reverse ? "反向" : "正向"}${crossPage ? "跨页" : "同页"}${detail === 1 ? "拖选" : detail === 2 ? "双击拖选" : "三击拖选"}`, async () => {
          const { f, view, driver } = whitespaceFixture(kind);
          await f.module.attachToReader(f.reader);
          const startPage = reverse && crossPage ? 1 : 0;
          driver.down(startPage, ...point, detail);
          const selectedKind = kind === "outside" ? "zh" : kind;
          if (detail > 1) driver.check(selectedKind, text);
          const end = reverse ? reverseEnd : forwardEnd;
          driver.move(crossPage ? 1 - startPage : startPage, ...end);
          let expected = detail === 1 ? (reverse ? reverseText : forwardText) : text;
          if (crossPage && kind !== "span") {
            // 终点取该类首/末字符，使跨页两侧都存在非空片段。
            const x = selectedKind === "zh" ? (reverse ? 351 : 369) : (reverse ? 51 : 69);
            const y = selectedKind === "hf" ? 25 : 205;
            driver.move(1 - startPage, x, y);
            expected = reverse ? `${text}|${detail === 1 ? reverseText : text}`
              : `${detail === 1 ? forwardText : text}|${text}`;
          }
          driver.check(selectedKind, expected);
          driver.up();
          driver.check(selectedKind, expected);
          driver.released(expected.replaceAll("|", " "));
          checkPopupMatchesCopy(driver, view, expected.replaceAll("|", " "));
          assert.equal(f.logs.length, 0);
          f.module.shutdown();
        });
      }
    }
  }
}

test("第 8 轮精确复现：英文行尾空白直接跨页后松开仍复制英文", async () => {
  const { f, view, driver } = whitespaceFixture("en");
  await f.module.attachToReader(f.reader);
  driver.down(0, 299, 205);
  driver.move(1, 69, 205);
  driver.up();
  driver.check("en", "e|Ee");
  driver.released("e Ee");
  checkPopupMatchesCopy(driver, view, "e Ee");
  // 迟到合并不得重新借用位于中文侧的全页最近字符。
  view._pdfPages[0] = view._mergeProcessedPageData(0, view._pdfPages[0]);
  driver.check("en", "e|Ee");
  checkPopupMatchesCopy(driver, view, "e Ee");
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

for (const [kind, point, selectedKind, text, partial, keyboardText] of [
  ["en", [299, 205], "en", "Ee", "e", "e"],
  ["zh", [301, 205], "zh", "中译", "中译", "中"],
  ["span", [299, 701], "span", "Ss", "s", "s"],
  ["hf", [69, 99], "hf", "Hh", "h", "h"],
  ["outside", [351, 120], "zh", "中译", "中译", "中"],
]) {
  for (const detail of [1, 2, 3]) {
    test(`区域空白起点：${kind}第 ${detail} 次点击后直接键盘扩选`, async () => {
      const { f, view, driver } = whitespaceFixture(kind);
      await f.module.attachToReader(f.reader);
      driver.down(0, ...point, detail);
      driver.up();
      driver.key(detail === 1 ? "Right" : "Left");
      // 区域内空白的原生 head 可能落在异类词/行中；首步仍须保持起点类别。
      const expected = detail === 1 ? keyboardText : kind === "outside" ? text[0] : text;
      driver.check(selectedKind, expected);
      assert.equal(driver.copy().data.get("text/plain"), expected);
      assert.equal(f.logs.length, 0);
      f.module.shutdown();
    });

    for (const existingAnchor of [false, true]) {
      test(`区域空白起点：${kind}第 ${detail} 次点击${existingAnchor ? "后 Shift 单击沿用起点" : "带 Shift 建立新起点"}`, async () => {
        const { f, view, driver } = whitespaceFixture(kind);
        await f.module.attachToReader(f.reader);
        driver.down(0, ...point, detail, "mouse", !existingAnchor);
        driver.move(0, 369, 205);
        const initial = detail === 1 ? partial : text;
        driver.check(selectedKind, initial);
        driver.up();
        if (existingAnchor) {
          // Shift 单击异类区域不能重新确定起点类别，双/三击须切回字符扩选。
          driver.down(1, selectedKind === "zh" ? 299 : 369, 205, 1, "mouse", true);
          driver.up();
        }
        const expected = existingAnchor && selectedKind !== "zh" && kind !== "span"
          ? `${initial}|${text}` : initial;
        driver.check(selectedKind, expected);
        driver.released(expected.replaceAll("|", " "));
        checkPopupMatchesCopy(driver, view, expected.replaceAll("|", " "));
        assert.equal(f.logs.length, 0);
        f.module.shutdown();
      });
    }
  }
}

for (const reverse of [false, true]) {
  for (const startFirst of [false, true]) {
    test(`原生最终选区：页眉原生隔离迟到${reverse ? "反向" : "正向"}，起点页${startFirst ? "先" : "后"}合并后直接松开`, async () => {
      const f = fixture();
      const view = f.internal._primaryView;
      const driver = nativeSelectionDriver(view);
      await f.module.attachToReader(f.reader);
      const startPage = reverse ? 1 : 0;
      driver.down(startPage, reverse ? 51 : 69, 25);
      driver.move(1 - startPage, reverse ? 51 : 69, 25);
      driver.check("hf", reverse ? "Hh|H" : "h|Hh");
      // 两种拖选方向、两种合并顺序均须保留原始 head，合并后直接松开。
      for (const index of startFirst ? [startPage, 1 - startPage] : [1 - startPage, startPage]) {
        view._processedPageIsolatedCharIndexes[index] = [0, 1];
        view._pdfPages[index] = view._mergeProcessedPageData(index, view._pdfPages[index]);
      }
      driver.check("hf", reverse ? "Hh|H" : "h|Hh");
      driver.up();
      driver.check("hf", reverse ? "Hh|H" : "h|Hh");
      driver.released(reverse ? "Hh H" : "h Hh");
      assert.equal(f.logs.length, 0);
      f.module.shutdown();
      assert.ok(view._pdfPages[0].chars.slice(0, 2).every(char => char.isolated));
      assert.ok(view._pdfPages[0].chars.slice(2).every(char => !char.isolated));
    });
  }
}

test("原生最终选区：跨页后折返页外空白，采用 pointerEventToAltPosition 并直接松开", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  await f.module.attachToReader(f.reader);
  driver.down(0, 69, 205);
  driver.move(1, 69, 205);
  driver.check("en", "e|Ee");
  driver.outside(-10, 735);
  driver.check("en", "Ee");
  driver.up();
  driver.check("en", "Ee");
  driver.released("Ee");
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

test("原生最终选区：跨页后折返，固定起点不随过滤后的 anchor 漂移", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  await f.module.attachToReader(f.reader);
  driver.down(0, 69, 205);
  driver.move(1, 69, 205);
  driver.check("en", "e|Ee");
  driver.move(0, 51, 735);
  driver.check("en", "Ee");
  driver.move(1, 69, 205);
  driver.check("en", "e|Ee");
  f.module.shutdown();
});

test("原生最终选区：回到固定插入点产生合法折叠区间，随后仍可继续扩选", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  await f.module.attachToReader(f.reader);
  driver.down(0, 69, 205);
  driver.move(1, 69, 205);
  driver.move(0, 69, 205);
  const range = view._selectionRanges[0];
  assert.equal(view._selectionRanges.length, 1);
  assert.equal(range.collapsed, true);
  assert.equal(range.anchor, true);
  assert.equal(range.head, true);
  assert.equal(range.anchorOffset, range.headOffset);
  assert.equal(range.text, "");
  assert.deepEqual(range.position.rects, []);
  assert.equal(view._getAnnotationFromSelectionRanges(view._selectionRanges, "highlight"), null);
  driver.move(1, 69, 205);
  driver.check("en", "e|Ee");
  f.module.shutdown();
});

test("键盘页尾：中文双击后左缩在原生滚动前采用新 head", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  await f.module.attachToReader(f.reader);
  driver.down(0, 369, 205, 2);
  driver.up();
  assert.equal(view._selectionRanges[0].headOffset, 8);
  assert.doesNotThrow(() => driver.key("Left"));
  assert.equal(driver.keyboardRanges.at(-1)[0].headOffset, 7, "原生滚动前已经推进 head，不能等插件监听补算");
  driver.check("zh", "中");
  assert.deepEqual(driver.navigations.at(-1), { pageIndex: 0, rects: [[360, 200, 370, 210]] });
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

test("键盘滚动：安全区间克隆失败时脱敏记录并撤销钩子，不影响阅读器", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  const originalScroll = view._scrollSelectionHeadIntoView;
  await f.module.attachToReader(f.reader);
  driver.down(0, 369, 205, 2);
  driver.up();
  const clone = f.context.Components.utils.cloneInto;
  f.context.Components.utils.cloneInto = (value, target) => {
    if (Array.isArray(value) && value.some(range => range.text === "中" && range.headOffset === 7)) {
      throw new Error("secret-key PDF原文 C:/private/paper.pdf user@example.com");
    }
    return clone(value, target);
  };
  // 单独调用滚动入口，只让安全参数的克隆失败，选区本身的克隆仍正常。
  assert.doesNotThrow(() => view._scrollSelectionHeadIntoView([
    { pageIndex: 0, anchorOffset: 6, headOffset: 7, head: true, text: "中" },
  ]));
  assert.equal(view._scrollSelectionHeadIntoView, originalScroll);
  assert.deepEqual(f.logs.map(JSON.parse), [
    { event: "reader_selection_fallback", reason: "selection_scroll_unavailable", count: 1 },
  ]);
  assert.equal(driver.navigations.length, 0);
  f.module.shutdown();
});

for (const [kind, text, x, y] of [
  ["zh", "中译", 350, 205], ["en", "Ee", 50, 205], ["span", "Ss", 50, 735],
]) {
  for (const detail of [1, 2, 3]) {
    for (const direction of ["Left", "Right", "Up", "Down"]) {
      test(`键盘页尾：${kind}${detail === 1 ? "拖选" : detail === 2 ? "双击" : "三击"}松开后 Shift+Arrow${direction}`, async () => {
        const f = fixture();
        const view = f.internal._primaryView;
        const driver = nativeSelectionDriver(view);
        // 将当前类别放在真实字符数组末尾，不能只把英文词尾当作页面末尾。
        const last = view._pdfPages[0].chars.findLastIndex(char => char.kind === kind);
        view._pdfPages[0].chars.splice(last + 1);
        await f.module.attachToReader(f.reader);
        const scroll = view._scrollSelectionHeadIntoView;
        driver.down(0, x + (detail === 1 ? 1 : 19), y, detail);
        if (detail === 1) driver.move(0, x + 19, y);
        driver.up();
        driver.check(kind, text);
        assert.equal(view._selectionRanges[0].headOffset, last + 1);
        assert.doesNotThrow(() => driver.key(direction));
        const expected = direction === "Left" || direction === "Up" && detail === 1
          ? text[0] : direction === "Up" ? "" : text;
        if (expected) {
          driver.check(kind, expected, [[x, y - 5, x + expected.length * 10, y + 5]]);
          checkPopupMatchesCopy(driver, view, expected);
        } else {
          assert.equal(view._selectionRanges.length, 1);
          assert.equal(view._selectionRanges[0].collapsed, true);
          assert.equal(view._selectionRanges[0].text, "");
          assert.deepEqual(view._selectionRanges[0].position.rects, []);
        }
        assert.equal(driver.scrollRanges.length, 1, "原生键盘监听确实走过滚动入口");
        assert.equal(f.logs.length, 0);
        f.module.shutdown();
        assert.notEqual(view._scrollSelectionHeadIntoView, scroll, "卸载恢复滚动入口");
      });
    }
  }
}

for (const detail of [2, 3]) {
  test(`原生最终选区：${detail === 2 ? "双击选词" : "三击选行"}仍按固定起点类别重建`, async () => {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    await f.module.attachToReader(f.reader);
    driver.down(0, 69, 205, detail);
    driver.check("en", "Ee");
    driver.up();
    driver.key("Left");
    driver.check("en", "E");
    driver.key("Right");
    driver.check("en", "Ee");
    driver.down(0, 69, 205, detail);
    driver.move(1, 369, 205);
    driver.check("en", "Ee|Ee");
    driver.up();
    driver.released("Ee Ee");
    // 键盘接手后保留展开区间的 anchor，不能重新缩到被点击字符的半字符插入点。
    for (let i = 0; i < 3; i++) driver.key("Left");
    driver.check("en", "Ee|E");
    driver.key("Right");
    driver.check("en", "Ee|Ee");
    assert.equal(f.logs.length, 0);
    f.module.shutdown();
  });
}

test("原生最终选区：触摸原生捕获监听先执行，仍在首次赋值前固定起点", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  await f.module.attachToReader(f.reader);
  driver.down(1, 51, 205, 1, "touch");
  driver.move(0, 369, 205);
  driver.check("en", "E");
  driver.up();
  driver.released("E");
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

test("原生最终选区：键盘扩选保留落在异类字符中的 head，能够继续跨页和折返", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  await f.module.attachToReader(f.reader);
  driver.down(0, 69, 205);
  driver.up();
  driver.key("Right");
  driver.check("en", "e");
  // 原始 head 继续经过中文、下一页页眉与跨栏；可见区间的 head 始终在英文边缘。
  for (let i = 0; i < 6; i++) driver.key("Right");
  driver.check("en", "e|E");
  driver.key("Right");
  driver.check("en", "e|Ee");
  for (let i = 0; i < 9; i++) driver.key("Left");
  driver.check("en", "e");
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

for (const [direction, expected] of [["Up", "e"], ["Down", "e"]]) {
  test(`原生最终选区：Shift+Arrow${direction} 按原生行偏移重算`, async () => {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    await f.module.attachToReader(f.reader);
    driver.down(0, 69, 205);
    driver.up();
    driver.key(direction);
    driver.check("en", expected);
    assert.equal(f.logs.length, 0);
    f.module.shutdown();
  });
}

test("原生最终选区：节流回调未执行就松开，批注和复制已使用松开位置", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view, true);
  await f.module.attachToReader(f.reader);
  driver.down(0, 69, 205);
  driver.move(1, 69, 205);
  driver.move(0, 51, 735);
  driver.check("en", "e|Ee");
  driver.up();
  driver.check("en", "Ee");
  driver.released("Ee");
  await new Promise(resolve => setTimeout(resolve, 70));
  driver.check("en", "Ee");
  f.module.shutdown();
});

test("原生最终选区：起点原生类别迟到后立即过滤，随后继续拖选", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  await f.module.attachToReader(f.reader);
  driver.down(0, 51, 205);
  driver.move(1, 69, 205);
  driver.check("en", "Ee|Ee");
  view._processedPageIsolatedCharIndexes[0] = [4];
  view._pdfPages[0] = view._mergeProcessedPageData(0, view._pdfPages[0]);
  driver.check("en", "E");
  driver.move(1, 69, 205);
  driver.check("en", "E");
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

test("原生最终选区：拖选中新增页面按当前起点补标记", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  delete view._pdfPages[1];
  await f.module.attachToReader(f.reader);
  driver.down(0, 69, 205);
  view._pdfPages[1] = view._mergeProcessedPageData(1, selectionPage(1));
  driver.move(1, 69, 205);
  driver.check("en", "e|Ee");
  f.module.shutdown();
});

for (const detail of [2, 3]) {
  test(`原生最终选区：${detail === 2 ? "双击" : "三击"} Eex 的 x 保留完整同类 ex`, async () => {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    englishRun(view, 0, "Eex", [0]);
    await f.module.attachToReader(f.reader);
    driver.down(0, 79, 205, detail);
    driver.check("en", "ex");
    assert.equal(view.action.mode, detail === 2 ? "words" : "lines");
    assert.equal(view._selectionRanges[0].anchorOffset, 5);
    assert.equal(view._selectionRanges[0].headOffset, 7);
    driver.up();
    driver.check("en", "ex");
    driver.released("ex");
    assert.equal(f.logs.length, 0);
    f.module.shutdown();
  });

  test(`原生最终选区：${detail === 2 ? "双击" : "三击"}反向展开只剔除尾部异类字符`, async () => {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    englishRun(view, 0, "xeE", [2]);
    await f.module.attachToReader(f.reader);
    driver.down(0, 51, 205, detail);
    driver.move(0, 51, 25);
    driver.check("en", "xe");
    assert.equal(view._selectionRanges[0].anchorOffset, 6);
    assert.equal(view._selectionRanges[0].headOffset, 4);
    driver.up();
    driver.released("xe");
    assert.equal(f.logs.length, 0);
    f.module.shutdown();
  });

  for (const [x, expected] of [[51, "Ee"], [61, "Ee|E"]]) {
    test(`原生最终选区：${detail === 2 ? "双击" : "三击"}后 Shift 单击 ${x} 回到字符模式`, async () => {
      const f = fixture();
      const view = f.internal._primaryView;
      const driver = nativeSelectionDriver(view);
      await f.module.attachToReader(f.reader);
      driver.down(0, 69, 205, detail);
      driver.up();
      driver.down(1, x, 205, 1, "mouse", true);
      assert.equal(view.action.mode, "chars", "原生 Shift 分支必须切回 chars");
      driver.check("en", expected);
      driver.up();
      driver.check("en", expected);
      driver.released(expected.replaceAll("|", " "));
      driver.key("Right");
      driver.check("en", x === 51 ? "Ee|E" : "Ee|Ee");
      assert.equal(f.logs.length, 0);
      f.module.shutdown();
    });
  }
}

for (const text of ["Eex", "Eexzy"]) {
  for (const reverse of [false, true]) {
    test(`原生最终选区：同页${text.length === 3 ? "两" : "三"}片段${reverse ? "反向" : "正向"}合并后由原生定位和复制消费`, async () => {
      const f = fixture();
      const view = f.internal._primaryView;
      const driver = nativeSelectionDriver(view);
      englishRun(view, 0, text, [1, 3]);
      const expected = text.length === 3 ? "Ex" : "Exy";
      const rects = [[50, 200, 60, 210], [70, 200, 80, 210]];
      if (text.length === 5) rects.push([90, 200, 100, 210]);
      await f.module.attachToReader(f.reader);
      driver.down(0, reverse ? 49 + text.length * 10 : 51, 205);
      driver.move(0, reverse ? 51 : 369, reverse ? 25 : 205);
      driver.check("en", expected, rects);
      const range = view._selectionRanges[0];
      assert.equal(range.anchorOffset, reverse ? 4 + text.length : 4);
      assert.equal(range.headOffset, reverse ? 4 : 4 + text.length);
      assert.deepEqual(view.getSelectionPosition(), { pageIndex: 0, rects });
      const copied = driver.copy();
      assert.equal(copied.data.get("text/plain"), expected);
      assert.equal(copied.textOnly, true, "同页多片段仍走不超过两页的批注复制分支");
      assert.equal(copied.annotation.position.nextPageRects, undefined);
      driver.up();
      driver.check("en", expected, rects);
      driver.released(expected);
      assert.equal(f.logs.length, 0);
      f.module.shutdown();
    });
  }
}

test("原生最终选区：两页各有隔离空洞，nextPageRects 和复制按真实页数消费", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  englishRun(view, 0, "Eexzy", [1, 3]);
  englishRun(view, 1, "Eex", [1]);
  await f.module.attachToReader(f.reader);
  driver.down(0, 51, 205);
  driver.move(1, 79, 205);
  const firstRects = [[50, 200, 60, 210], [70, 200, 80, 210], [90, 200, 100, 210]];
  const nextRects = [[50, 200, 60, 210], [70, 200, 80, 210]];
  driver.check("en", "Exy|Ex", [...firstRects, ...nextRects]);
  assert.deepEqual(view.getSelectionPosition(), { pageIndex: 0, rects: firstRects, nextPageRects: nextRects });
  const copied = driver.copy();
  assert.equal(copied.data.get("text/plain"), "Exy Ex");
  assert.equal(copied.textOnly, true);
  assert.deepEqual(copied.annotation.position.nextPageRects, nextRects);
  driver.up();
  driver.released("Exy Ex");
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

test("原生复制夹具：真实三页选区仍走换行拼接的纯文本分支", () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  view._pdfPages[2] = selectionPage(2);
  view._selectionRanges = getSelectionRanges(view._pdfPages,
    { pageIndex: 0, offset: 4 }, { pageIndex: 2, offset: 6 });
  const copied = driver.copy();
  assert.equal(copied.annotation, undefined);
  assert.equal(copied.data.get("text/plain"), "Ee 中译\nHh Ss Ee 中译\nHh Ss Ee");
  f.module.shutdown();
});

test("原生最终选区：原生区间重建夹入的异类字符保留独立矩形并按页合并", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  const chars = view._pdfPages[0].chars;
  chars.splice(6, 0, { ...chars[5], c: "x", rect: [70, 200, 80, 210],
    inlineRect: [70, 200, 80, 210] });
  chars.forEach((char, offset) => { char.offset = offset; });
  chars[5].isolated = true;
  await f.module.attachToReader(f.reader);
  driver.down(0, 51, 205);
  driver.move(0, 369, 205);
  driver.check("en", "Ex", [[50, 200, 60, 210], [70, 200, 80, 210]]);
  assert.deepEqual(view.getSelectionPosition(), { pageIndex: 0,
    rects: [[50, 200, 60, 210], [70, 200, 80, 210]] });
  const annotation = view._getAnnotationFromSelectionRanges(view._selectionRanges, "highlight", "#ffff00");
  assert.deepEqual(annotation.position.rects, view._selectionRanges.flatMap(range => range.position.rects));
  assert.equal(annotation.position.nextPageRects, undefined, "同页拆段仍为同页批注");
  assert.equal(annotation.text, "Ex");
  assert.equal(driver.copy().data.get("text/plain"), "Ex");
  const descriptor = Object.getOwnPropertyDescriptor(view, "_selectionRanges");
  assert.equal(typeof descriptor.set, "function");
  const selected = structuredClone(view._selectionRanges);
  f.module.shutdown();
  assert.deepEqual(view._selectionRanges, selected, "卸载保留当前选区而非安装前选区");
  assert.equal(Object.getOwnPropertyDescriptor(view, "_selectionRanges").writable, true);
  assert.equal(view._iframeWindow.listeners.get("pointermove").filter(entry => entry.capture).length, 0);
});

test("原生最终选区：50ms 节流等待期间保留选区，延迟回调仍按固定起点扩选", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view, true);
  await f.module.attachToReader(f.reader);
  driver.down(0, 69, 205);
  driver.move(1, 69, 205);
  driver.check("en", "e|Ee");
  driver.move(0, 51, 735);
  driver.check("en", "e|Ee");
  await new Promise(resolve => setTimeout(resolve, 70));
  driver.check("en", "Ee");
  f.module.shutdown();
});

// 同一行带放两个独立 span 框；第一个元素含两行，避免按整条跨栏带或单行误判。
function spanFixture() {
  const data = layout();
  for (const page of data.pages) {
    page.regions = page.regions.filter(region => region.k !== "span");
    page.regions.push({ k: "span", r: [0, 700, 280, 780] },
      { k: "span", r: [320, 700, 600, 780] });
  }
  const f = fixture({ info: { Custom: { ZTFSplitLayout: JSON.stringify(data) } } });
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  for (const page of Object.values(view._pdfPages)) {
    const template = page.chars[2];
    page.chars.splice(4, 0, ...[["Vv", 50, 710], ["Tt", 350, 730]].flatMap(([text, x, y]) =>
      [...text].map((c, index) => {
        const rect = [x + index * 10, y, x + index * 10 + 10, y + 10];
        return { ...template, c, rect, inlineRect: rect.slice(),
          wordBreakAfter: index === 1, lineBreakAfter: index === 1 };
      })));
    page.chars.forEach((char, offset) => { char.offset = offset; });
  }
  return { f, view, driver };
}

for (const detail of [1, 2, 3]) {
  for (const reverse of [false, true]) {
    test(`单个跨栏元素：区域行尾空白${reverse ? "反向跨页" : "正向到另一元素"}第 ${detail} 次点击`, async () => {
      const { f, view, driver } = spanFixture();
      await f.module.attachToReader(f.reader);
      // (279,735) 属于左边元素，但全页最近字符 Tt 属于右边元素。
      driver.down(reverse ? 1 : 0, 279, 735, detail);
      driver.move(0, 369, 735);
      const expected = reverse ? "Ss" : detail === 1 ? "Vv" : "Ss Vv";
      driver.check("span", expected);
      driver.up();
      driver.check("span", expected);
      driver.released(expected);
      checkPopupMatchesCopy(driver, view, expected);
      assert.deepEqual(view._selectionRanges.map(range => range.pageIndex), [reverse ? 1 : 0]);
      assert.equal(f.logs.length, 0);
      f.module.shutdown();
    });
  }
  for (const [name, start, end, expected] of [
    ["正向到同页另一跨栏元素", [0, 51, 735], [0, 369, 735], "Ss Vv"],
    ["反向到同页另一跨栏元素", [0, 369, 735], [0, 51, 735], "Tt"],
    ["到下方分栏正文", [0, 51, 735], [0, 369, 205], "Ss Vv"],
    ["到下一页跨栏元素", [0, 51, 735], [1, 369, 735], "Ss Vv"],
    ["反向到上一页跨栏元素", [1, 369, 735], [0, 51, 735], "Tt"],
  ]) {
    test(`单个跨栏元素：${detail === 1 ? "起拖" : detail === 2 ? "双击后拖选" : "三击后拖选"}${name}`, async () => {
      const { f, view, driver } = spanFixture();
      await f.module.attachToReader(f.reader);
      driver.down(...start, detail);
      if (detail > 1) driver.check("span", start[1] === 369 ? "Tt" : "Ss");
      driver.move(...end);
      driver.check("span", expected);
      assert.deepEqual(view._selectionRanges.map(range => range.pageIndex), [start[0]]);
      const selected = getCharsFromSelectionRanges(view._pdfPages, view._selectionRanges)
        .filter(char => !char.isolated);
      assert.ok(selected.every(char => start[1] === 369 ? char.rect[0] >= 320 : char.rect[2] <= 280),
        "同页同类但属于另一个 span 框的字符也必须隔离");
      driver.up();
      driver.check("span", expected);
      driver.released(expected);
      checkPopupMatchesCopy(driver, view, expected);
      assert.equal(view.getSelectionPosition().nextPageRects, undefined);
      assert.equal(f.logs.length, 0);
      f.module.shutdown();
    });
  }
}

for (const detail of [2, 3]) {
  test(`单个跨栏元素：${detail === 2 ? "双击" : "三击"}原生词行横跨多个元素时按元数据裁剪`, async () => {
    const { f, view, driver } = spanFixture();
    // 故意让原生词/行同时包住两个元素，不能依赖词行边界来识别元素。
    for (const index of [3, 5]) {
      view._pdfPages[0].chars[index].wordBreakAfter = false;
      view._pdfPages[0].chars[index].lineBreakAfter = false;
    }
    await f.module.attachToReader(f.reader);
    driver.down(0, 369, 735, detail);
    driver.check("span", "Tt");
    driver.up();
    driver.released("Tt");
    checkPopupMatchesCopy(driver, view, "Tt");
    assert.equal(f.logs.length, 0);
    f.module.shutdown();
  });
}

test("单个跨栏元素：新增页与迟到原生隔离不扩大元素范围，菜单与复制同步", async () => {
  const { f, view, driver } = spanFixture();
  const nextPage = view._pdfPages[1];
  delete view._pdfPages[1];
  await f.module.attachToReader(f.reader);
  driver.down(0, 51, 735);
  view._pdfPages[1] = view._mergeProcessedPageData(1, nextPage);
  assert.ok(view._pdfPages[1].chars.every(char => char.isolated), "新增页没有同一个跨栏元素");
  driver.move(1, 369, 735);
  driver.check("span", "Ss Vv");
  driver.up();
  // 起点变为原生隔离类别时，另一元素的原生隔离字符也不能混入。
  view._processedPageIsolatedCharIndexes[0] = [2, 3, 6, 7];
  view._pdfPages[0] = view._mergeProcessedPageData(0, view._pdfPages[0]);
  driver.check("span", "Ss");
  checkPopupMatchesCopy(driver, view, "Ss");
  assert.deepEqual(view._selectionRanges.map(range => range.pageIndex), [0]);
  assert.equal(f.logs.length, 0);
  f.module.shutdown();
});

// 菜单按钮实际提交的批注、加入笔记的批注与原生复制必须使用同一文本和位置。
function checkPopupMatchesCopy(driver, view, expected) {
  const copied = driver.copy();
  assert.equal(copied.data.get("text/plain"), expected);
  assert.equal(view._selectionPopup.annotation.text, expected);
  assert.deepEqual(view._selectionPopup.annotation.position, view.getSelectionPosition());
  assert.deepEqual(copied.annotation.position, view._selectionPopup.annotation.position);
  for (const type of ["highlight", "underline"]) driver.submitPopup(type);
  driver.addPopupToNote();
  for (const annotation of [...driver.submittedAnnotations, ...driver.noteAnnotations[0]]) {
    assert.equal(annotation.text, expected);
    assert.deepEqual(annotation.position, copied.annotation.position);
    assert.equal(annotation.sortIndex, copied.annotation.sortIndex);
  }
  assert.equal(driver.submittedAnnotations[1].type, "underline");
}

for (const kind of ["en", "zh", "span"]) {
  for (const reverse of [false, true]) {
    for (const detail of [1, 2, 3]) {
      for (const selectFirst of [false, true]) {
  test(`空字符页截止：${kind}${reverse ? "反向" : "正向"}${detail === 1 ? "拖选" : detail === 2 ? "双击" : "三击"}${selectFirst ? "已有选区后进入" : "直接进入"}空页`, async () => {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    view._pdfPages[2] = selectionPage(2);
    view._pdfPages[1].chars = [];
    await f.module.attachToReader(f.reader);
    const startPage = reverse ? 2 : 0;
    const x = kind === "zh" ? 350 : 50;
    const y = kind === "span" ? 735 : 205;
    const expected = kind === "en" ? "Ee" : kind === "zh" ? "中译" : "Ss";
    const rects = [[x, y - 5, x + 20, y + 5]];
    driver.down(startPage, reverse ? x + 19 : x + 1, y, detail);
    if (selectFirst) {
      driver.move(startPage, reverse ? x + 1 : x + 19, y);
      driver.check(kind, expected, rects);
    }
    // 重复进入空页及继续越过它，均不能把原生空结果视为用户清空。
    for (const pageIndex of [1, 1, reverse ? 0 : 2, 1]) {
      assert.doesNotThrow(() => driver.move(pageIndex, x + 1, y));
      driver.check(kind, expected, rects);
      assert.deepEqual(view.getSelectionPosition(), { pageIndex: startPage, rects });
    }
    if (detail === 1) {
      // 同一次拖选折返时使用真实 head；再次进入空页仍恢复到截止边界。
      driver.move(startPage, reverse ? x + 11 : x + 9, y);
      driver.check(kind, expected[reverse ? 1 : 0],
        [[reverse ? x + 10 : x, y - 5, reverse ? x + 20 : x + 10, y + 5]]);
      driver.move(1, x + 1, y);
    }
    assert.doesNotThrow(() => driver.up());
    driver.check(kind, expected, rects);
    driver.released(expected);
    checkPopupMatchesCopy(driver, view, expected);
    assert.equal(view._selectionPopup.annotation.position.nextPageRects, undefined);
    assert.deepEqual(f.logs.map(JSON.parse), [{
      event: "reader_selection_fallback", reason: "selection_empty_page_cutoff", count: 1,
    }], "空页拦截只记录固定诊断，不含 PDF 文本、坐标或异常详情");
    assert.equal(view._iframeWindow.count("pointerdown"), 2, "截止后钩子继续工作");
    // 非拖选运算导致的显式清空仍然有效。
    view._setSelectionRanges();
    assert.deepEqual(view._selectionRanges, []);
    f.module.shutdown();
  });
      }
    }
  }
}

for (const kind of ["en", "zh"]) {
  for (const reverse of [false, true]) {
    for (const detail of [1, 2, 3]) {
      test(`空字符页截止：${kind}${reverse ? "反向" : "正向"}${detail === 1 ? "拖选" : detail === 2 ? "双击" : "三击"}保留截止前两页`, async () => {
        const f = fixture();
        const view = f.internal._primaryView;
        const driver = nativeSelectionDriver(view);
        view._pdfPages[2] = selectionPage(2);
        const emptyPage = reverse ? 0 : 2;
        view._pdfPages[emptyPage].chars = [];
        await f.module.attachToReader(f.reader);
        const x = kind === "en" ? 50 : 350;
        const text = kind === "en" ? "Ee" : "中译";
        const startPage = reverse ? 2 : 0;
        driver.down(startPage, reverse ? x + 19 : x + 1, 205, detail);
        driver.move(1, reverse ? x + 1 : x + 19, 205);
        driver.check(kind, `${text}|${text}`);
        assert.doesNotThrow(() => driver.move(emptyPage, x + 1, 205));
        const rects = [[x, 200, x + 20, 210]];
        driver.check(kind, `${text}|${text}`, [...rects, ...rects]);
        const position = { pageIndex: reverse ? 1 : 0, rects, nextPageRects: rects };
        assert.deepEqual(view.getSelectionPosition(), position);
        driver.up();
        driver.check(kind, `${text}|${text}`, [...rects, ...rects]);
        driver.released(`${text} ${text}`);
        checkPopupMatchesCopy(driver, view, `${text} ${text}`);
        assert.deepEqual(view._selectionPopup.annotation.position, position);
        f.module.shutdown();
      });
    }
  }
}

for (const kind of ["en", "zh"]) {
  for (const emptyPages of [1, 2]) {
    for (const reverse of [false, true]) {
  test(`补挂钩：${reverse ? "反向" : "正向"}${kind}遇 ${emptyPages} 个无同侧字符的页面截止（已知限制，见规格目标 5）`, async () => {
    const data = layout();
    data.pages.push({ ...data.pages[0], src: 3 });
    const f = fixture({ info: { Custom: { ZTFSplitLayout: JSON.stringify(data) } } });
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    const lastPage = emptyPages + 1;
    for (let index = 1; index <= lastPage; index++) {
      view._pdfPages[index] = selectionPage(index);
      if (index < lastPage) {
        view._pdfPages[index].chars = view._pdfPages[index].chars.filter(char => char.kind !== kind);
        view._pdfPages[index].chars.forEach((char, offset) => { char.offset = offset; });
      }
    }
    await f.module.attachToReader(f.reader);
    const x = kind === "en" ? 50 : 350;
    const expected = kind === "en" ? (reverse ? "E" : "e") : (reverse ? "中" : "译");
    const rects = [[reverse ? x : x + 10, 200, reverse ? x + 10 : x + 20, 210]];
    const startPage = reverse ? lastPage : 0;
    driver.down(startPage, reverse ? x + 1 : x + 19, 205);
    driver.move(reverse ? 0 : lastPage, reverse ? x + 1 : x + 19, 205);
    driver.check(kind, expected, rects);
    assert.deepEqual(view._selectionRanges.map(range => range.pageIndex), [startPage]);
    assert.deepEqual(view.getSelectionPosition(), { pageIndex: startPage, rects });
    driver.up();
    driver.check(kind, expected, rects);
    driver.released(expected);
    checkPopupMatchesCopy(driver, view, expected);
    assert.equal(view._selectionPopup.annotation.position.nextPageRects, undefined);
    assert.deepEqual(view._selectionRanges.flatMap(range => range.position.rects.map(rect =>
      ({ pageIndex: range.position.pageIndex, rect }))), rects.map(rect => ({ pageIndex: startPage, rect })));
    assert.equal(f.logs.length, 0);
    f.module.shutdown();
  });
    }
  }
}

for (const kind of ["en", "zh"]) {
  for (const reverse of [false, true]) {
  test(`补挂钩：${kind}${reverse ? "反向" : "正向"}跨页保留截止页之前的连续两页（已知限制，见规格目标 5）`, async () => {
    const f = fixture();
    const view = f.internal._primaryView;
    const driver = nativeSelectionDriver(view);
    view._pdfPages[2] = selectionPage(2);
    const emptyPage = reverse ? 0 : 2;
    view._pdfPages[emptyPage].chars = view._pdfPages[emptyPage].chars.filter(char => char.kind !== kind);
    view._pdfPages[emptyPage].chars.forEach((char, offset) => { char.offset = offset; });
    await f.module.attachToReader(f.reader);
    const x = kind === "en" ? 50 : 350;
    driver.down(reverse ? 2 : 0, reverse ? x + 1 : x + 19, 205);
    driver.move(emptyPage, reverse ? x + 1 : x + 19, 205);
    const expected = kind === "en" ? (reverse ? "Ee|E" : "e|Ee") : (reverse ? "中译|中" : "译|中译");
    driver.check(kind, expected);
    driver.up();
    driver.check(kind, expected);
    assert.deepEqual(view._selectionRanges.map(range => range.pageIndex), reverse ? [1, 2] : [0, 1]);
    driver.released(expected.replace("|", " "));
    checkPopupMatchesCopy(driver, view, expected.replace("|", " "));
    const firstRects = [reverse ? [x, 200, x + 20, 210] : [x + 10, 200, x + 20, 210]];
    assert.deepEqual(view.getSelectionPosition(), { pageIndex: reverse ? 1 : 0,
      rects: firstRects, nextPageRects: [[x, 200, reverse ? x + 10 : x + 20, 210]] });
    assert.equal(f.logs.length, 0);
    f.module.shutdown();
  });
  }
}

for (const reverse of [false, true]) {
  for (const type of ["highlight", "underline"]) {
    test(`补挂钩：松开后起点隔离迟到，${reverse ? "反向" : "正向"}菜单 ${type} 与笔记按钮提交新批注`, async () => {
      const f = fixture();
      const view = f.internal._primaryView;
      const driver = nativeSelectionDriver(view);
      await f.module.attachToReader(f.reader);
      const startPage = reverse ? 1 : 0;
      driver.down(startPage, reverse ? 69 : 51, 205);
      driver.move(1 - startPage, reverse ? 51 : 69, 205);
      driver.up();
      driver.released("Ee Ee");
      const oldAnnotation = view._selectionPopup.annotation;
      const rects = [reverse ? [60, 200, 70, 210] : [50, 200, 60, 210]];
      // 本次 merge 返回新字符对象，菜单刷新必须使用返回页，而非尚未更新的缓存。
      view._processedPageIsolatedCharIndexes[startPage] = [reverse ? 5 : 4];
      view._pdfPages[startPage] = view._mergeProcessedPageData(startPage, view._pdfPages[startPage]);
      driver.check("en", reverse ? "e" : "E", rects);
      assert.equal(driver.copy().data.get("text/plain"), reverse ? "e" : "E");
      driver.submitPopup(type, "#ff6666");
      driver.addPopupToNote();
      const submitted = driver.submittedAnnotations[0];
      assert.equal(submitted.text, reverse ? "e" : "E", "按钮实际提交重算后的文本");
      assert.deepEqual(submitted.position, { pageIndex: startPage, rects }, "按钮实际提交重算后的矩形");
      assert.equal(submitted.type, type);
      assert.equal(submitted.color, "#ff6666");
      assert.equal(submitted.pageLabel, String(startPage + 1));
      assert.equal(submitted.sortIndex, view._selectionRanges[0].sortIndex);
      assert.notEqual(view._selectionPopup.annotation, oldAnnotation, "重新生成批注，替换菜单缓存");
      assert.deepEqual(view._selectionPopup.rect, view.getClientRectForPopup({ pageIndex: startPage, rects }));
      assert.equal(driver.noteAnnotations[0][0].text, submitted.text);
      assert.deepEqual(driver.noteAnnotations[0][0].position, submitted.position);
      assert.equal(f.logs.length, 0);
      f.module.shutdown();
    });
  }
}

test("诊断按类型去重：空页截止后菜单几何失败仍记录脱敏诊断", async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  view._pdfPages[1].chars = [];
  await f.module.attachToReader(f.reader);
  driver.down(0, 51, 205);
  driver.move(1, 51, 205);
  driver.up();
  driver.check("en", "Ee");
  assert.ok(view._selectionPopup);
  view.getClientRectForPopup = () => {
    throw new Error("secret-key PDF原文 C:/private/paper.pdf user@example.com");
  };
  view._processedPageIsolatedCharIndexes[0] = [4];
  assert.doesNotThrow(() => {
    view._pdfPages[0] = view._mergeProcessedPageData(0, view._pdfPages[0]);
  });
  assert.equal(view._selectionPopup, undefined);
  driver.check("en", "E", [[50, 200, 60, 210]]);
  assert.deepEqual(f.logs.map(JSON.parse), [
    { event: "reader_selection_fallback", reason: "selection_empty_page_cutoff", count: 1 },
    { event: "reader_selection_fallback", reason: "selection_popup_unavailable", count: 1 },
  ]);
  assert.ok(!/secret-key|PDF原文|private|paper\.pdf|user@example\.com/.test(f.logs.join("")));
  // 同一故障再次发生仍去重，不能重新关闭的菜单缓存触发重复日志。
  view._onSetSelectionPopup({ annotation: {}, rect: [] });
  view._pdfPages[0] = view._mergeProcessedPageData(0, view._pdfPages[0]);
  assert.equal(f.logs.length, 2);
  f.module.shutdown();
});

for (const failure of ["geometry", "annotation", "clone"]) {
test(`补挂钩：迟到隔离后菜单 ${failure} 失败，关闭菜单并记录脱敏诊断`, async () => {
  const f = fixture();
  const view = f.internal._primaryView;
  const driver = nativeSelectionDriver(view);
  await f.module.attachToReader(f.reader);
  driver.down(0, 51, 205);
  driver.move(1, 69, 205);
  driver.up();
  assert.ok(view._selectionPopup);
  const privateError = () => { throw new Error("secret-key PDF原文 C:/private/paper.pdf user@example.com"); };
  if (failure === "geometry") view.getClientRectForPopup = privateError;
  else if (failure === "annotation") view._getAnnotationFromSelectionRanges = privateError;
  else {
    const clone = f.context.Components.utils.cloneInto;
    f.context.Components.utils.cloneInto = (value, target) => {
      if (value.annotation) privateError();
      return clone(value, target);
    };
  }
  view._processedPageIsolatedCharIndexes[0] = [4];
  view._pdfPages[0] = view._mergeProcessedPageData(0, view._pdfPages[0]);
  driver.check("en", "E", [[50, 200, 60, 210]]);
  assert.equal(view._selectionPopup, undefined, "不能留下旧批注供按钮提交");
  assert.throws(() => driver.submitPopup(), /菜单应已打开/);
  assert.deepEqual(driver.submittedAnnotations, []);
  if (failure !== "annotation") assert.equal(driver.copy().data.get("text/plain"), "E");
  assert.equal(f.logs.length, 1);
  assert.deepEqual(JSON.parse(f.logs[0]), {
    event: "reader_selection_fallback", reason: "selection_popup_unavailable", count: 1,
  });
  assert.ok(!/secret-key|PDF原文|private|paper\.pdf|user@example\.com/.test(f.logs.join("")),
    "诊断不能包含异常消息中的凭据、原文、路径或账号");
  f.module.shutdown();
});
}

(async () => {
  for (const { name, run } of tests) {
    await run();
    process.stdout.write(`通过：${name}\n`);
  }
  process.stdout.write(`readerSelection：${tests.length} 项测试通过\n`);
})().catch(error => { console.error(error); process.exitCode = 1; });
