/* global Components */

// 职责：仅对带冻结版面元数据的 PDF 安装划词隔离；内部接口失效即恢复原生行为。
// init 配置依赖，attachToReader 等待阅读器就绪，shutdown 撤销全部监听和方法包装。
var ZoteroTranslateFilesReaderSelection = (function () {
  var running = false;
  var dependencies = null;
  var readers = new WeakMap();
  var liveReaders = new Set();
  var invalidReaderLogged = false;

  function unwrap(value) {
    return value && (value.wrappedJSObject || value);
  }

  function requireValue(condition) {
    if (!condition) throw new Error("阅读器内部接口不可用");
  }

  function diagnose(record, reason, event) {
    event = event || "reader_selection_fallback";
    if (!record.loggedReasons) record.loggedReasons = new Set();
    var key = event + ":" + reason;
    if (record.loggedReasons.has(key)) return;
    record.loggedReasons.add(key);
    var line = JSON.stringify({ event: event, reason: reason, count: 1 });
    // 不记录 PDF 内容、路径或异常消息；日志接收方失效也不能影响阅读器。
    try {
      if (dependencies && typeof dependencies.log === "function") dependencies.log(line);
      else if (dependencies && dependencies.Zotero && dependencies.Zotero.debug) {
        dependencies.Zotero.debug(line);
      }
    } catch (error) {
      // 日志回调属于宿主；无法继续报告它自身的失败。
    }
  }

  function cloneInto(value, window) {
    requireValue(typeof Components !== "undefined" && Components.utils &&
      typeof Components.utils.cloneInto === "function");
    return Components.utils.cloneInto(value, window);
  }

  function validRect(rect) {
    return Array.isArray(rect) && rect.length === 4 && rect.every(Number.isFinite) &&
      rect[0] <= rect[2] && rect[1] <= rect[3];
  }

  function parseLayout(raw) {
    requireValue(typeof raw === "string");
    var data = JSON.parse(raw);
    requireValue(data && data.v === 1 && Array.isArray(data.pages) && data.pages.length > 0);
    for (var page of data.pages) {
      requireValue(page && Number.isInteger(page.src) && page.src >= 0 &&
        typeof page.part === "string" && Array.isArray(page.regions));
      for (var region of page.regions) {
        requireValue(region && ["en", "zh", "span", "hf"].includes(region.k) &&
          validRect(region.r) && region.r[0] < region.r[2] && region.r[1] < region.r[3]);
      }
    }
    return data;
  }

  // r 与字符 rect 均为 PDF 用户空间、左下原点；不做页面高度翻转。
  function regionOfRect(record, pageIndex, rect) {
    requireValue(validRect(rect));
    var page = record.layout.pages[pageIndex];
    if (!page) return null;
    var x = (rect[0] + rect[2]) / 2;
    var y = (rect[1] + rect[3]) / 2;
    // 跨栏和页眉页脚带优先于正文框的边界重叠。
    for (var kind of ["hf", "span", "en", "zh"]) {
      for (var region of page.regions) {
        var r = region.r;
        if (region.k === kind && x >= r[0] && x <= r[2] && y >= r[1] && y <= r[3]) {
          return region;
        }
      }
    }
    return null;
  }

  function classOfRect(record, pageIndex, rect) {
    var region = regionOfRect(record, pageIndex, rect);
    return region ? region.k : null;
  }

  function remember(state, char) {
    requireValue(char && validRect(char.rect));
    if (!state.originals.has(char)) {
      state.originals.set(char, {
        own: Object.prototype.hasOwnProperty.call(char, "isolated"), value: char.isolated,
      });
    }
    return state.originals.get(char);
  }

  function restorePage(state, page) {
    if (!page) return;
    requireValue(Array.isArray(page.chars));
    for (var char of page.chars) {
      var original = state.originals.get(char);
      if (!original) continue;
      if (original.own) char.isolated = original.value;
      else delete char.isolated;
    }
  }

  function applyPage(record, state, index, page) {
    if (!page) return;
    requireValue(Array.isArray(page.chars));
    var layoutPage = record.layout.pages[index];
    if (state.startClass === null || !layoutPage || !layoutPage.regions.length) {
      if (state.startClass !== null) {
        diagnose(record, layoutPage ? "layout_regions_empty" : "layout_page_missing");
      }
      restorePage(state, page);
      return;
    }
    for (var char of page.chars) {
      char.isolated = !matchesStart(record, state, index, char);
    }
  }

  function classOfChar(record, state, index, char) {
    var kind = classOfRect(record, index, char.rect);
    // 原生隔离标记不能抹掉版面类别，否则两侧被原生隔离的字符会互相开放。
    // 保留原生隔离组，同时继续按本页冻结区域约束英文、中文和跨栏元素。
    return remember(state, char).value ? "zotero-isolated:" + kind : kind;
  }

  function matchesStart(record, state, index, char) {
    if (classOfChar(record, state, index, char) !== state.startClass) return false;
    if (!state.start.span) return true;
    // 冻结元数据以输出页号和 span 区域框识别元素，不把同页的其他 span 视为同一元素。
    if (index !== state.start.pageIndex) return false;
    var region = regionOfRect(record, index, char.rect);
    return region && region.k === "span" && region.r.every(function (value, i) {
      return value === state.start.span[i];
    });
  }

  function nearestChar(state, page, rect, accepts) {
    requireValue(page && Array.isArray(page.chars));
    var nearest = null;
    var distance = Infinity;
    for (var char of page.chars) {
      remember(state, char);
      if (accepts && !accepts(char)) continue;
      var r = char.rect;
      var dx = Math.max(r[0] - rect[2], 0, rect[0] - r[2]);
      var dy = Math.max(r[1] - rect[3], 0, rect[1] - r[3]);
      var d = dx * dx + dy * dy;
      if (d < distance) { nearest = char; distance = d; }
    }
    return nearest;
  }

  // 对齐 reader.js:42732–42764 的半字符规则（该版本 isRTL 恒为 false）。
  function caretOffset(page, char, rect) {
    var r = char.rect;
    var rotation = char.rotation || 0;
    var after = !rotation && rect[0] > (r[0] + r[2]) / 2 ||
      rotation === 90 && rect[1] > (r[1] + r[3]) / 2 ||
      rotation === 180 && rect[0] < (r[0] + r[2]) / 2 ||
      rotation === 270 && rect[1] < (r[1] + r[3]) / 2;
    return page.chars.indexOf(char) + (after ? 1 : 0);
  }

  // reader.js:42908–42957：词/行以 inlineRect 的包围框距离定位，返回半开字符区间。
  function closestUnit(page, rect, boundary) {
    var result = null;
    var distance = Infinity;
    var from = 0;
    var box = null;
    for (var index = 0; index < page.chars.length; index++) {
      var char = page.chars[index];
      var r = char.inlineRect;
      requireValue(validRect(r));
      box = box ? [Math.min(box[0], r[0]), Math.min(box[1], r[1]),
        Math.max(box[2], r[2]), Math.max(box[3], r[3])] : r.slice();
      if (!char[boundary]) continue;
      var dx = Math.max(box[0] - rect[2], 0, rect[0] - box[2]);
      var dy = Math.max(box[1] - rect[3], 0, rect[1] - box[3]);
      var d = dx * dx + dy * dy;
      if (d < distance) { result = { from: from, to: index + 1 }; distance = d; }
      from = index + 1;
      box = null;
    }
    return result;
  }

  // reader.js:42863–42907、43022–43115：键盘从未经过隔离的 head 移动。
  // 保留原生跨页步长及相邻行按字符矩形距离选列的规则。
  function moveHead(pdfPages, current, direction) {
    var head = { pageIndex: current.pageIndex, offset: current.offset };
    var chars = pdfPages[head.pageIndex].chars;
    var backward = direction === "Left" || direction === "Up";
    if (backward && head.offset === 0) {
      var previous = pdfPages[head.pageIndex - 1];
      if (previous && previous.chars.length) {
        head.pageIndex--;
        head.offset = previous.chars.length - 1;
      }
    } else if (!backward && head.offset === chars.length) {
      var next = pdfPages[head.pageIndex + 1];
      if (next && next.chars.length) { head.pageIndex++; head.offset = 1; }
    } else if (direction === "Left" || direction === "Right") {
      head.offset += backward ? -1 : 1;
    } else {
      var offset = head.offset === chars.length ? head.offset - 1 : head.offset;
      var from;
      var to;
      if (backward) {
        to = chars.findLastIndex(function (char, index) { return index < offset && char.lineBreakAfter; });
        if (to === -1) { head.offset = 0; return head; }
        from = chars.findLastIndex(function (char, index) { return index < to && char.lineBreakAfter; }) + 1;
      } else {
        from = chars.findIndex(function (char, index) { return index >= offset && char.lineBreakAfter; });
        if (from === -1 || from === chars.length - 1) { head.offset = chars.length; return head; }
        from++;
        to = chars.findIndex(function (char, index) { return index >= from && char.lineBreakAfter; });
      }
      var distance = Infinity;
      var r = chars[offset].rect;
      for (var index = from; index <= to; index++) {
        var rect = chars[index].rect;
        var dx = Math.max(rect[0] - r[2], 0, r[0] - rect[2]);
        var dy = Math.max(rect[1] - r[3], 0, r[1] - rect[3]);
        var d = dx * dx + dy * dy;
        if (d < distance) { head.offset = index; distance = d; }
      }
    }
    return head;
  }

  // 原生隔离会缩减端点，甚至留下折叠的首页区间。这里不读取其中的字符范围：
  // 始终从固定起点与独立保存的 head 重算，同类片段各自保留矩形，每页只输出一个区间。
  function rebuildRanges(record, state, mergedIndex, mergedPage) {
    var start = state.start;
    var head = state.head;
    var getPage = function (index) {
      return index === mergedIndex ? mergedPage : state.view._pdfPages[index];
    };
    var startPage = getPage(start.pageIndex);
    requireValue(startPage && Array.isArray(startPage.chars));
    // 空起点没有字符锚点，不能借用相邻区域或下一页的字符建立选区。
    // 保留 state.start 及类别供 Shift 单击、迟到合并和下一次起拖使用。
    var reverse = head.pageIndex < start.pageIndex ||
      head.pageIndex === start.pageIndex && head.offset < start.caret;
    var anchor = start.unit ? (reverse ? start.unit.to : start.unit.from) : start.caret;
    var first = startPage.chars[reverse ? anchor - 1 : anchor];
    // 半字符插入点恰好越过类别边界时，锚点取被点击字符靠拖选反方向的一侧。
    if (!start.unit && !start.expandedAnchor &&
      (!first || !matchesStart(record, state, start.pageIndex, first))) {
      anchor = start.offset + (reverse ? 1 : 0);
    }
    var result = [];
    var collapsed = start.empty || !start.unit && head.pageIndex === start.pageIndex && head.offset === start.caret;
    // 从起点沿拖选方向逐页延伸；跨栏起点始终限于当前输出页的本元素。
    // 无同侧字符的页面即截止（已知限制，见规格目标 5），保证原生消费时页码连续。
    for (var index = start.pageIndex;
      !collapsed && (reverse ? index >= head.pageIndex : index <= head.pageIndex); index += reverse ? -1 : 1) {
      if (start.span && index !== start.pageIndex) break;
      var page = getPage(index);
      if (!page) break;
      requireValue(page && Array.isArray(page.chars));
      var a = index === start.pageIndex ? anchor : reverse ? page.chars.length : 0;
      var h = index === head.pageIndex ? head.offset : reverse ? 0 : page.chars.length;
      var from = Math.max(0, Math.min(a, h));
      var to = Math.min(page.chars.length, Math.max(a, h));
      var offset = from;
      var firstOffset = null;
      var lastOffset;
      var rects = [];
      var text = [];
      while (offset < to) {
        if (!matchesStart(record, state, index, page.chars[offset])) {
          offset++;
          continue;
        }
        var fromOffset = offset;
        while (offset < to && matchesStart(record, state, index, page.chars[offset])) offset++;
        if (firstOffset === null) firstOffset = fromOffset;
        lastOffset = offset;
        var chars = page.chars.slice(fromOffset, offset);
        var line = null;
        // 与 reader.js:42799–42837 一致，保留原生空格、段落及 ignorable 语义。
        for (var char of chars) {
          requireValue(validRect(char.inlineRect));
          var r = char.inlineRect;
          line = line ? [Math.min(line[0], r[0]), Math.min(line[1], r[1]),
            Math.max(line[2], r[2]), Math.max(line[3], r[3])] : r.slice();
          if (char.lineBreakAfter) { rects.push(line); line = null; }
          if (!char.ignorable) {
            text.push(char.c);
            if (char.spaceAfter || char.lineBreakAfter) text.push(" ");
            if (char.paragraphBreakAfter) text.push(" ");
          }
        }
        if (line) rects.push(line);
      }
      if (firstOffset === null) break;
      // offset 包围该页同类字符的首尾；空洞由并列矩形和按内容流拼接的文本表达。
      // sortIndex 必须用合并后的全部矩形重算，对齐 reader.js:43001–43021。
      var topRect = rects.slice().sort(function (a, b) { return b[2] - a[2]; })[0];
      var top = Math.max(0, page.viewBox[3] - page.viewBox[1] - topRect[3]);
      var sortOffset = page.chars.indexOf(nearestChar(state, page, topRect));
      result.push({
        pageIndex: index,
        anchorOffset: reverse ? lastOffset : firstOffset, headOffset: reverse ? firstOffset : lastOffset,
        position: { pageIndex: index, rects: rects }, text: text.join("").trim(),
        sortIndex: [String(index).slice(0, 5).padStart(5, "0"),
          String(sortOffset).slice(0, 6).padStart(6, "0"),
          String(Math.floor(top)).slice(0, 5).padStart(5, "0")].join("|"),
      });
    }
    if (reverse) result.reverse();
    if (result.length) {
      result[reverse ? result.length - 1 : 0].anchor = true;
      result[reverse ? 0 : result.length - 1].head = true;
    } else {
      // 保留可供原生 getModifiedSelectionRanges 继续扩选的合法折叠区间。
      result.push({ pageIndex: start.pageIndex, anchor: true, head: true, collapsed: true,
        anchorOffset: start.caret, headOffset: start.caret,
        position: { pageIndex: start.pageIndex, rects: [] }, text: "" });
    }
    return cloneInto(result, state.window);
  }

  function watchSelection(record, state) {
    var view = state.view;
    var descriptor = Object.getOwnPropertyDescriptor(view, "_selectionRanges");
    requireValue(descriptor && descriptor.configurable && descriptor.writable &&
      Array.isArray(descriptor.value));
    var value = descriptor.value;
    state.refreshSelection = function (index, page) {
      if (state.hasSelection && state.start && state.head && state.startClass !== null) {
        value = rebuildRanges(record, state, index, page);
        if (view._selectionPopup) {
          var popup;
          if (value[0] && !value[0].collapsed) {
            try {
              // 对齐原生 _setSelectionRanges / pointerup：按钮使用菜单缓存的批注。
              // 不再调用 _setSelectionRanges，以免 setter 用尚未写回缓存的旧页重算。
              popup = cloneInto({ rect: view.getClientRectForPopup(value[0].position),
                annotation: view._getAnnotationFromSelectionRanges(value, "highlight") }, state.window);
            } catch (error) {
              // 几何或批注无法安全重建时关闭菜单，避免按钮继续提交旧批注。
              diagnose(record, "selection_popup_unavailable");
            }
          }
          view._onSetSelectionPopup(popup);
        }
      }
    };
    var get = function () { return value; };
    var set = function (ranges) {
      var previous = value;
      value = ranges;
      if (!state.active || !record.active) return;
      try {
        if (!state.pendingHead && state.hasSelection && state.start && state.head && ranges.length) {
          var nativeHead = ranges.find(function (range) { return range.head; });
          if (nativeHead) {
            // 原生键盘赋值后立即滚动（reader.js:78680–78691）。必须在本 setter
            // 采用新 head，不能等后注册的键盘监听才推进，否则页尾收缩会恢复旧端点。
            state.nativeStep = { previousHead: state.head,
              head: { pageIndex: nativeHead.pageIndex, offset: nativeHead.headOffset } };
            if (state.start.unit) {
              var anchorRange = previous.find(function (range) { return range.anchor; });
              requireValue(anchorRange);
              state.start.caret = anchorRange.anchorOffset;
              state.start.unit = null;
              state.start.boundary = null;
              state.start.expandedAnchor = true;
            }
            state.head = state.nativeStep.head;
          }
        }
        // 显式清空仍由 Zotero 控制；定位回调刚提供 head 时，空中间结果也必须重建。
        // 空内容的原生结果可能为 []；空起点仍用折叠区间表达，供复制安全判空。
        state.hasSelection = ranges.length > 0 || state.pendingHead || !!(state.start && state.start.empty);
        state.pendingHead = false;
        if (state.hasSelection && state.start && state.head && state.startClass !== null) {
          value = rebuildRanges(record, state);
        }
      }
      catch (error) { fail(record, "selection_unavailable"); }
    };
    Object.defineProperty(view, "_selectionRanges", {
      configurable: true, enumerable: descriptor.enumerable, get: get, set: set,
    });
    state.undo.push(function () {
      var current = Object.getOwnPropertyDescriptor(view, "_selectionRanges");
      if (current && current.get === get && current.set === set) {
        Object.defineProperty(view, "_selectionRanges", Object.assign({}, descriptor, { value: value }));
      }
    });
  }

  function replaceMethod(target, name, wrapper) {
    var descriptor = Object.getOwnPropertyDescriptor(target, name);
    target[name] = wrapper;
    requireValue(target[name] === wrapper);
    return function () {
      // 其他插件后来又包装时，不覆盖它的入口；本包装通过 active 状态失效。
      if (target[name] !== wrapper) return;
      if (descriptor) Object.defineProperty(target, name, descriptor);
      else delete target[name];
    };
  }

  function cleanup(record, operation) {
    try { operation(); }
    catch (error) { diagnose(record, "cleanup_unavailable"); }
  }

  function removeView(record, state) {
    if (!state.active) return;
    state.active = false;
    for (var undo of state.undo.reverse()) cleanup(record, undo);
    cleanup(record, function () {
      for (var key of Object.keys(state.view._pdfPages || {})) {
        restorePage(state, state.view._pdfPages[key]);
      }
    });
    record.views.delete(state.view);
  }

  function release(record) {
    record.active = false;
    for (var state of Array.from(record.views.values())) removeView(record, state);
    for (var undo of record.undo.reverse()) cleanup(record, undo);
    record.undo = [];
    liveReaders.delete(record);
  }

  function fail(record, reason) {
    diagnose(record, reason);
    record.failed = true;
    release(record);
    return false;
  }

  async function attachView(record, candidate) {
    var view = unwrap(candidate);
    requireValue(view);
    if (view.initializedPromise) await view.initializedPromise;
    if (!record.active || !running) return;
    // 等待期间可能已取消分屏或重建视图；不把钩子装到已退出阅读器的对象上。
    if (view !== unwrap(record.internal._primaryView) &&
      view !== unwrap(record.internal._secondaryView)) return;
    if (record.views.has(view)) return;
    var window = unwrap(view._iframeWindow);
    requireValue(window && typeof window.addEventListener === "function" &&
      typeof window.removeEventListener === "function" && view._pdfPages &&
      typeof view._pdfPages === "object" &&
      typeof view.pointerEventToPosition === "function" &&
      typeof view.pointerEventToAltPosition === "function" &&
      typeof view._setSelectionRanges === "function" && typeof view._render === "function" &&
      typeof view._scrollSelectionHeadIntoView === "function" &&
      typeof view._onSetSelectionPopup === "function" &&
      typeof view._getAnnotationFromSelectionRanges === "function" &&
      typeof view._mergeProcessedPageData === "function");
    // 提前确认跨域能力，避免安装一半后才在首个 pointerdown 失败。
    requireValue(typeof Components !== "undefined" && Components.utils &&
      typeof Components.utils.cloneInto === "function");
    var state = { view: view, window: window, active: true, originals: new WeakMap(),
      startClass: null, start: null, head: null, hasSelection: false, pendingHead: false, undo: [] };
    record.views.set(view, state);
    watchSelection(record, state);

    var useCharacterAnchor = function () {
      if (!state.start.unit) return;
      // 原生从词/行转为字符扩选时，固定的是当前已展开选区的 anchor。
      var anchorRange = view._selectionRanges.find(function (range) { return range.anchor; });
      requireValue(anchorRange);
      state.start.caret = anchorRange.anchorOffset;
      state.start.unit = null;
      state.start.boundary = null;
      state.start.expandedAnchor = true;
    };
    var beginSelection = function (event, position) {
      if (!state.active || !record.active) return;
      try {
        if (event.button !== undefined && event.button !== 0) return;
        if (state.downEvent === event) return;
        state.downEvent = event;
        // reader.js:77794–77808：Shift 单击沿用 anchor，但原生模式已回到 chars。
        if (event.shiftKey && (!event.detail || event.detail === 1) && state.start && state.hasSelection) {
          useCharacterAnchor();
          saveHead(position);
          return;
        }
        state.start = null;
        state.head = null;
        state.hasSelection = false;
        state.pendingHead = false;
        state.nativeStep = null;
        state.startClass = null;
        if (!position) {
          diagnose(record, "selection_position_missing");
          for (var key of Object.keys(view._pdfPages)) restorePage(state, view._pdfPages[key]);
          return;
        }
        requireValue(Number.isInteger(position.pageIndex) && position.pageIndex >= 0 &&
          position.rects && validRect(position.rects[0]));
        var rect = position.rects[0];
        var region = regionOfRect(record, position.pageIndex, rect);
        var kind = region ? region.k : null;
        var layoutPage = record.layout.pages[position.pageIndex];
        if (!layoutPage || !layoutPage.regions.length) {
          diagnose(record, layoutPage ? "layout_regions_empty" : "layout_page_missing");
        }
        if (layoutPage && layoutPage.regions.length) {
          var page = view._pdfPages[position.pageIndex];
          // 区域内空白仍属于该区域；仅在区域外才按全页最近字符回退。
          // 最近字符须来自坐标命中的区域，不能用旁边区域掩盖合法的空区域。
          var nearest = nearestChar(state, page, rect, region && function (char) {
            var charRegion = regionOfRect(record, position.pageIndex, char.rect);
            return charRegion === region;
          });
          if (!nearest) {
            diagnose(record, "selection_empty_start");
            state.start = { pageIndex: position.pageIndex, offset: 0, caret: 0, empty: true };
          } else {
            if (!region) {
              region = regionOfRect(record, position.pageIndex, nearest.rect);
              kind = region ? region.k : null;
            }
            // Zotero 原有隔离组独立保留，但不能借异类最近字符改变版面类别。
            kind = classOfChar(record, state, position.pageIndex, nearest);
            state.start = { pageIndex: position.pageIndex, offset: page.chars.indexOf(nearest),
              caret: caretOffset(page, nearest, rect) };
            if (event.detail === 2 || event.detail === 3) {
              state.start.boundary = event.detail === 2 ? "wordBreakAfter" : "lineBreakAfter";
              state.start.unit = closestUnit(page, nearest.rect, state.start.boundary);
              if (!state.start.unit) {
                state.start.empty = true;
                diagnose(record, "selection_empty_unit");
              }
            }
          }
          if (region && region.k === "span") state.start.span = region.r.slice();
          state.head = { pageIndex: position.pageIndex, offset: state.start.caret };
          if (state.start.unit) state.head.offset = state.start.unit.to;
          state.pendingHead = true;
        }
        state.startClass = kind;
        requireValue(view._pdfPages);
        for (var key of Object.keys(view._pdfPages)) {
          applyPage(record, state, Number(key), view._pdfPages[key]);
        }
        if (state.start && state.startClass === null && !state.start.empty) {
          diagnose(record, "selection_class_missing");
        }
        if (state.start && !state.start.empty && state.startClass !== null) {
          diagnose(state, "selection_applied", "reader_selection_hook");
        }
      } catch (error) { fail(record, "selection_unavailable"); }
    };
    var saveHead = function (position) {
      if (position && state.start && state.startClass !== null) {
        if (state.start.empty) {
          state.pendingHead = true;
          return;
        }
        var page = view._pdfPages[position.pageIndex];
        var nearest = nearestChar(state, page, position.rects[0]);
        if (!nearest) {
          // 空页也是真实 head：重建到该页之前截止，不能把原生空结果当作清空。
          state.head = { pageIndex: position.pageIndex, offset: 0 };
          state.pendingHead = true;
          diagnose(record, "selection_empty_page_cutoff");
          // reader.js:43116–43335 的词/行扩选不支持空页端点（反向会抛错）。
          // 仅给原生计算一个起点页内的安全临时端点；setter 仍按真实 head 重建。
          return cloneInto({ pageIndex: state.start.pageIndex,
            rects: [view._pdfPages[state.start.pageIndex].chars[state.start.offset].rect.slice()] }, window);
        }
        state.head = { pageIndex: position.pageIndex, offset: caretOffset(page, nearest, position.rects[0]) };
        if (state.start.unit && view.action &&
          (view.action.mode === "words" || view.action.mode === "lines")) {
          var unit = closestUnit(page, position.rects[0], state.start.boundary);
          if (unit) {
            var reverse = position.pageIndex < state.start.pageIndex ||
              position.pageIndex === state.start.pageIndex && unit.from < state.start.unit.from;
            state.head.offset = reverse ? unit.from : unit.to;
          } else {
            diagnose(record, "selection_empty_unit");
            state.pendingHead = true;
            // 与空页一样，原生词/行计算不能消费缺失单元；真实 head 已保存。
            return cloneInto({ pageIndex: state.start.pageIndex,
              rects: [view._pdfPages[state.start.pageIndex].chars[state.start.offset].rect.slice()] }, window);
          }
        }
        state.pendingHead = true;
      }
    };
    var onPointerDown = function (event) {
      if (!state.active || !record.active || state.downEvent === event) return;
      try {
        // 不传 chrome 事件对象；只克隆定位所需字段到视图窗口。
        beginSelection(event, view.pointerEventToPosition(cloneInto({
          clientX: event.clientX, clientY: event.clientY,
        }, window)));
      } catch (error) { fail(record, "selection_unavailable"); }
    };
    // 原生 pointermove 节流为 50ms：只在实际执行的定位调用中保存 head。
    // 原生先调用替代定位再调用正常定位（reader.js:77969–77981），正常定位优先。
    var toAltPosition = view.pointerEventToAltPosition;
    state.undo.push(replaceMethod(view, "pointerEventToAltPosition", function (event) {
      var position = toAltPosition.apply(this, arguments);
      if (state.active && record.active) state.altPosition = position;
      return position;
    }));
    var toPosition = view.pointerEventToPosition;
    state.undo.push(replaceMethod(view, "pointerEventToPosition", function (event) {
      var position = toPosition.apply(this, arguments);
      if (!state.active || !record.active || !event) return position;
      try {
        if (event.type === "mousedown" || event.type === "pointerdown") {
          // 原生触摸监听可能先于本模块的捕获监听执行，在首次赋值前就记录起点。
          beginSelection(event, position);
          // reader.js:42908–42957 在空页返回 undefined/NaN 词/行端点，
          // 随后的 getSelectionRanges 会抛错。走原生空定位分支清空选区；
          // setter 仍保留本区域的空起点，不伪造其他页或其他区域的字符。
          if (state.start && state.start.empty) return null;
        } else if (view.pointerDownPosition && view.action && view.action.type === "selectText" &&
          ["pointermove", "dragover", "pointerup"].includes(event.type)) {
          var headPosition = position || (event.type === "pointerup"
            ? view.pointerEventToAltPosition(event, view.pointerDownPosition.pageIndex) : state.altPosition);
          var safePosition = saveHead(headPosition);
          if (safePosition) position = safePosition;
          state.altPosition = null;
          // pointerup 不再给 _selectionRanges 赋值，但会立即用它生成批注和复制文本。
          if (event.type === "pointerup") {
            state.refreshSelection();
            state.pendingHead = false;
          }
        }
      } catch (error) { fail(record, "selection_unavailable"); }
      return position;
    }));
    // 选区 offset 是半开插入点，合法上限为 chars.length；原生滚动却直接索引
    // chars[headOffset]（reader.js:76747–76761）。仅在滚动参数中转为字符索引，
    // 不缩短选区文本，也不改后续 getModifiedSelectionRanges 使用的插入点。
    var scrollHead = view._scrollSelectionHeadIntoView;
    state.undo.push(replaceMethod(view, "_scrollSelectionHeadIntoView", function (ranges) {
      if (!state.active || !record.active || state.startClass === null) {
        return scrollHead.apply(this, arguments);
      }
      var scrollRanges;
      try {
        scrollRanges = cloneInto(ranges.filter(function (range) {
          // 折叠区间无须滚动；空页也没有可索引字符，均不是接口失效。
          if (range.collapsed) return false;
          var page = view._pdfPages[range.pageIndex];
          requireValue(page && Array.isArray(page.chars));
          if (!page.chars.length) diagnose(record, "selection_empty_page_scroll");
          return page.chars.length > 0;
        }).map(function (range) {
          var page = view._pdfPages[range.pageIndex];
          var last = page.chars.length - 1;
          return Object.assign({}, range, {
            anchorOffset: Math.max(0, Math.min(last, range.anchorOffset)),
            headOffset: Math.max(0, Math.min(last, range.headOffset)),
          });
        }), window);
      } catch (error) {
        fail(record, "selection_scroll_unavailable");
        return;
      }
      return scrollHead.call(this, scrollRanges);
    }));
    // 原生监听先执行，新 head 已在 setter 保存；只对隔离过滤隐藏的移动补算原生步长。
    var onKeyDown = function (event) {
      if (!state.active || !record.active || !state.hasSelection || !state.head || state.start.empty ||
        state.startClass === null || !event.shiftKey || event.ctrlKey || event.metaKey || event.altKey ||
        !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key) ||
        view._nativeTextSelection && view._nativeTextSelection.enabled ||
        typeof view._textAnnotationFocused === "function" && view._textAnnotationFocused() ||
        event.target && event.target.classList && event.target.classList.contains("textAnnotation")) return;
      try {
        useCharacterAnchor();
        var step = state.nativeStep;
        state.nativeStep = null;
        var moved = moveHead(view._pdfPages, step ? step.previousHead : state.head, event.key.slice(5));
        // 原生隔离可能裁掉异类字符中的 head；保留它才能继续跨页与折返。
        // 未裁剪时以本次原生计算出的 head 为准，不从已推进的 head 再走一步。
        state.head = step && step.head.pageIndex === moved.pageIndex && step.head.offset === moved.offset
          ? step.head : moved;
        state.pendingHead = true;
        view._setSelectionRanges(cloneInto(view._selectionRanges, window));
        view._render();
      } catch (error) { fail(record, "selection_unavailable"); }
    };
    state.undo.push(function () { window.removeEventListener("keydown", onKeyDown, true); });
    window.addEventListener("keydown", onKeyDown, true);
    var merge = view._mergeProcessedPageData;
    var wrappedMerge = function (pageIndex, pageData) {
      if (!state.active || !record.active) return merge.apply(this, arguments);
      try {
        // Zotero 会复用字符或用 {...char, isolated: true} 复制字符。
        // 先还原输入，再让原入口产生原生标记，最后重新保存并应用当前类别。
        restorePage(state, pageData);
      } catch (error) { fail(record, "page_data_unavailable"); }
      // 原方法自身的异常仍按 Zotero 原有路径传播，不吞掉、不重试。
      var result = merge.apply(this, arguments);
      if (!state.active || !record.active) return result;
      try {
        var previousClass = state.startClass;
        if (result) {
          requireValue(Array.isArray(result.chars));
          for (var char of result.chars) {
            state.originals.delete(char);
            remember(state, char);
          }
          if (state.start && !state.start.empty && Number(pageIndex) === state.start.pageIndex) {
            var startChar = result.chars[state.start.offset];
            if (startChar) state.startClass = classOfChar(record, state, Number(pageIndex), startChar);
            else {
              // 起点字符在重新解析后消失也是合法空内容，后续扩选不能再索引它。
              diagnose(record, "selection_empty_start");
              state.start.empty = true;
              state.start.unit = null;
              state.start.caret = 0;
              state.start.offset = 0;
            }
          }
        }
        // 起点刚变成原生隔离字符时，必须重标全部页，而非仅当前合并页。
        if (previousClass !== state.startClass) {
          for (var key of Object.keys(view._pdfPages)) {
            applyPage(record, state, Number(key), view._pdfPages[key]);
          }
        }
        applyPage(record, state, Number(pageIndex), pageData);
        applyPage(record, state, Number(pageIndex), result);
        // 原调用者尚未把返回页写回缓存；从固定起点和保存的 head 用结果页重算。
        if (result) state.refreshSelection(Number(pageIndex), result);
      } catch (error) {
        // 返回值尚未放入 _pdfPages，release 无法遍历它；在此一并还原。
        cleanup(record, function () { restorePage(state, pageData); });
        cleanup(record, function () { restorePage(state, result); });
        fail(record, "page_data_unavailable");
      }
      return result;
    };
    state.undo.push(replaceMethod(view, "_mergeProcessedPageData", wrappedMerge));
    state.undo.push(function () { window.removeEventListener("pointerdown", onPointerDown, true); });
    window.addEventListener("pointerdown", onPointerDown, true);
    var onUnload = function () { removeView(record, state); };
    state.undo.push(function () { window.removeEventListener("unload", onUnload, true); });
    window.addEventListener("unload", onUnload, true);
    // 独立于故障日志，明确证明本视图已完成挂载；不含正文、路径或账号信息。
    diagnose(state, "view_attached", "reader_selection_hook");
  }

  async function attach(record) {
    try {
      var reader = record.reader;
      if (reader._initPromise) await reader._initPromise;
      if (!record.active || !running) return false;
      var internal = unwrap(reader._internalReader);
      requireValue(internal && internal._primaryView);
      record.internal = internal;
      if (internal.initializedPromise) await internal.initializedPromise;
      if (!record.active || !running) return false;
      var primary = unwrap(internal._primaryView);
      var window = unwrap(reader._iframeWindow);
      requireValue(window && typeof window.addEventListener === "function" &&
        typeof window.removeEventListener === "function");
      if (!record.watching) {
        var onClose = function () {
          release(record);
          readers.delete(reader);
        };
        record.undo.push(function () { window.removeEventListener("unload", onClose, true); });
        window.addEventListener("unload", onClose, true);
        record.watching = true;
      }
      if (primary.initializedPromise) await primary.initializedPromise;
      if (!record.active || !running) return false;
      if (!record.checked) {
        var pdfWindow = unwrap(primary._iframeWindow);
        var app = pdfWindow && unwrap(pdfWindow.PDFViewerApplication);
        var document = app && app.pdfDocument;
        requireValue(document && typeof document.getMetadata === "function");
        var metadata = await document.getMetadata();
        if (!record.active || !running) return false;
        var custom = metadata && metadata.info && metadata.info.Custom;
        record.checked = true;
        if (!custom || !Object.prototype.hasOwnProperty.call(custom, "ZTFSplitLayout")) {
          diagnose(record, "layout_metadata_missing");
          release(record);
          return false;
        }
        record.layout = parseLayout(custom.ZTFSplitLayout);
      }
      if (!record.layout) return fail(record, "layout_unavailable");
      if (!record.createHooked) {
        // 分屏在打开之后才创建：保持同步返回值，只在其初始化完成后安装钩子。
        var create = internal._createView;
        requireValue(typeof create === "function");
        record.undo.push(replaceMethod(internal, "_createView", function () {
          var view = create.apply(this, arguments);
          if (record.active) attachView(record, view).catch(function () {
            fail(record, "view_unavailable");
          });
          return view;
        }));
        record.createHooked = true;
      }
      // getMetadata 等待期间主视图可能被重建，不能继续给旧快照挂钩并误报成功。
      await attachView(record, internal._primaryView);
      if (internal._secondaryView) await attachView(record, internal._secondaryView);
      if (record.active && !record.views.has(unwrap(internal._primaryView))) {
        return fail(record, "primary_view_not_hooked");
      }
      return record.active;
    } catch (error) { return fail(record, "reader_internals_unavailable"); }
  }

  /**
   * 启用模块；必须先于 attachToReader 调用。重复调用不替换本轮依赖。
   * @param {Object} [options] 可选依赖对象；省略时仍可挂钩，但不输出诊断。
   * @param {{debug?: function(string): void}} [options.Zotero] 可选宿主对象，
   *   仅在未提供 log 时调用其可选 debug(line) 方法。
   * @param {function(string): void} [options.log] 可选同步日志函数；log(line) 每次
   *   接收一行已脱敏 JSON 字符串（不含换行、PDF 文本、路径或异常详情），返回值忽略，
   *   抛错被吞掉；fallback 每个 reader/reason 最多一次，hook 每个 view/reason 最多一次。
   *   reader_selection_hook 区分 view_attached 与 selection_applied。优先于 Zotero.debug。
   * @returns {void}
   */
  function init(options) {
    if (running) return;
    dependencies = options || {};
    running = true;
    invalidReaderLogged = false;
  }

  /**
   * 等待 Zotero ReaderInstance 及主/次视图初始化，读取冻结元数据后安装隔离钩子。
   * @param {Object} reader Zotero.Reader 提供的 ReaderInstance；需具有 _internalReader、
   *   _iframeWindow 及相应 PDF 视图内部接口；可选 _initPromise 会被等待。
   * @returns {Promise<boolean>} true 表示本次等待结束时 reader 仍活动且已挂钩；
   *   false 表示未 init、普通 PDF、等待中关闭/shutdown，或内部接口失效已完整回退。
   *   同一 reader 并发调用共享 Promise，重复调用不重复安装；失败实例本轮不再重试。
   *   reader unload 自动清理，未来分屏初始化后自动挂钩；宿主卸载模块必须调用 shutdown。
   */
  function attachToReader(reader) {
    if (!running) return Promise.resolve(false);
    if (!reader || (typeof reader !== "object" && typeof reader !== "function")) {
      if (!invalidReaderLogged) diagnose({}, "reader_unavailable");
      invalidReaderLogged = true;
      return Promise.resolve(false);
    }
    var record = readers.get(reader);
    if (!record) {
      record = { reader: reader, active: true, views: new Map(), undo: [], checked: false };
      readers.set(reader, record);
      liveReaders.add(record);
    }
    if (record.failed) return Promise.resolve(false);
    if (record.pending) return record.pending;
    record.pending = attach(record).finally(function () { record.pending = null; });
    return record.pending;
  }

  /**
   * 停用并同步撤销所有监听、属性/方法包装，恢复字符原生 isolated（含迟到的原生标记）。
   * 可重复调用；使尚在等待的 attach 返回 false。再次使用须先 init，再 attachToReader。
   * @returns {void}
   */
  function shutdown() {
    running = false;
    for (var record of liveReaders) release(record);
    readers = new WeakMap();
    dependencies = null;
  }

  return { init: init, attachToReader: attachToReader, shutdown: shutdown };
})();
