// Polls /telegram/status while a "connect this bot" attempt is pending, and
// jumps back to the dashboard (server-rendered) the moment it succeeds.
document.addEventListener('DOMContentLoaded', function () {
  var statusEl = document.getElementById('telegram-status');
  if (!statusEl) {
    return;
  }

  var pollUrl = statusEl.dataset.pollUrl;
  var redirectUrl = statusEl.dataset.redirectUrl;
  var attempts = 0;
  var maxAttempts = 60;   // ~2 minutes at 2s between polls

  function poll() {
    attempts += 1;
    fetch(pollUrl)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.connected) {
          window.location.href = redirectUrl;
          return;
        }
        if (data.error) {
          statusEl.textContent = 'Telegram error: ' + data.error;
        }
        if (attempts < maxAttempts) {
          setTimeout(poll, 2000);
        } else {
          statusEl.textContent = 'Still waiting -- reload the page if you already tapped Start.';
        }
      })
      .catch(function () {
        if (attempts < maxAttempts) {
          setTimeout(poll, 2000);
        }
      });
  }

  setTimeout(poll, 2000);
});

// The three "run this against real data" buttons -- "Test expression" and
// "Test template" in the alert editor, and Evaluate in the expression
// debugger -- all POST to the same endpoint and differ only in what they
// send and how the answer is shown. Each button carries data-test (which
// field to send), an optional data-result (where to draw, default
// #test-result) and data-debug (show the value/type view instead of the
// would-it-fire view).
document.addEventListener('DOMContentLoaded', function () {
  var buttons = document.querySelectorAll('.js-test');
  if (!buttons.length) {
    return;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function show(out, nodes) {
    out.textContent = '';
    nodes.forEach(function (n) { out.appendChild(n); });
    out.hidden = false;
  }

  // A compiler-style pointer at where a syntax error is, e.g.
  //   2 |     and avg('windSpeed', 30, 'knot') > 1
  //     |     ^
  // Only syntax errors carry a position; anything else is just its message.
  function errorNodes(err) {
    var nodes = [];
    if (err.line !== undefined) {
      var gutter = String(err.lineno) + ' | ';
      var caret = ' '.repeat(gutter.length + Math.max(0, (err.offset || 1) - 1)) + '^';
      nodes.push(el('pre', 'code-frame', gutter + err.line + '\n' + caret));
    }
    nodes.push(el('p', 'test-error', err.error));
    return nodes;
  }

  function kb(bytes) {
    return (bytes / 1024).toFixed(bytes < 10240 ? 1 : 0) + ' kB';
  }

  // What the snapshot box in the editor produced: the frame itself, plus
  // what it cost -- how long the camera took and what shrinking saved.
  function snapshotNodes(snapshot) {
    var nodes = [el('h3', null, 'Snapshot')];
    if (snapshot.error) {
      nodes.push(el('p', 'test-error',
                    'Camera failed: ' + snapshot.error +
                    ' -- the message would still be sent, without a picture.'));
      return nodes;
    }
    var img = document.createElement('img');
    img.className = 'snapshot-preview';
    img.src = snapshot.preview;
    img.alt = 'The frame that would be sent with this alert';
    nodes.push(img);
    if (snapshot.from_default) {
      nodes.push(el('p', 'muted', 'From the station camera: ' + snapshot.url));
    }
    var summary = snapshot.compressed
      ? kb(snapshot.original_bytes) + ' from the camera, sent as ' +
        kb(snapshot.bytes) + ' (' +
        Math.round(100 - snapshot.bytes * 100 / snapshot.original_bytes) + '% smaller)'
      : kb(snapshot.bytes) + ', sent as-is';
    nodes.push(el('p', 'muted', summary + ' -- camera took ' + snapshot.fetch_ms + ' ms.'));
    return nodes;
  }

  function recordDetails(record) {
    var details = document.createElement('details');
    details.appendChild(el('summary', null,
                           'Record it was evaluated against (' + record.length + ' fields)'));
    var chips = el('div', 'field-chips');
    record.forEach(function (f) {
      chips.appendChild(el('code', null, f.name + ' = ' + f.value));
    });
    details.appendChild(chips);
    return details;
  }

  // Debugger view: what did this expression actually return?
  function renderDebug(data) {
    if (!data.ok) {
      return [el('p', 'test-error', data.error)];
    }
    var nodes = [];
    if (!data.expression) {
      return [el('p', 'muted', 'Type an expression first.')];
    }
    if (data.expression.error) {
      errorNodes(data.expression).forEach(function (n) { nodes.push(n); });
    } else {
      nodes.push(el('pre', 'test-message', data.expression.value));
      nodes.push(el('p', 'muted',
                    'type: ' + data.expression.type +
                    ' -- as an alert condition this counts as ' +
                    (data.expression.triggered ? 'TRUE' : 'FALSE') + '.'));
    }
    if (data.record_time) {
      nodes.push(el('p', 'muted', 'Archive record from ' + data.record_time + '.'));
    }
    if (data.record) {
      nodes.push(recordDetails(data.record));
    }
    return nodes;
  }

  // Alert-editor view: would this alert fire, and what would it send?
  function renderTest(data) {
    if (!data.ok) {
      return [el('p', 'test-error', data.error)];
    }
    var nodes = [];
    if (data.record_time) {
      nodes.push(el('p', 'muted', 'Tested against the archive record from ' +
                                  data.record_time + '.'));
    }
    if (data.expression) {
      if (data.expression.error) {
        errorNodes(data.expression).forEach(function (n) { nodes.push(n); });
        nodes.push(el('p', 'muted', 'The alert would not fire.'));
      } else {
        nodes.push(el('p', data.expression.triggered ? 'test-ok' : 'test-idle',
                      data.expression.triggered
                        ? 'Expression is TRUE right now -- the alert would fire.'
                        : 'Expression is FALSE right now -- the alert would not fire.'));
        nodes.push(el('p', 'muted', 'Value: ' + data.expression.value));
      }
    }
    if (data.template) {
      nodes.push(el('h3', null, 'Message'));
      if (data.template.subject) {
        nodes.push(el('p', 'muted', 'Subject (email only): ' + data.template.subject));
      }
      nodes.push(el('pre', 'test-message', data.template.text));
      (data.template.errors || []).forEach(function (e) {
        nodes.push(el('p', 'test-error',
                      '{' + e.field + '} failed (' + e.error +
                      ') -- it stays in the message as literal text.'));
      });
    }
    if (data.snapshot) {
      snapshotNodes(data.snapshot).forEach(function (n) { nodes.push(n); });
    }
    if (!nodes.length || (!data.expression && !data.template)) {
      nodes.push(el('p', 'muted', "Nothing to test -- that box is empty."));
    }
    return nodes;
  }

  // The snapshot boxes live outside the expression/template pair, so both
  // the preview and the send have to carry them along explicitly.
  function addSnapshotFields(form, params) {
    var enabled = form.querySelector('[name=image_enabled]');
    if (!enabled || !enabled.checked) {
      return;
    }
    params.image_enabled = '1';
    // Blank is meaningful: the server falls back to the station-wide
    // default_image_url from weewx.conf.
    params.image_url = form.querySelector('[name=image_url]').value;
    var compress = form.querySelector('[name=image_compress]');
    if (compress && compress.checked) {
      params.image_compress = '1';
      params.image_max_width = form.querySelector('[name=image_max_width]').value;
      params.image_quality = form.querySelector('[name=image_quality]').value;
    }
  }

  function run(btn) {
    var form = btn.closest('form');
    var field = btn.dataset.test;
    var debug = btn.dataset.debug === '1';
    var out = document.getElementById(btn.dataset.result || 'test-result');
    var params = {};
    params[field] = form.querySelector('[name=' + field + ']').value;
    if (debug) {
      params.include_record = '1';
    } else {
      // Only used to render {alert_id} in a template preview.
      params.id = form.querySelector('[name=id]').value;
      if (field === 'template') {
        var subjectBox = form.querySelector('[name=subject]');
        params.subject = subjectBox ? subjectBox.value : '';
        addSnapshotFields(form, params);
      }
    }
    btn.disabled = true;
    show(out, [el('p', 'muted', 'Evaluating…')]);
    fetch(btn.dataset.testUrl, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams(params).toString()
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        show(out, debug ? renderDebug(data) : renderTest(data));
        // Mark the offending line in the editor's gutter, so a syntax error
        // points at the text as well as describing it.
        var box = form.querySelector('[name=' + field + ']');
        if (box && box.markErrorLine) {
          box.markErrorLine(data.ok && data[field] ? data[field].lineno : null);
        }
      })
      .catch(function (e) {
        show(out, [el('p', 'test-error', "Couldn't reach the panel: " + e)]);
      })
      .finally(function () { btn.disabled = false; });
  }

  Array.prototype.forEach.call(buttons, function (btn) {
    btn.addEventListener('click', function () { run(btn); });
  });

  // "Send test message": same render, but it actually goes out over the
  // ticked channels, so it confirms first and reports per channel.
  function renderSend(data) {
    if (!data.ok) {
      return [el('p', 'test-error', data.error)];
    }
    var nodes = [];
    if (data.record_time) {
      nodes.push(el('p', 'muted', 'Rendered from the archive record from ' +
                                  data.record_time + '.'));
    }
    if (data.subject) {
      nodes.push(el('p', 'muted', 'Subject (email only): ' + data.subject));
    }
    nodes.push(el('pre', 'test-message', data.text));
    (data.errors || []).forEach(function (e) {
      nodes.push(el('p', 'test-error',
                    '{' + e.field + '} failed (' + e.error +
                    ') -- it stays in the message as literal text.'));
    });
    if (data.snapshot) {
      snapshotNodes(data.snapshot).forEach(function (n) { nodes.push(n); });
    }
    (data.sent || []).forEach(function (r) {
      nodes.push(r.ok
        ? el('p', 'test-ok', 'Sent via ' + r.channel + '.')
        : el('p', 'test-error', r.channel + ' failed: ' + r.error));
    });
    return nodes;
  }

  Array.prototype.forEach.call(document.querySelectorAll('.js-send'), function (btn) {
    btn.addEventListener('click', function () {
      var form = btn.closest('form');
      var out = document.getElementById('test-result');
      var checked = Array.prototype.filter.call(
        form.querySelectorAll('[name=channels]'), function (c) { return c.checked; });
      if (!checked.length) {
        show(out, [el('p', 'test-error',
                      'Tick at least one channel above to send a test to.')]);
        return;
      }
      var names = checked.map(function (c) { return c.value; }).join(', ');
      if (!window.confirm('Send this message for real over: ' + names + '?')) {
        return;
      }
      var subjectBox = form.querySelector('[name=subject]');
      var fields = {
        id: form.querySelector('[name=id]').value,
        template: form.querySelector('[name=template]').value,
        subject: subjectBox ? subjectBox.value : ''
      };
      addSnapshotFields(form, fields);
      var params = new URLSearchParams(fields);
      checked.forEach(function (c) { params.append('channels', c.value); });
      btn.disabled = true;
      show(out, [el('p', 'muted', 'Sending…')]);
      fetch(btn.dataset.sendUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: params.toString()
      })
        .then(function (r) { return r.json(); })
        .then(function (data) { show(out, renderSend(data)); })
        .catch(function (e) {
          show(out, [el('p', 'test-error', "Couldn't reach the panel: " + e)]);
        })
        .finally(function () { btn.disabled = false; });
    });
  });

  // Ctrl/Cmd+Enter in the debugger box evaluates, so it behaves like the
  // REPL it stands in for.
  var debugForm = document.getElementById('debug-form');
  if (debugForm) {
    debugForm.querySelector('textarea').addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
        ev.preventDefault();
        run(debugForm.querySelector('.js-test'));
      }
    });
  }
});

// Turns each textarea[data-code] into a small code editor: line numbers,
// syntax highlighting, Tab to indent, auto-indent on Enter, and the error
// line from the last test marked in the gutter. No libraries -- the panel
// is meant to work on a LAN with no internet, so nothing is loaded from a
// CDN, and it's all progressive: the textarea is still the real form field,
// so if any of this fails the box keeps working as a plain textarea.
document.addEventListener('DOMContentLoaded', function () {
  var INDENT = '    ';

  // Names the expression language defines -- highlighted as functions so a
  // typo ("to_c") visibly isn't one. Kept in sync by hand with
  // useralerts.py's namespace builders.
  var FUNCTIONS = ['avg', 'amin', 'amax', 'asum', 'to_C', 'to_F', 'to_kts',
                   'to_mps', 'convert', 'compass',
                   'abs', 'round', 'min', 'max', 'len'];
  var KEYWORDS = ['and', 'or', 'not', 'is', 'in', 'if', 'else',
                  'True', 'False', 'None'];
  // Injected per record: the time/date values, plus what only a template
  // gets. Not fields, so they're highlighted as built-in values.
  var INJECTED = ['hour', 'minute', 'minute_of_day', 'weekday', 'day',
                  'month', 'yday', 'year', 'dateTime_str', 'alert_id'];

  var TOKEN_RE = new RegExp([
    "'(?:[^'\\\\\\n]|\\\\.)*'?",          // 'single quoted'
    '"(?:[^"\\\\\\n]|\\\\.)*"?',          // "double quoted"
    '\\b\\d+(?:\\.\\d*)?\\b',             // 123, 1.5
    '\\b[A-Za-z_][A-Za-z0-9_]*\\b',       // name
    '[=!<>]=|[-+*/%<>()\\[\\],:=]'        // operators / punctuation
  ].join('|'), 'g');

  function escapeHtml(text) {
    return text.replace(/[&<>]/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c];
    });
  }

  function span(cls, text) {
    return '<span class="tok-' + cls + '">' + escapeHtml(text) + '</span>';
  }

  function classify(token, source, index) {
    var c = token[0];
    if (c === "'" || c === '"') { return 'str'; }
    if (c >= '0' && c <= '9') { return 'num'; }
    if (/[A-Za-z_]/.test(c)) {
      if (KEYWORDS.indexOf(token) !== -1) { return 'kw'; }
      if (FUNCTIONS.indexOf(token) !== -1) { return 'fn'; }
      if (INJECTED.indexOf(token) !== -1) { return 'builtin'; }
      // A name followed by '(' is being called -- an unknown function,
      // which is worth showing as a call rather than as a field.
      if (/^\s*\(/.test(source.slice(index + token.length))) { return 'fn'; }
      return 'name';
    }
    return 'op';
  }

  // Highlight one expression: the whole box in expression mode, and the
  // inside of each {...} in template mode.
  function highlightExpression(source) {
    var out = '';
    var last = 0;
    var match;
    TOKEN_RE.lastIndex = 0;
    while ((match = TOKEN_RE.exec(source)) !== null) {
      out += escapeHtml(source.slice(last, match.index));
      out += span(classify(match[0], source, match.index), match[0]);
      last = match.index + match[0].length;
    }
    return out + escapeHtml(source.slice(last));
  }

  // A template is literal text plus {...} placeholders ({{ and }} are
  // literal braces). An unclosed '{' is highlighted as an error, since
  // that's exactly the typo that silently swallows the rest of a message.
  function highlightTemplate(source) {
    var out = '';
    var i = 0;
    while (i < source.length) {
      var c = source[i];
      if (c === '{' && source[i + 1] === '{') { out += span('brace', '{{'); i += 2; continue; }
      if (c === '}' && source[i + 1] === '}') { out += span('brace', '}}'); i += 2; continue; }
      if (c === '{') {
        var end = source.indexOf('}', i + 1);
        if (end === -1) {
          out += span('err', source.slice(i));
          break;
        }
        out += span('brace', '{') +
               highlightExpression(source.slice(i + 1, end)) +
               span('brace', '}');
        i = end + 1;
        continue;
      }
      var next = source.indexOf('{', i);
      var stop = next === -1 ? source.length : next;
      out += escapeHtml(source.slice(i, stop));
      i = stop;
    }
    return out;
  }

  function enhance(textarea) {
    var mode = textarea.dataset.code;
    var wrap = document.createElement('div');
    wrap.className = 'code-editor';
    var gutter = document.createElement('div');
    gutter.className = 'code-gutter';
    var stack = document.createElement('div');
    stack.className = 'code-stack';
    var pre = document.createElement('pre');
    pre.className = 'code-highlight';
    pre.setAttribute('aria-hidden', 'true');

    textarea.parentNode.insertBefore(wrap, textarea);
    wrap.appendChild(gutter);
    wrap.appendChild(stack);
    stack.appendChild(pre);
    stack.appendChild(textarea);

    var errorLine = null;

    function paint() {
      var source = textarea.value;
      pre.innerHTML = (mode === 'template' ? highlightTemplate(source)
                                           : highlightExpression(source)) + '\n';
      var lines = source.split('\n').length;
      var html = '';
      for (var n = 1; n <= lines; n++) {
        html += '<div' + (n === errorLine ? ' class="err"' : '') + '>' + n + '</div>';
      }
      gutter.innerHTML = html;
      // Grow with the content, so a long expression isn't edited through a
      // 4-line porthole -- but stay bounded so the page doesn't run away.
      var rows = Math.min(Math.max(lines, textarea.rows), 20);
      textarea.style.height = (rows * 1.5) + 'em';
    }

    function sync() {
      pre.scrollLeft = textarea.scrollLeft;
      pre.scrollTop = textarea.scrollTop;
      gutter.scrollTop = textarea.scrollTop;
    }

    function replaceSelection(text, caret) {
      var start = textarea.selectionStart;
      var end = textarea.selectionEnd;
      textarea.setRangeText(text, start, end, 'end');
      if (caret !== undefined) {
        textarea.selectionStart = textarea.selectionEnd = start + caret;
      }
      paint();
    }

    textarea.addEventListener('input', function () { errorLine = null; paint(); });
    textarea.addEventListener('scroll', sync);

    textarea.addEventListener('keydown', function (ev) {
      if (ev.key === 'Tab') {
        // Tab indents instead of leaving the box. Escape first, then Tab,
        // still moves on -- the standard way out of a code editor.
        ev.preventDefault();
        var start = textarea.selectionStart;
        var lineStart = textarea.value.lastIndexOf('\n', start - 1) + 1;
        if (ev.shiftKey) {
          var head = textarea.value.slice(lineStart, start);
          var strip = Math.min(head.length - head.replace(/^ {1,4}/, '').length, head.length);
          if (strip) {
            textarea.setRangeText('', lineStart, lineStart + strip, 'end');
            paint();
          }
        } else {
          replaceSelection(INDENT);
        }
      } else if (ev.key === 'Enter' && !ev.ctrlKey && !ev.metaKey) {
        // Keep the current line's indentation, like any editor would.
        var pos = textarea.selectionStart;
        var from = textarea.value.lastIndexOf('\n', pos - 1) + 1;
        var indent = (textarea.value.slice(from, pos).match(/^[ \t]*/) || [''])[0];
        if (indent) {
          ev.preventDefault();
          replaceSelection('\n' + indent);
        }
      }
    });

    paint();
    // The gutter must not scroll on its own -- it only follows the textarea.
    gutter.addEventListener('wheel', function (ev) {
      textarea.scrollTop += ev.deltaY;
      sync();
      ev.preventDefault();
    }, {passive: false});

    // Used by the test buttons to point at the line a syntax error is on.
    textarea.markErrorLine = function (lineno) {
      errorLine = lineno || null;
      paint();
    };
  }

  Array.prototype.forEach.call(document.querySelectorAll('textarea[data-code]'), enhance);
});
