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

// "Test" button in the alert editor: POSTs whatever is currently typed into
// the expression/template boxes, evaluates it server-side against the latest
// archive record, and shows the outcome. Saves nothing, sends nothing.
document.addEventListener('DOMContentLoaded', function () {
  var btn = document.getElementById('test-alert');
  var out = document.getElementById('test-result');
  if (!btn || !out) {
    return;
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function show(nodes) {
    out.textContent = '';
    nodes.forEach(function (n) { out.appendChild(n); });
    out.hidden = false;
  }

  function render(data) {
    if (!data.ok) {
      show([el('p', 'test-error', data.error)]);
      return;
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
    if (!nodes.length) {
      nodes.push(el('p', 'muted', 'Nothing to test -- fill in an expression or a template.'));
    }
    show(nodes);
  }

  btn.addEventListener('click', function () {
    var form = btn.closest('form');
    var body = new URLSearchParams({
      id: form.querySelector('[name=id]').value,
      expression: form.querySelector('[name=expression]').value,
      template: form.querySelector('[name=template]').value
    });
    btn.disabled = true;
    show([el('p', 'muted', 'Testing…')]);
    fetch(btn.dataset.testUrl, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: body.toString()
    })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (e) {
        show([el('p', 'test-error', "Couldn't reach the panel: " + e)]);
      })
      .finally(function () { btn.disabled = false; });
  });
});
