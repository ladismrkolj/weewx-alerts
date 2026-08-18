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
      nodes.push(el('p', 'test-error', data.expression.error));
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
        nodes.push(el('p', 'test-error',
                      'Expression failed: ' + data.expression.error +
                      ' -- the alert would not fire.'));
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
      nodes.push(el('pre', 'test-message', data.template.text));
      (data.template.errors || []).forEach(function (e) {
        nodes.push(el('p', 'test-error',
                      '{' + e.field + '} failed (' + e.error +
                      ') -- it stays in the message as literal text.'));
      });
    }
    if (!nodes.length || (!data.expression && !data.template)) {
      nodes.push(el('p', 'muted', "Nothing to test -- that box is empty."));
    }
    return nodes;
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
    }
    btn.disabled = true;
    show(out, [el('p', 'muted', 'Evaluating…')]);
    fetch(btn.dataset.testUrl, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams(params).toString()
    })
      .then(function (r) { return r.json(); })
      .then(function (data) { show(out, debug ? renderDebug(data) : renderTest(data)); })
      .catch(function (e) {
        show(out, [el('p', 'test-error', "Couldn't reach the panel: " + e)]);
      })
      .finally(function () { btn.disabled = false; });
  }

  Array.prototype.forEach.call(buttons, function (btn) {
    btn.addEventListener('click', function () { run(btn); });
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
