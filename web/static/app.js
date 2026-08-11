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
